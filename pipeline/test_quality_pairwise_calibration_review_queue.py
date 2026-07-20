from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from quality_pairwise_calibration_review_queue import (  # noqa: E402
    RULE_VERSION,
    build_queue_items,
    create_review_queue,
    record_review_decision,
)
from quality_pairwise_calibration_consumer import stable_pair_key  # noqa: E402


def row(segment_id: int, old: float, new: float) -> dict:
    return {
        "evidence_id": segment_id + 1000,
        "segment_id": segment_id,
        "relative_path": f"file_{segment_id}.yml",
        "source_key": f"key_{segment_id}",
        "source_line_number": segment_id,
        "evidence_type": "generic_provider_evidence",
        "baseline_text": f"old {segment_id}",
        "candidate_text": f"new {segment_id}",
        "baseline_score_raw": old,
        "candidate_score_raw": new,
        "baseline_action": "needs_human",
        "candidate_action": "needs_human",
        "confirmation_source": "pairwise_monotonic_repair",
        "confirmation_label": "pairwise_monotonic_preferred",
    }


class PairwiseCalibrationReviewQueueTests(unittest.TestCase):
    def test_queue_is_version_agnostic_and_separates_review_from_controls(self) -> None:
        items = build_queue_items(
            [
                row(1, 0.50, 0.495),
                row(2, 0.50, 0.4995),
                row(3, 0.50, 0.50005),
                row(4, 0.50, 0.56),
                row(5, 0.50, 0.55),
            ],
            control_count=1,
        )

        self.assertNotIn("v4", RULE_VERSION.casefold())
        lanes = [item["review_lane"] for item in items]
        self.assertEqual(lanes.count("priority_review"), 1)
        self.assertEqual(lanes.count("score_review"), 2)
        self.assertEqual(lanes.count("positive_control"), 1)
        self.assertEqual(lanes.count("negative_control"), 1)
        self.assertEqual(len({item["segment_id"] for item in items}), len(items))
        negative = next(item for item in items if item["review_lane"] == "negative_control")
        self.assertEqual(negative["expected_preference"], "baseline_preferred")
        self.assertEqual(negative["baseline_text"], "new 5")
        self.assertEqual(negative["candidate_text"], "old 5")
        for control in (item for item in items if item["review_lane"].endswith("control")):
            self.assertEqual(control["holdout_eligible"], 1)
            self.assertEqual(control["training_eligible"], 0)
            self.assertEqual(control["control_blinded"], 1)

    def test_decisions_keep_controls_out_of_training_and_measure_accuracy(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE ml_pairwise_calibration_review_runs (
              id INTEGER PRIMARY KEY, pending_count INTEGER, decided_count INTEGER,
              controls_passed_count INTEGER, control_accuracy REAL, updated_at TEXT
            );
            CREATE TABLE ml_pairwise_calibration_review_items (
              id INTEGER PRIMARY KEY, run_id INTEGER, review_lane TEXT,
              expected_preference TEXT, review_status TEXT, reviewer_label TEXT,
              reviewer_reason TEXT, reviewer TEXT, reviewed_at TEXT,
              training_eligible INTEGER, holdout_eligible INTEGER, updated_at TEXT
            );
            CREATE TABLE ml_pairwise_calibration_review_decisions (
              id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, item_id INTEGER,
              review_label TEXT, review_reason TEXT, reviewer TEXT,
              control_passed INTEGER, created_at TEXT
            );
            INSERT INTO ml_pairwise_calibration_review_runs VALUES (1, 2, 0, 0, NULL, 'now');
            INSERT INTO ml_pairwise_calibration_review_items VALUES
              (1, 1, 'negative_control', 'baseline_preferred', 'pending', NULL, NULL, NULL, NULL, 0, 1, 'now'),
              (2, 1, 'priority_review', 'candidate_preferred', 'pending', NULL, NULL, NULL, NULL, 0, 0, 'now');
            """
        )

        control = record_review_decision(
            conn,
            item_id=1,
            review_label="baseline_preferred",
            review_reason=None,
            reviewer="test",
        )
        self.assertEqual(control["control_passed"], 1)
        self.assertEqual(control["training_eligible"], 0)
        self.assertEqual(control["control_accuracy"], 1.0)

        review = record_review_decision(
            conn,
            item_id=2,
            review_label="candidate_preferred",
            review_reason="repair is preferable",
            reviewer="test",
        )
        self.assertEqual(review["training_eligible"], 1)
        self.assertEqual(review["pending_count"], 0)
        conn.close()

    def test_new_queue_inherits_exact_reviews_but_not_controls(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE quality_epochs (
              id INTEGER PRIMARY KEY, old_score_run_id INTEGER, output_score_run_id INTEGER,
              status TEXT
            );
            CREATE TABLE ml_pairwise_quality_evidence (
              id INTEGER PRIMARY KEY, segment_id INTEGER, relative_path TEXT, source_key TEXT,
              evidence_type TEXT, baseline_text TEXT, candidate_text TEXT,
              token_integrity_ok INTEGER, post_validation_clean INTEGER,
              source_metadata_json TEXT
            );
            CREATE TABLE ml_score_items (
              run_id INTEGER, segment_id INTEGER, candidate_text TEXT,
              model_safe_probability REAL, final_action TEXT
            );
            CREATE TABLE output_segments (segment_id INTEGER, portuguese_text TEXT);
            CREATE TABLE source_segments (
              id INTEGER PRIMARY KEY, source_line_number INTEGER, is_active INTEGER
            );
            CREATE TABLE segment_confirmations (
              segment_id INTEGER, confirmed_text TEXT, confirmation_source TEXT,
              confirmation_label TEXT
            );
            CREATE TABLE ml_pairwise_calibration_review_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT, queue_key TEXT UNIQUE, rule_version TEXT,
              quality_epoch_id INTEGER, old_score_run_id INTEGER, output_score_run_id INTEGER,
              technical_tie_epsilon REAL, priority_delta_threshold REAL, control_count INTEGER,
              candidate_count INTEGER, review_count INTEGER, priority_count INTEGER,
              positive_control_count INTEGER, negative_control_count INTEGER,
              pending_count INTEGER, decided_count INTEGER, controls_passed_count INTEGER DEFAULT 0,
              control_accuracy REAL, consumption_status TEXT DEFAULT 'pending',
              consumption_rule_version TEXT, consumed_count INTEGER DEFAULT 0,
              inherited_count INTEGER DEFAULT 0, consumed_at TEXT,
              consumption_summary_json TEXT, report_path TEXT, decisions_template_path TEXT,
              started_at TEXT, finished_at TEXT, updated_at TEXT
            );
            CREATE TABLE ml_pairwise_calibration_review_items (
              id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, evidence_id INTEGER,
              segment_id INTEGER, relative_path TEXT, source_key TEXT, source_line_number INTEGER,
              review_lane TEXT, display_order INTEGER, baseline_text TEXT, candidate_text TEXT,
              baseline_score_raw REAL, candidate_score_raw REAL, raw_delta REAL,
              score_outcome TEXT, expected_preference TEXT, review_status TEXT,
              reviewer_label TEXT, reviewer_reason TEXT, reviewer TEXT, reviewed_at TEXT,
              training_eligible INTEGER, holdout_eligible INTEGER, control_blinded INTEGER,
              pair_hash TEXT, stable_pair_key TEXT, metadata_json TEXT,
              created_at TEXT, updated_at TEXT
            );
            CREATE TABLE ml_pairwise_calibration_review_decisions (
              id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, item_id INTEGER,
              review_label TEXT, review_reason TEXT, reviewer TEXT,
              control_passed INTEGER, created_at TEXT
            );
            CREATE TABLE ml_pairwise_calibration_labels (
              id INTEGER PRIMARY KEY AUTOINCREMENT, pair_key TEXT UNIQUE,
              segment_id INTEGER, evidence_id INTEGER, evidence_type TEXT,
              baseline_hash TEXT, candidate_hash TEXT, baseline_text TEXT, candidate_text TEXT,
              review_label TEXT, review_reason TEXT, reviewer TEXT, training_eligible INTEGER,
              source_run_id INTEGER, source_item_id INTEGER, control_accuracy REAL,
              created_at TEXT, updated_at TEXT
            );
            INSERT INTO quality_epochs VALUES (4, 10, 11, 'scored');
            INSERT INTO source_segments VALUES (1, 1, 1), (2, 2, 1), (3, 3, 1);
            INSERT INTO ml_pairwise_quality_evidence VALUES
              (101, 1, 'a.yml', 'a', 'generic_provider_evidence', 'old 1', 'new 1', 1, 1, '{}'),
              (102, 2, 'b.yml', 'b', 'generic_provider_evidence', 'old 2', 'new 2', 1, 1, '{}'),
              (103, 3, 'c.yml', 'c', 'generic_provider_evidence', 'old 3', 'new 3', 1, 1, '{}');
            INSERT INTO ml_score_items VALUES
              (10, 1, 'old 1', 0.50, 'needs_human'), (11, 1, 'new 1', 0.49, 'needs_human'),
              (10, 2, 'old 2', 0.50, 'needs_human'), (11, 2, 'new 2', 0.56, 'needs_human'),
              (10, 3, 'old 3', 0.50, 'needs_human'), (11, 3, 'new 3', 0.55, 'needs_human');
            INSERT INTO output_segments VALUES (1, 'new 1'), (2, 'new 2'), (3, 'new 3');
            INSERT INTO segment_confirmations VALUES
              (1, 'new 1', 'pairwise', 'preferred'),
              (2, 'new 2', 'pairwise', 'preferred'),
              (3, 'new 3', 'pairwise', 'preferred');
            """
        )
        pair_key = stable_pair_key(
            segment_id=1,
            evidence_type="generic_provider_evidence",
            baseline_text="old 1",
            candidate_text="new 1",
        )
        conn.execute(
            """
            INSERT INTO ml_pairwise_calibration_labels (
              pair_key, segment_id, evidence_id, evidence_type, baseline_hash, candidate_hash,
              baseline_text, candidate_text, review_label, reviewer, training_eligible,
              source_run_id, source_item_id, created_at, updated_at
            ) VALUES (?, 1, 101, 'generic_provider_evidence', 'old', 'new', 'old 1', 'new 1',
                      'candidate_preferred', 'human', 1, 1, 1, 'before', 'before')
            """,
            (pair_key,),
        )
        with tempfile.TemporaryDirectory() as reports_dir:
            payload = create_review_queue(
                conn,
                {"reports_dir": reports_dir},
                apply=True,
                control_count=1,
            )

        self.assertEqual(payload["inherited_count"], 1)
        self.assertEqual(payload["pending_count"], 2)
        inherited = conn.execute(
            "SELECT * FROM ml_pairwise_calibration_review_items WHERE control_blinded = 0"
        ).fetchone()
        self.assertEqual(inherited["review_status"], "decided")
        self.assertEqual(inherited["reviewer_label"], "candidate_preferred")
        controls = conn.execute(
            "SELECT COUNT(*) FROM ml_pairwise_calibration_review_items WHERE control_blinded = 1 AND review_status = 'pending'"
        ).fetchone()[0]
        self.assertEqual(controls, 2)
        conn.close()


if __name__ == "__main__":
    unittest.main()
