from __future__ import annotations

import unittest

from dashboard import backend


class PairwiseCalibrationGateTests(unittest.TestCase):
    def test_required_pending_review_is_advisory_for_evaluation(self) -> None:
        safety = {
            "evaluation_gate": {
                "status": "liberada",
                "can_start_evaluation_full_production_now": True,
                "blocking_reasons": [],
            }
        }
        diff_review = {
            "calibration_review": {
                "policy_decision": "required",
                "pending_count": 16,
            }
        }

        backend._apply_pairwise_calibration_gate(safety, diff_review)

        gate = safety["evaluation_gate"]
        self.assertTrue(gate["can_start_evaluation_full_production_now"])
        self.assertEqual(gate["calibration_pending_count"], 16)
        self.assertEqual(gate["calibration_review_status"], "pending")
        self.assertIn("pairwise_calibration_review_pending", gate["advisory_reasons"])
        self.assertNotIn("pairwise_calibration_review_pending", gate["blocking_reasons"])

    def test_consumed_review_does_not_block_evaluation(self) -> None:
        safety = {
            "evaluation_gate": {
                "status": "liberada",
                "can_start_evaluation_full_production_now": True,
                "blocking_reasons": [],
            }
        }
        diff_review = {
            "calibration_review": {
                "policy_decision": "required",
                "pending_count": 0,
            }
        }

        backend._apply_pairwise_calibration_gate(safety, diff_review)

        self.assertTrue(safety["evaluation_gate"]["can_start_evaluation_full_production_now"])
        self.assertEqual(safety["evaluation_gate"]["calibration_review_status"], "clear")


class QualityEpochGateTests(unittest.TestCase):
    def test_materialized_baseline_requires_preparation(self) -> None:
        self.assertTrue(backend._evaluation_requires_score_preparation({
            "status": "stale", "invalidation_reason": "baseline_materialized",
        }))
        self.assertFalse(backend._evaluation_requires_score_preparation({
            "status": "stale", "invalidation_reason": "model_changed",
        }))
        self.assertFalse(backend._evaluation_requires_score_preparation({}))

    def test_published_epoch_remains_valid_for_new_evaluation(self) -> None:
        self.assertTrue(backend._quality_epoch_has_scores("published"))

    def test_published_epoch_remains_valid_for_later_apply(self) -> None:
        self.assertTrue(backend._quality_epoch_has_evaluation("published"))

    def test_unscored_epoch_is_still_blocked(self) -> None:
        self.assertFalse(backend._quality_epoch_has_scores("open"))
        self.assertFalse(backend._quality_epoch_has_evaluation("scored"))


if __name__ == "__main__":
    unittest.main()
