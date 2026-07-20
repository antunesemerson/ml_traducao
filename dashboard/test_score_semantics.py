from __future__ import annotations

import sqlite3
import unittest

from dashboard import backend


class ScoreSemanticsPayloadTest(unittest.TestCase):
    def test_risk_classifier_is_not_presented_as_absolute_quality(self) -> None:
        payload = backend._score_semantics_payload(
            {"model_kind": "risk_action_classifier"},
            {"model_kind": "risk_action_classifier"},
            comparable_contract=True,
        )

        self.assertEqual(payload["metric_kind"], "operational_risk_probability")
        self.assertTrue(payload["baseline_sensitive"])
        self.assertTrue(payload["within_epoch_contract_comparable"])
        self.assertFalse(payload["cross_version_comparable"])
        self.assertFalse(payload["absolute_quality_interpretation"])


class MaterializedPairwiseGainPayloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE package_versions (
              id INTEGER PRIMARY KEY,
              version_number INTEGER,
              version_label TEXT,
              measured_score_count INTEGER,
              status TEXT
            );
            CREATE TABLE package_version_changes (
              id INTEGER PRIMARY KEY,
              version_id INTEGER,
              previous_score REAL,
              current_score REAL,
              score_delta REAL
            );
            INSERT INTO package_versions VALUES (5, 5, 'v5', 1000, 'materialized');
            INSERT INTO package_versions VALUES (6, 6, 'v6', 1000, 'materialized');
            INSERT INTO package_version_changes VALUES (1, 6, 0.20, 0.25, 0.05);
            INSERT INTO package_version_changes VALUES (2, 6, 0.30, 0.32, 0.02);
            INSERT INTO package_version_changes VALUES (3, 6, 0.40, 0.39, -0.01);
            """
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_latest_materialized_version_uses_frozen_pairwise_deltas(self) -> None:
        payload = backend._latest_materialized_pairwise_gain_payload(self.conn)

        self.assertTrue(payload["available"])
        self.assertEqual(payload["version_number"], 6)
        self.assertEqual(payload["paired_count"], 3)
        self.assertEqual(payload["improved_count"], 2)
        self.assertEqual(payload["regressed_count"], 1)
        self.assertAlmostEqual(payload["avg_delta"], 0.02)
        self.assertAlmostEqual(payload["global_equivalent_delta"], 0.00006)


if __name__ == "__main__":
    unittest.main()
