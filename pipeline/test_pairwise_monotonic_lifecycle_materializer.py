from __future__ import annotations

import sys
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from pairwise_monotonic_lifecycle_materializer import evaluate  # noqa: E402
from segment_state_snapshot import (  # noqa: E402
    PAIRWISE_MONOTONIC_REPAIR_LIFECYCLE_CLOSURE_ACTION,
    lifecycle_closure_allowed,
)


class PairwiseMonotonicLifecycleMaterializerTests(unittest.TestCase):
    def test_registered_generic_pairwise_confirmation_is_released(self) -> None:
        evidence_type = "deterministic_new_quality_pattern"
        record = {
            "segment_id": 1,
            "old_text": "old",
            "output_text": "new",
            "confirmed_text": "new",
            "confirmation_source": "pairwise_monotonic_repair",
            "confirmation_label": "pairwise_any_stable_label_v1",
            "confirmation_locked": 0,
            "final_state": "reopen_auto_confirmed_autofix",
            "needs_output_apply": 0,
            "confirmed_matches_output": 1,
            "open_issue_count": 0,
            "evidence_id": 42,
            "evidence_type": evidence_type,
            "baseline_text": "old",
            "candidate_text": "new",
            "promotion_eligible": 0,
            "token_integrity_ok": 1,
            "post_validation_clean": 1,
        }

        result = evaluate(record, {evidence_type})

        self.assertTrue(result["policy_allowed"])
        self.assertEqual(result["evidence_kind"], evidence_type)
        self.assertEqual(result["block_reasons"], [])

    def test_unregistered_pairwise_evidence_is_blocked(self) -> None:
        record = {
            "old_text": "old",
            "output_text": "new",
            "confirmed_text": "new",
            "confirmation_source": "pairwise_monotonic_repair",
            "confirmation_label": "pairwise_unknown_v1",
            "confirmation_locked": 0,
            "final_state": "pending_apply_confirmed",
            "needs_output_apply": 1,
            "confirmed_matches_output": 0,
            "open_issue_count": 0,
            "evidence_id": 99,
            "evidence_type": "unknown_evidence",
            "baseline_text": "old",
            "candidate_text": "new",
            "promotion_eligible": 1,
            "token_integrity_ok": 1,
            "post_validation_clean": 1,
        }

        result = evaluate(record, {"registered_evidence"})

        self.assertFalse(result["policy_allowed"])
        self.assertIn("pairwise_evidence_type_not_registered", result["block_reasons"])

    def test_segment_state_accepts_generic_pairwise_confirmation_label(self) -> None:
        row = {
            "lifecycle_policy_action": PAIRWISE_MONOTONIC_REPAIR_LIFECYCLE_CLOSURE_ACTION,
            "lifecycle_policy_allowed": 1,
            "confirmation_source": "pairwise_monotonic_repair",
            "confirmation_label": "pairwise_deterministic_new_quality_pattern_v1",
        }

        self.assertTrue(lifecycle_closure_allowed(row))


if __name__ == "__main__":
    unittest.main()
