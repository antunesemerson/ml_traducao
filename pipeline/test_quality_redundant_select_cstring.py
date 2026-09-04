from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from local_quality_validator import (  # noqa: E402
    collapse_redundant_select_cstring,
    redundant_select_cstring_calls,
)
from quality_promotion_cycle import load_providers  # noqa: E402
from quality_redundant_select_cstring_pairwise_evidence import (  # noqa: E402
    EVIDENCE_TYPE,
    prepare_evidence,
    sha256_text,
)
from quality_redundant_select_cstring_shadow import (  # noqa: E402
    ELIGIBLE_LANE,
    RULE_VERSION as SOURCE_RULE_VERSION,
    intentional_elision_token_integrity,
)


class RedundantSelectCStringTests(unittest.TestCase):
    def test_collapses_exact_wrappers_with_nested_conditions(self) -> None:
        before = (
            "[actor.GetShortUIName|U] "
            "[Select_CString(And(actor.IsFemale, actor.IsLocalPlayer), "
            "'continuará', 'continuará')] e "
            "[Select_CString(actor.IsLocalPlayer, 'sua', 'sua')] corte"
        )

        after, repairs = collapse_redundant_select_cstring(before)

        self.assertEqual(
            after,
            "[actor.GetShortUIName|U] continuará e sua corte",
        )
        self.assertEqual(len(repairs), 2)
        self.assertTrue(
            intentional_elision_token_integrity(before, after, repairs)
        )

    def test_keeps_unequal_case_filtered_and_nested_calls(self) -> None:
        samples = (
            "[Select_CString(actor.IsFemale, 'a', 'o')]",
            "[Select_CString(actor.IsFemale, 'Você', 'você')]",
            "[Select_CString(actor.IsFemale, 'nome', 'nome')|U]",
            "[Concept('vassal',Select_CString(actor.IsFemale,'vassala','vassala'))|E]",
        )

        for sample in samples:
            with self.subTest(sample=sample):
                after, repairs = collapse_redundant_select_cstring(sample)
                self.assertEqual(after, sample)
                self.assertEqual(repairs, [])

    def test_detector_keeps_contextual_equalities_visible(self) -> None:
        text = "[Select_CString(actor.IsFemale, 'Você', 'você')]"

        calls = redundant_select_cstring_calls(text)

        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["exact_wrapper"])
        self.assertFalse(calls[0]["exact_literal_match"])

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
            VALUES (
              11,
              101,
              '[Select_CString(actor.IsLocalPlayer, ''continuará'', ''continuará'')]',
              0.03
            );
            """
        )
        baseline = (
            "[Select_CString(actor.IsLocalPlayer, 'continuará', 'continuará')]"
        )
        candidate = "continuará"
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
                "raw_candidate_score": 0.03,
                "calibrated_candidate_score": 0.05,
            }
        ]

        evidence = prepare_evidence(conn, shadow_rows)
        conn.close()

        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["candidate_text"], candidate)
        self.assertEqual(EVIDENCE_TYPE, "deterministic_redundant_select_cstring_repair")
        self.assertAlmostEqual(evidence[0]["pairwise_delta"], 0.02)

    def test_provider_is_registered(self) -> None:
        providers = {provider.provider_id: provider for provider in load_providers()}

        self.assertIn("redundant_select_cstring", providers)
        provider = providers["redundant_select_cstring"]
        self.assertEqual(provider.evidence_type, EVIDENCE_TYPE)
        self.assertEqual(
            provider.discovery_issue_types,
            ("redundant_select_cstring_options",),
        )


if __name__ == "__main__":
    unittest.main()
