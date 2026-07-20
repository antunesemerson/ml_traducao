from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

import ml_score_segments  # noqa: E402


class MlScoreResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE ml_score_runs (
              id INTEGER PRIMARY KEY,
              rule_version TEXT,
              model_run_id INTEGER,
              path_filter TEXT,
              limit_count INTEGER,
              source_snapshot_id INTEGER,
              candidate_text_source TEXT,
              candidate_tree_hash TEXT,
              scored_count INTEGER,
              finished_at TEXT
            );
            CREATE TABLE ml_score_items (run_id INTEGER, segment_id INTEGER);
            INSERT INTO ml_score_runs VALUES
              (379, 'ml_score_segments_v1', 76, NULL, NULL, 1,
               'old', 'tree-old', 2, NULL);
            INSERT INTO ml_score_items VALUES (379, 1), (379, 2);
            """
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_resume_requires_exact_contract_and_returns_committed_offset(self) -> None:
        run_id, offset = ml_score_segments.validate_resume_run(
            self.conn,
            379,
            model_run_id=76,
            path_like=None,
            limit=None,
            source_snapshot_id=1,
            candidate_text_source="old",
            candidate_tree_hash="tree-old",
            scope_sql=None,
        )

        self.assertEqual(run_id, 379)
        self.assertEqual(offset, 2)

    def test_resume_rejects_different_model_contract(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "model_run_id"):
            ml_score_segments.validate_resume_run(
                self.conn,
                379,
                model_run_id=358,
                path_like=None,
                limit=None,
                source_snapshot_id=1,
                candidate_text_source="old",
                candidate_tree_hash="tree-old",
                scope_sql=None,
            )


if __name__ == "__main__":
    unittest.main()
