from __future__ import annotations

import unittest
import sys
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from quality_calibration_policy import (  # noqa: E402
    CalibrationPolicyConfig,
    decide_calibration,
)


def metrics(**overrides):
    base = {
        "quality_epoch_id": 10,
        "population_count": 4,
        "candidate_count": 4,
        "invalid_integrity_count": 0,
        "non_improving_count": 0,
        "score_contract_changed": False,
        "large_batch_threshold": 250,
        "immature_provider_ids": [],
        "last_control_accuracy": 1.0,
        "low_confidence_count": 0,
        "scored_epochs_since_calibration": 1,
        "existing_pending_count": 0,
        "current_epoch_calibrated": False,
    }
    return {**base, **overrides}


class CalibrationPolicyDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = CalibrationPolicyConfig()

    def test_skips_when_there_are_no_applied_candidates(self) -> None:
        result = decide_calibration(
            metrics(population_count=0, candidate_count=0),
            self.config,
        )
        self.assertEqual(result["decision"], "skip")
        self.assertEqual(result["reasons"], ["no_applied_pairwise_candidates"])
        self.assertEqual(result["recommended_control_count"], 0)

    def test_requires_review_for_new_provider_or_non_improving_score(self) -> None:
        new_provider = decide_calibration(
            metrics(immature_provider_ids=["new_provider"]),
            self.config,
        )
        self.assertEqual(new_provider["decision"], "required")
        self.assertIn("immature_provider", new_provider["reasons"])

        regression = decide_calibration(
            metrics(non_improving_count=1),
            self.config,
        )
        self.assertEqual(regression["decision"], "required")
        self.assertIn("raw_non_improving", regression["reasons"])

    def test_existing_pending_queue_is_never_hidden(self) -> None:
        result = decide_calibration(
            metrics(existing_pending_count=3),
            self.config,
        )
        self.assertEqual(result["decision"], "required")
        self.assertIn("existing_pending_review", result["reasons"])

    def test_completed_current_epoch_is_not_reopened(self) -> None:
        result = decide_calibration(
            metrics(
                current_epoch_calibrated=True,
                immature_provider_ids=["new_provider"],
                low_confidence_count=4,
            ),
            self.config,
        )
        self.assertEqual(result["decision"], "skip")
        self.assertEqual(result["reasons"], ["already_calibrated_current_epoch"])

    def test_uses_sample_for_isolated_low_confidence(self) -> None:
        result = decide_calibration(
            metrics(low_confidence_count=1),
            self.config,
        )
        self.assertEqual(result["decision"], "sample")
        self.assertEqual(result["reasons"], ["low_confidence_sample"])
        self.assertEqual(result["recommended_control_count"], 2)

    def test_skips_small_stable_batch_from_mature_provider(self) -> None:
        result = decide_calibration(metrics(), self.config)
        self.assertEqual(result["decision"], "skip")
        self.assertEqual(result["reasons"], ["stable_mature_small_batch"])

    def test_requires_large_or_unsafe_batch(self) -> None:
        result = decide_calibration(
            metrics(
                population_count=300,
                candidate_count=299,
                invalid_integrity_count=1,
            ),
            self.config,
        )
        self.assertEqual(result["decision"], "required")
        self.assertIn("invalid_integrity", result["reasons"])
        self.assertIn("large_batch", result["reasons"])
        self.assertEqual(result["recommended_control_count"], 8)


if __name__ == "__main__":
    unittest.main()
