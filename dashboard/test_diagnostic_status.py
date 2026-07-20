from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
