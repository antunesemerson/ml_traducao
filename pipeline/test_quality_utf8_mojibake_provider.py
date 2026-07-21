from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from quality_utf8_mojibake_pairwise_evidence import prepare_evidence, sha256_text  # noqa: E402
from quality_utf8_mojibake_shadow import repair_utf8_mojibake  # noqa: E402


class QualityUtf8MojibakeProviderTests(unittest.TestCase):
    def test_repair_is_exact_and_preserves_non_mojibake_text(self) -> None:
        baseline = "A coroa serÃ¡ entregue e a decisÃ£o terÃ¡ efeito."

        candidate, repairs = repair_utf8_mojibake(baseline)

        self.assertEqual(candidate, "A coroa será entregue e a decisão terá efeito.")
        self.assertEqual(sum(item["occurrence_count"] for item in repairs), 3)

    def test_prepare_evidence_keeps_locked_rows_for_human_review(self) -> None:
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
            INSERT INTO ml_score_items VALUES (11, 101, 'A decisÃ£o vencerÃ¡.', 0.31);
            """
        )
        baseline = "A decisÃ£o vencerÃ¡."
        candidate = "A decisão vencerá."
        shadow_rows = [
            {
                "score_run_id": 11,
                "model_run_id": 7,
                "segment_id": 101,
                "relative_path": "events/test_l_spanish.yml",
                "source_key": "test_key",
                "blockers": [],
                "token_integrity_ok": True,
                "human_locked": True,
                "baseline_hash": sha256_text(baseline),
                "candidate_hash": sha256_text(candidate),
                "raw_candidate_score": 0.20,
                "calibrated_candidate_score": 0.33,
            }
        ]

        evidence = prepare_evidence(conn, shadow_rows)
        conn.close()

        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["candidate_text"], candidate)
        self.assertEqual(evidence[0]["recommended_route"], "human_unlock_review")
        self.assertTrue(
            evidence[0]["source_metadata"]["promotion_requires_human_unlock"]
        )
        self.assertAlmostEqual(evidence[0]["pairwise_delta"], 0.02)


if __name__ == "__main__":
    unittest.main()
