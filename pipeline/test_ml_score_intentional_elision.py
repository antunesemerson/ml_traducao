from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from apply_safe_output_updates import protected_tokens  # noqa: E402
from ml_score_segments import (  # noqa: E402
    final_decision,
    load_pairwise_elision_evidence_by_candidate,
)
from quality_spanish_dynamic_literal_authorization import (  # noqa: E402
    PAIRWISE_ELISION_CONFIRMATION_LABEL,
    PAIRWISE_ELISION_CONFIRMATION_SOURCE,
    PAIRWISE_ELISION_EVIDENCE_TYPE,
    PAIRWISE_ELISION_SOURCE,
    sha256_text,
)


class MlScoreIntentionalElisionTests(unittest.TestCase):
    def row(self, candidate: str | None = None) -> dict[str, object]:
        baseline = (
            "[actor.GetName] "
            "[Select_CString(actor.IsLocalPlayer,'ganaste','ganó')] adquiriu ouro"
        )
        expected = "[actor.GetName] adquiriu ouro"
        selected = candidate if candidate is not None else expected
        return {
            "segment_id": 2733,
            "relative_path": "events/test_l_spanish.yml",
            "source_key": "test_key",
            "source_line_number": 1,
            "english_text": "[actor.GetName] gained gold",
            "spanish_text": baseline,
            "old_text": baseline,
            "output_text": selected,
            "candidate_text": selected,
            "candidate_text_source": "output",
            "confirmation_level": "auto_confirmed",
            "confirmed_text": selected,
            "confirmation_source": PAIRWISE_ELISION_CONFIRMATION_SOURCE,
            "confirmation_label": PAIRWISE_ELISION_CONFIRMATION_LABEL,
            "locked": 0,
            "pairwise_evidence_id": 841,
            "pairwise_evidence_type": PAIRWISE_ELISION_EVIDENCE_TYPE,
            "pairwise_baseline_text": baseline,
            "pairwise_baseline_hash": sha256_text(baseline),
            "pairwise_candidate_hash": sha256_text(expected),
            "pairwise_token_integrity_ok": 1,
            "pairwise_post_validation_clean": 1,
            "pairwise_training_eligible": 0,
            "pairwise_promotion_eligible": 0,
            "pairwise_source_metadata_json": json.dumps(
                {
                    "source": PAIRWISE_ELISION_SOURCE,
                    "source_token_status": "intentional_elision",
                }
            ),
        }

    def decision(self, row: dict[str, object]) -> dict[str, object]:
        return final_decision(
            row,
            model_action="needs_autofix",
            safe_probability=0.15,
            model_confidence=0.70,
            probability_by_class={"auto_safe": 0.15, "needs_autofix": 0.70},
            safe_threshold=0.90,
        )

    def test_approved_elision_is_not_blocked_as_structure(self) -> None:
        result = self.decision(self.row())

        self.assertEqual(result["token_status"], "intentional_elision")
        self.assertEqual(result["final_action"], "needs_autofix")
        self.assertEqual(result["deterministic_blocked"], 0)
        self.assertIn(
            "deterministic:allow_pairwise_intentional_elision",
            result["reasons"],
        )

    def test_unrelated_token_loss_remains_blocked(self) -> None:
        row = self.row(candidate="adquiriu ouro")
        self.assertNotEqual(
            protected_tokens(row["spanish_text"]),
            protected_tokens(row["candidate_text"]),
        )

        result = self.decision(row)

        self.assertEqual(result["token_status"], "mismatch")
        self.assertEqual(result["final_action"], "blocked_structure")
        self.assertEqual(result["deterministic_blocked"], 1)

    def test_evidence_lookup_loads_only_latest_eligible_pair(self) -> None:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute(
            """
            CREATE TABLE ml_pairwise_quality_evidence (
                id INTEGER PRIMARY KEY,
                segment_id INTEGER NOT NULL,
                candidate_text TEXT NOT NULL,
                evidence_type TEXT NOT NULL,
                token_integrity_ok INTEGER NOT NULL,
                post_validation_clean INTEGER NOT NULL,
                last_run_id INTEGER NOT NULL,
                baseline_text TEXT NOT NULL,
                baseline_hash TEXT NOT NULL,
                candidate_hash TEXT NOT NULL,
                training_eligible INTEGER NOT NULL,
                promotion_eligible INTEGER NOT NULL,
                source_metadata_json TEXT NOT NULL
            )
            """
        )
        rows = [
            (1, 10, "candidate", PAIRWISE_ELISION_EVIDENCE_TYPE, 1, 1, 1, "baseline-v1"),
            (2, 10, "candidate", PAIRWISE_ELISION_EVIDENCE_TYPE, 1, 1, 2, "baseline-v2"),
            (3, 11, "ignored", PAIRWISE_ELISION_EVIDENCE_TYPE, 0, 1, 3, "baseline"),
        ]
        con.executemany(
            """
            INSERT INTO ml_pairwise_quality_evidence (
                id, segment_id, candidate_text, evidence_type,
                token_integrity_ok, post_validation_clean, last_run_id,
                baseline_text, baseline_hash, candidate_hash,
                training_eligible, promotion_eligible, source_metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'baseline-hash', 'candidate-hash', 0, 0, '{}')
            """,
            rows,
        )

        evidence = load_pairwise_elision_evidence_by_candidate(con)

        self.assertEqual(set(evidence), {(10, "candidate")})
        self.assertEqual(evidence[(10, "candidate")]["pairwise_evidence_id"], 2)
        self.assertEqual(
            evidence[(10, "candidate")]["pairwise_baseline_text"],
            "baseline-v2",
        )


if __name__ == "__main__":
    unittest.main()
