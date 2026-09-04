from __future__ import annotations

import sqlite3
import unittest

from segment_state_snapshot import latest_quality_epoch_score_runs


class SegmentStateEpochScoreSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE ml_score_runs (
              id INTEGER PRIMARY KEY,
              model_run_id INTEGER,
              rule_version TEXT,
              source_snapshot_id INTEGER,
              candidate_text_source TEXT,
              finished_at TEXT
            );
            CREATE TABLE quality_epochs (
              id INTEGER PRIMARY KEY,
              old_score_run_id INTEGER,
              output_score_run_id INTEGER,
              policy_run_id INTEGER,
              status TEXT,
              updated_at TEXT
            );
            """
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_identical_package_epoch_accepts_shared_output_score_run(self) -> None:
        self.conn.execute(
            """
            INSERT INTO ml_score_runs (
              id, model_run_id, rule_version, source_snapshot_id,
              candidate_text_source, finished_at
            ) VALUES (402, 76, 'score_v3', 1, 'output', '2026-07-25T19:49:41')
            """
        )
        self.conn.execute(
            """
            INSERT INTO quality_epochs (
              id, old_score_run_id, output_score_run_id, policy_run_id,
              status, updated_at
            ) VALUES (27, 402, 402, 101, 'evaluated', '2026-07-25T20:02:45')
            """
        )

        self.assertEqual(
            latest_quality_epoch_score_runs(self.conn),
            (402, 402, 101),
        )

    def test_distinct_package_epoch_still_requires_old_and_output_sources(self) -> None:
        self.conn.executemany(
            """
            INSERT INTO ml_score_runs (
              id, model_run_id, rule_version, source_snapshot_id,
              candidate_text_source, finished_at
            ) VALUES (?, 76, 'score_v3', 1, ?, '2026-07-25T19:49:41')
            """,
            [(410, "old"), (411, "output")],
        )
        self.conn.execute(
            """
            INSERT INTO quality_epochs (
              id, old_score_run_id, output_score_run_id, policy_run_id,
              status, updated_at
            ) VALUES (28, 410, 411, 102, 'scored', '2026-07-25T20:05:00')
            """
        )

        self.assertEqual(
            latest_quality_epoch_score_runs(self.conn),
            (410, 411, 102),
        )


if __name__ == "__main__":
    unittest.main()
