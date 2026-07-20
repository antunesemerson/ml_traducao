from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

import quality_shadow_store  # noqa: E402


class QualityShadowStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE source_segments (id INTEGER PRIMARY KEY);
            CREATE TABLE ml_score_runs (id INTEGER PRIMARY KEY);
            CREATE TABLE ml_quality_shadow_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              snapshot_key TEXT NOT NULL UNIQUE,
              source_rule_version TEXT NOT NULL,
              score_run_id INTEGER NOT NULL,
              record_count INTEGER NOT NULL,
              eligible_count INTEGER NOT NULL,
              status TEXT NOT NULL,
              metadata_json TEXT NOT NULL,
              started_at TEXT NOT NULL,
              finished_at TEXT,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE ml_quality_shadow_items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id INTEGER NOT NULL,
              segment_id INTEGER NOT NULL,
              lane TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(run_id, segment_id)
            );
            INSERT INTO source_segments VALUES (1), (2);
            INSERT INTO ml_score_runs VALUES (10);
            """
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_exact_handoff_is_loaded_from_database_and_is_idempotent(self) -> None:
        records = [
            {
                "source": "shadow-v1",
                "score_run_id": 10,
                "segment_id": 1,
                "lane": "eligible",
                "candidate_hash": "a",
            },
            {
                "source": "shadow-v1",
                "score_run_id": 10,
                "segment_id": 2,
                "lane": "blocked",
                "candidate_hash": "b",
            },
        ]

        first = quality_shadow_store.persist_snapshot(
            self.conn,
            source_rule_version="shadow-v1",
            score_run_id=10,
            records=records,
            eligible_lane="eligible",
        )
        second = quality_shadow_store.persist_snapshot(
            self.conn,
            source_rule_version="shadow-v1",
            score_run_id=10,
            records=records,
            eligible_lane="eligible",
        )
        run, eligible = quality_shadow_store.load_snapshot(
            self.conn,
            source_rule_version="shadow-v1",
            run_id=first["shadow_run_id"],
            eligible_lane="eligible",
        )

        self.assertEqual(first["shadow_run_id"], second["shadow_run_id"])
        self.assertEqual(run["record_count"], 2)
        self.assertEqual([item["segment_id"] for item in eligible], [1])


if __name__ == "__main__":
    unittest.main()
