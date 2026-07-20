from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from apply_safe_output_updates import protected_tokens  # noqa: E402
from quality_dynamic_name_de_prefix_pairwise_evidence import (  # noqa: E402
    EVIDENCE_TYPE,
    prepare_evidence,
    sha256_text,
)
from quality_dynamic_name_de_prefix_shadow import (  # noqa: E402
    ELIGIBLE_LANE,
    RULE_VERSION as SOURCE_RULE_VERSION,
    repair_dynamic_name_de_prefix,
)


class DynamicNameDePrefixTests(unittest.TestCase):
    def test_repairs_only_first_and_full_name_getters(self) -> None:
        before = (
            "Defesa d [guest.GetFirstNameNoTooltip] e companhia d [owner.GetFullName]. "
            "Titulo d [host.GetTitleAsNameNoTooltip] e d [host.Custom('ES_DelDela')]."
        )
        after, replacements = repair_dynamic_name_de_prefix(before)

        self.assertEqual(
            after,
            (
                "Defesa de [guest.GetFirstNameNoTooltip] e companhia de [owner.GetFullName]. "
                "Titulo d [host.GetTitleAsNameNoTooltip] e d [host.Custom('ES_DelDela')]."
            ),
        )
        self.assertEqual(len(replacements), 2)
        self.assertEqual(protected_tokens(before), protected_tokens(after))

    def test_preserves_uppercase_at_sentence_start(self) -> None:
        candidate, _ = repair_dynamic_name_de_prefix("D [guest.GetFirstName] depende tudo.")

        self.assertEqual(candidate, "De [guest.GetFirstName] depende tudo.")

    def test_prepare_evidence_rebuilds_exact_monotonic_pair(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE ml_score_runs (id INTEGER PRIMARY KEY, model_run_id INTEGER);
            CREATE TABLE ml_score_items (
              run_id INTEGER,
              segment_id INTEGER,
              candidate_text TEXT,
              model_safe_probability REAL
            );
            INSERT INTO ml_score_runs VALUES (11, 7);
            INSERT INTO ml_score_items
            VALUES (11, 101, 'Defesa d [guest.GetFirstNameNoTooltip].', 0.31);
            """
        )
        baseline = "Defesa d [guest.GetFirstNameNoTooltip]."
        candidate = "Defesa de [guest.GetFirstNameNoTooltip]."
        shadow_rows = [
            {
                "source": SOURCE_RULE_VERSION,
                "lane": ELIGIBLE_LANE,
                "score_run_id": 11,
                "model_run_id": 7,
                "segment_id": 101,
                "relative_path": "events/test_l_spanish.yml",
                "source_key": "test_key",
                "blockers": [],
                "token_integrity_ok": True,
                "baseline_hash": sha256_text(baseline),
                "candidate_hash": sha256_text(candidate),
                "raw_candidate_score": 0.30,
                "calibrated_candidate_score": 0.33,
            }
        ]

        evidence = prepare_evidence(conn, shadow_rows)
        conn.close()

        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["candidate_text"], candidate)
        self.assertEqual(evidence[0]["source_metadata"]["source"], SOURCE_RULE_VERSION)
        self.assertEqual(len(evidence[0]["evidence_key"]), 64)
        self.assertEqual(EVIDENCE_TYPE, "deterministic_dynamic_name_de_prefix_repair")
        self.assertAlmostEqual(evidence[0]["pairwise_delta"], 0.02)


if __name__ == "__main__":
    unittest.main()
