from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from quality_pairwise_calibration_consumer import (  # noqa: E402
    consume_review_run,
    load_labels_by_pair_keys,
    stable_pair_key,
)


def database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE ml_pairwise_calibration_review_runs (
          id INTEGER PRIMARY KEY, quality_epoch_id INTEGER, pending_count INTEGER,
          consumption_status TEXT, consumption_rule_version TEXT, consumed_count INTEGER,
          inherited_count INTEGER, consumed_at TEXT, consumption_summary_json TEXT,
          updated_at TEXT
        );
        CREATE TABLE ml_pairwise_quality_evidence (
          id INTEGER PRIMARY KEY, evidence_type TEXT, baseline_hash TEXT, candidate_hash TEXT,
          source_metadata_json TEXT, preference_label TEXT, training_target TEXT,
          training_eligible INTEGER, evidence_weight REAL, baseline_score_raw REAL,
          candidate_score_raw REAL, pairwise_score REAL, pairwise_delta REAL,
          promotion_eligible INTEGER, auto_safe_eligible INTEGER, last_seen_at TEXT
        );
        CREATE TABLE ml_pairwise_calibration_review_items (
          id INTEGER PRIMARY KEY, run_id INTEGER, evidence_id INTEGER, segment_id INTEGER,
          review_lane TEXT, display_order INTEGER, baseline_text TEXT, candidate_text TEXT,
          expected_preference TEXT, review_status TEXT, reviewer_label TEXT,
          reviewer_reason TEXT, reviewer TEXT, control_blinded INTEGER,
          stable_pair_key TEXT, updated_at TEXT
        );
        CREATE TABLE ml_pairwise_calibration_labels (
          id INTEGER PRIMARY KEY AUTOINCREMENT, pair_key TEXT NOT NULL UNIQUE,
          segment_id INTEGER, evidence_id INTEGER, evidence_type TEXT,
          baseline_hash TEXT, candidate_hash TEXT, baseline_text TEXT, candidate_text TEXT,
          review_label TEXT, review_reason TEXT, reviewer TEXT, training_eligible INTEGER,
          source_run_id INTEGER, source_item_id INTEGER, control_accuracy REAL,
          created_at TEXT, updated_at TEXT
        );
        INSERT INTO ml_pairwise_calibration_review_runs VALUES
          (1, 4, 0, 'pending', NULL, 0, 0, NULL, NULL, 'now');
        INSERT INTO ml_pairwise_quality_evidence VALUES
          (10, 'token_punctuation', 'old-hash', 'new-hash', '{}',
           'candidate_preferred', 'pairwise_preference_only', 1, 1.0,
           0.40, 0.39, 0.42, 0.02, 0, 0, 'now');
        INSERT INTO ml_pairwise_calibration_review_items VALUES
          (100, 1, 10, 7, 'priority_review', 1, 'old ] , text', 'new ], text',
           'candidate_preferred', 'decided', 'candidate_preferred', 'clean repair', 'human', 0, NULL, 'now'),
          (101, 1, 10, 8, 'positive_control', 2, 'old A', 'new A',
           'candidate_preferred', 'decided', 'candidate_preferred', NULL, 'human', 1, NULL, 'now'),
          (102, 1, 10, 9, 'negative_control', 3, 'new B', 'old B',
           'baseline_preferred', 'decided', 'baseline_preferred', NULL, 'human', 1, NULL, 'now');
        """
    )
    return conn


class PairwiseCalibrationConsumerTests(unittest.TestCase):
    def test_completed_review_is_consumed_into_ledger_and_evidence(self) -> None:
        conn = database()
        payload = consume_review_run(conn, run_id=1, apply=True)

        self.assertEqual(payload["status"], "consumed")
        self.assertEqual(payload["control_accuracy"], 1.0)
        self.assertEqual(payload["consumed_count"], 1)
        run = conn.execute(
            "SELECT consumption_status, consumed_count FROM ml_pairwise_calibration_review_runs WHERE id = 1"
        ).fetchone()
        self.assertEqual(run["consumption_status"], "consumed")
        self.assertEqual(run["consumed_count"], 1)
        evidence = conn.execute(
            "SELECT preference_label, training_target, training_eligible, evidence_weight, pairwise_score FROM ml_pairwise_quality_evidence WHERE id = 10"
        ).fetchone()
        self.assertEqual(evidence["preference_label"], "candidate_preferred")
        self.assertEqual(evidence["training_target"], "human_pairwise_preference")
        self.assertEqual(evidence["training_eligible"], 1)
        self.assertEqual(evidence["evidence_weight"], 2.0)
        self.assertEqual(evidence["pairwise_score"], 0.42)
        label = conn.execute("SELECT * FROM ml_pairwise_calibration_labels").fetchone()
        self.assertEqual(label["review_label"], "candidate_preferred")
        self.assertEqual(label["control_accuracy"], 1.0)
        self.assertTrue(label["pair_key"])
        conn.close()

    def test_low_control_accuracy_blocks_consumption(self) -> None:
        conn = database()
        conn.execute(
            "UPDATE ml_pairwise_calibration_review_items SET reviewer_label = 'baseline_preferred' WHERE id = 101"
        )
        payload = consume_review_run(conn, run_id=1, apply=True)

        self.assertEqual(payload["status"], "blocked_control_accuracy")
        self.assertEqual(payload["control_accuracy"], 0.5)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM ml_pairwise_calibration_labels").fetchone()[0],
            0,
        )
        conn.close()

    def test_stable_pair_key_can_recover_an_exact_decision(self) -> None:
        conn = database()
        consume_review_run(conn, run_id=1, apply=True)
        key = stable_pair_key(
            segment_id=7,
            evidence_type="token_punctuation",
            baseline_text="old ] , text",
            candidate_text="new ], text",
        )
        labels = load_labels_by_pair_keys(conn, [key])
        self.assertEqual(labels[key]["review_label"], "candidate_preferred")
        self.assertEqual(
            key,
            stable_pair_key(
                segment_id=7,
                evidence_type="token_punctuation",
                baseline_text="old ] , text",
                candidate_text="new ], text",
            ),
        )
        conn.close()


if __name__ == "__main__":
    unittest.main()
