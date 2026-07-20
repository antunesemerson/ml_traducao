from __future__ import annotations

import sqlite3
import sys
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock


PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

import segment_state_snapshot as snapshot  # noqa: E402


def make_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE segment_state_runs (
            id INTEGER PRIMARY KEY,
            total_segments INTEGER NOT NULL,
            notes_json TEXT,
            finished_at TEXT
        );
        CREATE TABLE segment_state_items (
            run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            final_state TEXT NOT NULL
        );
        CREATE TABLE output_segments (
            segment_id INTEGER PRIMARY KEY,
            portuguese_text TEXT
        );
        """
    )
    return conn


class PreviousLifecycleReviewRowsTests(unittest.TestCase):
    def test_recovers_evidence_from_latest_completed_database_snapshot(self) -> None:
        conn = make_connection()
        self.addCleanup(conn.close)
        conn.executemany(
            "INSERT INTO segment_state_runs (id, total_segments, finished_at) VALUES (?, ?, ?)",
            [(1, 288100, "2026-07-18T10:00:00Z"), (2, 288100, None)],
        )
        conn.executemany(
            """
            INSERT INTO segment_state_items (
                run_id, segment_id, relative_path, source_key, final_state
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (1, 10, "events/test_l_spanish.yml", "test.key", "legacy_closed"),
                (2, 20, "events/unfinished_l_spanish.yml", "unfinished.key", "legacy_closed"),
            ],
        )
        conn.executemany(
            "INSERT INTO output_segments (segment_id, portuguese_text) VALUES (?, ?)",
            [(10, "Texto atual"), (20, "Não deve ser usado")],
        )

        rows = snapshot.previous_lifecycle_review_rows(
            conn,
            {"legacy_closed": {"decision": "reviewed_in_database", "queue_run_id": 99}},
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["segment_id"], 10)
        self.assertEqual(rows[0]["segment_state_run_id"], 1)
        self.assertEqual(rows[0]["current_text"], "Texto atual")
        self.assertEqual(rows[0]["decision"], "reviewed_in_database")
        self.assertEqual(rows[0]["queue_run_id"], 99)
        self.assertTrue(rows[0]["tokens_preserved"])
        self.assertFalse(rows[0]["requires_apply_later"])

    def test_returns_empty_evidence_without_a_completed_snapshot(self) -> None:
        conn = make_connection()
        self.addCleanup(conn.close)
        conn.execute(
            "INSERT INTO segment_state_runs (id, total_segments, finished_at) VALUES (1, 288100, NULL)"
        )

        rows = snapshot.previous_lifecycle_review_rows(
            conn,
            {"legacy_closed": {"decision": "reviewed_in_database"}},
        )

        self.assertEqual(rows, [])


class AuxiliaryReportIndependenceTests(unittest.TestCase):
    def test_report_is_skipped_without_executing_expensive_builder(self) -> None:
        counters = {"state_group": Counter({"closed": 3})}
        with mock.patch.object(snapshot, "build_report") as build_report:
            lines, status = snapshot.build_auxiliary_report(
                None,
                12,
                counters,
                {},
                "start",
                "finish",
                enabled=False,
            )

        build_report.assert_not_called()
        self.assertEqual(status["status"], "skipped")
        self.assertTrue(any("database state is authoritative" in line for line in lines))

    def test_report_failure_does_not_invalidate_committed_snapshot(self) -> None:
        counters = {"state_group": Counter({"closed": 3})}
        with mock.patch.object(
            snapshot,
            "build_report",
            side_effect=OSError("reports unavailable"),
        ):
            lines, status = snapshot.build_auxiliary_report(
                None,
                12,
                counters,
                {},
                "start",
                "finish",
                enabled=True,
            )

        self.assertEqual(status["status"], "failed")
        self.assertIn("reports unavailable", status["error"])
        self.assertTrue(any("without invalidating snapshot" in line for line in lines))

    def test_operational_metadata_preserves_existing_notes(self) -> None:
        conn = make_connection()
        self.addCleanup(conn.close)
        conn.execute(
            """
            INSERT INTO segment_state_runs (id, total_segments, notes_json, finished_at)
            VALUES (7, 3, '{"purpose":"fixture"}', '2026-07-20T00:00:00Z')
            """
        )

        snapshot.update_run_operational_metadata(
            conn,
            7,
            phase_timings_seconds={"critical_total": 1.23456},
            report_status="skipped",
        )

        row = conn.execute("SELECT notes_json FROM segment_state_runs WHERE id = 7").fetchone()
        notes = snapshot.json.loads(row["notes_json"])
        self.assertEqual(notes["purpose"], "fixture")
        self.assertEqual(notes["phase_timings_seconds"]["critical_total"], 1.235)
        self.assertEqual(notes["auxiliary_report"]["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
