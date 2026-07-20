from __future__ import annotations

import unittest

from dashboard import backend


class PairwiseCalibrationGateTests(unittest.TestCase):
    def test_required_pending_review_blocks_evaluation(self) -> None:
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
        self.assertFalse(gate["can_start_evaluation_full_production_now"])
        self.assertEqual(gate["calibration_pending_count"], 16)
        self.assertIn("pairwise_calibration_review_pending", gate["blocking_reasons"])

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


if __name__ == "__main__":
    unittest.main()
