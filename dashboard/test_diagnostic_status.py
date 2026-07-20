from __future__ import annotations

import sqlite3
import tempfile
import sys
import unittest
from pathlib import Path

from dashboard import backend


class DiagnosticStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_status_file = backend.DIAGNOSTIC_RUN_STATUS_FILE
        backend.DIAGNOSTIC_RUN_STATUS_FILE = Path(self.temp_dir.name) / "diagnostic.json"

    def tearDown(self) -> None:
        backend.DIAGNOSTIC_RUN_STATUS_FILE = self.original_status_file
        self.temp_dir.cleanup()

    def test_status_tracks_real_stages_and_completes_all_phases(self) -> None:
        status = backend._new_diagnostic_status()

        backend._diagnostic_start_stage(status, "source_tree_snapshot")
        self.assertEqual(status["current_stage"], "source_tree_snapshot")
        self.assertEqual(status["phases"][0]["status"], "running")

        backend._diagnostic_finish_stage(status, "source_tree_snapshot", exit_code=0)
        backend._diagnostic_start_stage(status, "segment_state")
        backend._diagnostic_finish_stage(status, "segment_state", exit_code=0)
        self.assertEqual(status["phases"][0]["status"], "done")

        backend._diagnostic_start_stage(status, "quality_promotion_discovery")
        backend._diagnostic_finish_stage(status, "quality_promotion_discovery", exit_code=0)
        self.assertEqual(status["phases"][1]["status"], "done")
        self.assertEqual(status["phases"][2]["status"], "done")

        backend._diagnostic_start_stage(status, "quality_epoch_noop_close")
        backend._diagnostic_finish_stage(status, "quality_epoch_noop_close", exit_code=0)
        self.assertEqual(status["phases"][3]["status"], "running")

        backend._diagnostic_start_stage(status, "dashboard_cache")
        backend._diagnostic_finish_stage(status, "dashboard_cache", exit_code=0)

        persisted = backend._read_diagnostic_status()
        self.assertEqual(persisted["run_id"], status["run_id"])
        self.assertEqual(persisted["status"], "completed")
        self.assertEqual(persisted["progress_pct"], 100)
        self.assertTrue(all(phase["status"] == "done" for phase in persisted["phases"]))

    def test_failed_stage_is_persisted_with_error(self) -> None:
        status = backend._new_diagnostic_status()
        backend._diagnostic_start_stage(status, "segment_state")
        backend._diagnostic_finish_stage(
            status,
            "segment_state",
            exit_code=1,
            stderr_tail=["segment-state failed"],
        )

        persisted = backend._read_diagnostic_status()
        self.assertEqual(persisted["status"], "failed")
        self.assertEqual(persisted["phases"][0]["status"], "failed")
        self.assertIn("segment-state failed", persisted["error"])

    def test_running_command_persists_heartbeat_and_incremental_progress(self) -> None:
        status = backend._new_diagnostic_status()
        status["planned_stage_count"] = 1
        backend._diagnostic_start_stage(status, "segment_state")

        process = backend._run_diagnostic_command(
            status,
            "segment_state",
            [sys.executable, "-c", "import time; time.sleep(0.15); print('finished')"],
            timeout=2,
            heartbeat_interval=0.02,
        )

        persisted = backend._read_diagnostic_status()
        stage = persisted["stages"][-1]
        self.assertEqual(process.returncode, 0)
        self.assertIn("finished", process.stdout)
        self.assertTrue(stage["heartbeat_at"])
        self.assertGreater(stage["elapsed_seconds"], 0)
        self.assertGreater(stage["progress_pct"], 1)
        self.assertGreater(persisted["progress_pct"], 1)

    def test_compatible_finished_segment_state_can_resume_failed_diagnostic(self) -> None:
        db_path = Path(self.temp_dir.name) / "resume.sqlite"
        with sqlite3.connect(db_path) as con:
            con.executescript(
                """
                CREATE TABLE quality_epochs (
                    id INTEGER PRIMARY KEY,
                    output_score_run_id INTEGER,
                    policy_run_id INTEGER,
                    segment_state_run_id INTEGER,
                    scored_at TEXT
                );
                CREATE TABLE segment_state_runs (
                    id INTEGER PRIMARY KEY,
                    rule_version TEXT,
                    candidate_score_run_id INTEGER,
                    policy_run_id INTEGER,
                    total_segments INTEGER,
                    closed_count INTEGER,
                    pending_count INTEGER,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE TABLE segment_state_items (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER NOT NULL
                );
                INSERT INTO segment_state_runs VALUES (
                    10, 'segment_state_snapshot_v1', 100, 20,
                    288100, 288100, 0,
                    '2026-07-20T10:00:00Z', '2026-07-20T10:05:00Z'
                );
                INSERT INTO segment_state_runs VALUES (
                    11, 'segment_state_snapshot_v1', 101, 21,
                    288100, 288100, 0,
                    '2026-07-20T11:00:00Z', '2026-07-20T11:05:00Z'
                );
                INSERT INTO segment_state_items VALUES (1, 11);
                INSERT INTO quality_epochs VALUES (
                    3, 101, 21, 10, '2026-07-20T11:00:00Z'
                );
                """
            )
        con.close()

        run_id = backend._resumable_diagnostic_segment_state_run_id(
            db_path,
            {"epoch_id": 3},
        )

        self.assertEqual(run_id, 11)

    def test_attached_segment_state_is_not_resumed_again(self) -> None:
        db_path = Path(self.temp_dir.name) / "attached.sqlite"
        with sqlite3.connect(db_path) as con:
            con.executescript(
                """
                CREATE TABLE quality_epochs (
                    id INTEGER PRIMARY KEY,
                    output_score_run_id INTEGER,
                    policy_run_id INTEGER,
                    segment_state_run_id INTEGER,
                    scored_at TEXT
                );
                CREATE TABLE segment_state_runs (
                    id INTEGER PRIMARY KEY,
                    rule_version TEXT,
                    candidate_score_run_id INTEGER,
                    policy_run_id INTEGER,
                    total_segments INTEGER,
                    closed_count INTEGER,
                    pending_count INTEGER,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE TABLE segment_state_items (id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL);
                INSERT INTO segment_state_runs VALUES (
                    11, 'segment_state_snapshot_v1', 101, 21,
                    288100, 288100, 0,
                    '2026-07-20T11:00:00Z', '2026-07-20T11:05:00Z'
                );
                INSERT INTO segment_state_items VALUES (1, 11);
                INSERT INTO quality_epochs VALUES (
                    3, 101, 21, 11, '2026-07-20T11:00:00Z'
                );
                """
            )
        con.close()

        self.assertIsNone(
            backend._resumable_diagnostic_segment_state_run_id(
                db_path,
                {"epoch_id": 3},
            )
        )

    def test_segment_state_has_dedicated_critical_timeout(self) -> None:
        self.assertEqual(backend._diagnostic_stage_timeout("segment_state"), 1800)
        self.assertEqual(backend._diagnostic_stage_timeout("score_package_output"), 7200)
        self.assertEqual(backend._diagnostic_stage_timeout("quality_epoch_open"), 900)


if __name__ == "__main__":
    unittest.main()
