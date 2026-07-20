from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from quality_mojibake_lexicon_pairwise_evidence import (  # noqa: E402
    EVIDENCE_TYPE,
    apply_recorded_replacements,
    load_shadow_rows,
    prepare_evidence,
    sha256_text,
)
from quality_mojibake_lexicon_shadow import (  # noqa: E402
    ELIGIBLE_LANE,
    RULE_VERSION as SOURCE_RULE_VERSION,
)


class QualityMojibakeLexiconPairwiseEvidenceTests(unittest.TestCase):
    def test_recorded_replacements_require_an_exact_occurrence_contract(self) -> None:
        replacements = [
            {"original": "poder?", "replacement": "poderá", "corpus_support": 2},
            {"original": "poder?", "replacement": "poderá", "corpus_support": 2},
        ]

        self.assertEqual(
            apply_recorded_replacements("Ele poder? e ela poder?", replacements),
            "Ele poderá e ela poderá",
        )
        with self.assertRaisesRegex(RuntimeError, "no longer matches"):
            apply_recorded_replacements("Ele poder?", replacements)

    def test_shadow_loader_keeps_only_current_eligible_rows(self) -> None:
        rows = [
            {"source": SOURCE_RULE_VERSION, "lane": ELIGIBLE_LANE, "segment_id": 1},
            {"source": SOURCE_RULE_VERSION, "lane": "blocked_or_context", "segment_id": 2},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shadow.jsonl"
            path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )

            selected = load_shadow_rows(path)

        self.assertEqual([row["segment_id"] for row in selected], [1])

    def test_prepare_evidence_rebuilds_exact_text_and_preserves_tokens(self) -> None:
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
            INSERT INTO ml_score_items VALUES (11, 101, 'Ele poder? vencer.', 0.31);
            """
        )
        baseline = "Ele poder? vencer."
        candidate = "Ele poderá vencer."
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
                "replacements": [
                    {"original": "poder?", "replacement": "poderá", "corpus_support": 2}
                ],
                "raw_candidate_score": 0.33,
                "calibrated_candidate_score": 0.33,
            }
        ]

        evidence = prepare_evidence(conn, shadow_rows)
        conn.close()

        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["candidate_text"], candidate)
        self.assertAlmostEqual(evidence[0]["pairwise_delta"], 0.02)
        self.assertEqual(len(evidence[0]["evidence_key"]), 64)
        self.assertEqual(
            evidence[0]["source_metadata"]["source"], SOURCE_RULE_VERSION
        )


if __name__ == "__main__":
    unittest.main()
