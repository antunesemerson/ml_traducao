from __future__ import annotations

import sqlite3
import unittest

from dashboard import backend


class SourceUpdateReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(
            """
            CREATE TABLE source_tree_snapshots (
                id INTEGER PRIMARY KEY,
                snapshot_label TEXT,
                game_version TEXT,
                english_tree_hash TEXT,
                spanish_tree_hash TEXT,
                english_file_count INTEGER,
                spanish_file_count INTEGER,
                english_total_bytes INTEGER,
                spanish_total_bytes INTEGER,
                metadata_json TEXT,
                created_at TEXT
            );
            CREATE TABLE source_tree_snapshot_files (
                snapshot_id INTEGER,
                source_kind TEXT,
                relative_path TEXT,
                file_hash TEXT,
                size_bytes INTEGER,
                source_mtime_ns INTEGER
            );
            CREATE TABLE source_segments (
                id INTEGER PRIMARY KEY,
                relative_path TEXT,
                is_active INTEGER
            );
            CREATE TABLE quality_epochs (
                id INTEGER PRIMARY KEY,
                status TEXT,
                source_snapshot_id INTEGER,
                segment_state_run_id INTEGER,
                evaluated_at TEXT,
                published_at TEXT,
                updated_at TEXT
            );
            """
        )

    def tearDown(self) -> None:
        self.con.close()

    def test_classifies_new_changed_removed_and_preserved_source_lanes(self) -> None:
        self.con.executemany(
            """
            INSERT INTO source_tree_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)
            """,
            (
                (1, "before", "1.0", "en-1", "es-1", 0, 3, 0, 30, "2026-08-01"),
                (2, "after", "1.1", "en-1", "es-2", 0, 3, 0, 35, "2026-09-01"),
            ),
        )
        self.con.executemany(
            """
            INSERT INTO source_tree_snapshot_files VALUES (?, 'spanish', ?, ?, 10, 1)
            """,
            (
                (1, "preserved.yml", "same"),
                (1, "changed.yml", "before"),
                (1, "removed.yml", "removed"),
                (2, "preserved.yml", "same"),
                (2, "changed.yml", "after"),
                (2, "new.yml", "new"),
            ),
        )
        segment_rows = []
        segment_id = 1
        for path, count, active in (
            ("preserved.yml", 2, 1),
            ("changed.yml", 3, 1),
            ("new.yml", 1, 1),
            ("removed.yml", 2, 0),
        ):
            for _ in range(count):
                segment_rows.append((segment_id, path, active))
                segment_id += 1
        self.con.executemany(
            "INSERT INTO source_segments VALUES (?, ?, ?)",
            segment_rows,
        )

        payload = backend._source_update_readiness_payload(self.con)

        self.assertEqual(payload["status"], "change_pending_evaluation")
        self.assertTrue(payload["can_prepare_incremental_cycle"])
        self.assertTrue(payload["pending_source_evaluation"])
        self.assertTrue(payload["gates"]["full_evaluation_required_before_publish"])
        self.assertEqual(payload["file_delta"]["spanish"]["new_count"], 1)
        self.assertEqual(payload["file_delta"]["spanish"]["changed_count"], 1)
        self.assertEqual(payload["file_delta"]["spanish"]["removed_count"], 1)
        self.assertEqual(
            payload["segment_delta"],
            {
                "contract": "file_exact_segment_affected_v1",
                "new": 1,
                "changed": 3,
                "removed": 2,
                "preserved": 2,
                "active_total": 6,
                "note": "Arquivos são comparados por hash exato; segmentos alterados representam o conjunto afetado nos arquivos espanhóis modificados.",
            },
        )

    def test_single_snapshot_is_a_stable_baseline_not_an_update(self) -> None:
        self.con.execute(
            """
            INSERT INTO source_tree_snapshots VALUES
            (1, 'baseline', '1.0', 'en', 'es', 0, 0, 0, 0, '{}', '2026-08-01')
            """
        )

        payload = backend._source_update_readiness_payload(self.con)

        self.assertEqual(payload["status"], "baseline_ready")
        self.assertFalse(payload["has_delta"])
        self.assertFalse(payload["can_prepare_incremental_cycle"])

    def test_changed_snapshot_stops_blocking_after_its_epoch_is_evaluated(self) -> None:
        self.con.executemany(
            """
            INSERT INTO source_tree_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)
            """,
            (
                (1, "before", "1.0", "en-1", "es-1", 0, 1, 0, 10, "2026-08-01"),
                (2, "after", "1.1", "en-1", "es-2", 0, 1, 0, 12, "2026-09-01"),
            ),
        )
        self.con.executemany(
            "INSERT INTO source_tree_snapshot_files VALUES (?, 'spanish', 'changed.yml', ?, 10, 1)",
            ((1, "before"), (2, "after")),
        )
        self.con.execute("INSERT INTO source_segments VALUES (1, 'changed.yml', 1)")
        self.con.execute(
            """
            INSERT INTO quality_epochs (
                id, status, source_snapshot_id, segment_state_run_id,
                evaluated_at, published_at, updated_at
            ) VALUES (7, 'evaluated', 2, 42, '2026-09-01', NULL, '2026-09-01')
            """
        )

        payload = backend._source_update_readiness_payload(self.con)

        self.assertEqual(payload["status"], "change_evaluated")
        self.assertTrue(payload["has_delta"])
        self.assertFalse(payload["pending_source_evaluation"])
        self.assertTrue(payload["current_snapshot_evaluated"])
        self.assertFalse(payload["gates"]["full_evaluation_required_before_publish"])


if __name__ == "__main__":
    unittest.main()
