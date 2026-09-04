from __future__ import annotations

import sqlite3
import unittest

from dashboard import backend


class PairwiseCalibrationPayloadTest(unittest.TestCase):
    def test_skip_policy_preserves_completed_run_from_same_epoch(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE ml_pairwise_calibration_policy_decisions (
                id INTEGER PRIMARY KEY,
                quality_epoch_id INTEGER,
                old_score_run_id INTEGER,
                output_score_run_id INTEGER,
                decision TEXT,
                recommended_control_count INTEGER,
                reasons_json TEXT,
                metrics_json TEXT
            );
            CREATE TABLE ml_pairwise_calibration_review_runs (
                id INTEGER PRIMARY KEY,
                quality_epoch_id INTEGER,
                old_score_run_id INTEGER,
                output_score_run_id INTEGER,
                pending_count INTEGER,
                decided_count INTEGER,
                review_count INTEGER,
                priority_count INTEGER,
                positive_control_count INTEGER,
                negative_control_count INTEGER,
                control_accuracy REAL,
                consumption_status TEXT,
                consumed_count INTEGER,
                inherited_count INTEGER,
                consumed_at TEXT
            );
            CREATE TABLE ml_pairwise_calibration_review_items (
                id INTEGER PRIMARY KEY,
                run_id INTEGER,
                evidence_id INTEGER,
                segment_id INTEGER,
                relative_path TEXT,
                source_key TEXT,
                source_line_number INTEGER,
                review_lane TEXT,
                display_order INTEGER,
                baseline_text TEXT,
                candidate_text TEXT,
                baseline_score_raw REAL,
                candidate_score_raw REAL,
                raw_delta REAL,
                score_outcome TEXT,
                review_status TEXT,
                reviewer_label TEXT,
                reviewer_reason TEXT,
                reviewer TEXT,
                reviewed_at TEXT,
                training_eligible INTEGER,
                holdout_eligible INTEGER,
                control_blinded INTEGER,
                pair_hash TEXT
            );
            CREATE TABLE source_segments (
                id INTEGER PRIMARY KEY,
                english_text TEXT,
                spanish_text TEXT
            );
            INSERT INTO ml_pairwise_calibration_policy_decisions VALUES
                (2, 9, 377, 378, 'skip', 0,
                 '["already_calibrated_current_epoch"]', '{}');
            INSERT INTO ml_pairwise_calibration_review_runs VALUES
                (6, 9, 377, 378, 0, 1, 1, 1, 0, 0, 1.0,
                 'consumed', 1, 0, '2026-07-19T14:00:00Z');
            INSERT INTO ml_pairwise_calibration_review_items VALUES
                (101, 6, 7, 42, 'example.yml', 'key', 3, 'priority_review', 1,
                 'texto A', 'texto B', 0.7, 0.698, -0.002,
                 'raw_non_improving', 'decided', 'candidate_preferred', NULL,
                 'tester', '2026-07-19T13:50:00Z', 1, 0, 0, 'pair');
            INSERT INTO source_segments VALUES
                (42, 'English reference', 'Referencia española');
            """
        )

        payload = backend._pairwise_calibration_review_payload(conn)

        self.assertEqual(payload["policy_decision"], "skip")
        self.assertEqual(payload["run_id"], 6)
        self.assertEqual(payload["consumption_status"], "consumed")
        self.assertEqual(payload["decided_count"], 1)
        self.assertEqual(payload["items"][0]["segment_id"], 42)
        self.assertEqual(payload["items"][0]["review_status"], "decided")
        self.assertEqual(payload["items"][0]["english_text"], "English reference")
        self.assertEqual(payload["items"][0]["spanish_text"], "Referencia española")


if __name__ == "__main__":
    unittest.main()
