from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard import backend


class ProductionRuntimeStateTest(unittest.TestCase):
    def test_live_run_overlays_stale_dashboard_cache_without_mutating_it(self) -> None:
        cached_app_state = {
            "cache": {"generated_at": "old"},
            "production": {
                "active": False,
                "last_run": {"run_id": "old-run", "status": "completed"},
                "progress_pct": 100,
            },
        }
        cache = {
            "generated_at": "2026-07-18T16:00:00",
            "source_db_mtime": 10.0,
            "app_state": cached_app_state,
        }
        live_run = {
            "run_id": "live-run",
            "mode": "evaluation",
            "status": "running",
            "current_stage": "preflight_sync",
            "stages": [
                {"id": "snapshot", "status": "done", "progress_pct": 100},
                {"id": "preflight_sync", "status": "running", "progress_pct": 40},
            ],
        }

        with (
            patch.object(backend, "_get_dashboard_cache", return_value=cache),
            patch.object(backend, "_read_production_run_status", return_value=live_run),
            patch.object(backend, "_db_mtime", return_value=10.0),
        ):
            payload = backend._app_state_payload(Path("fake.sqlite"))

        self.assertTrue(payload["production"]["active"])
        self.assertEqual(payload["production"]["last_run"]["run_id"], "live-run")
        self.assertEqual(payload["production"]["current_stage"], "preflight_sync")
        self.assertEqual(payload["production"]["progress_pct"], 70)
        self.assertEqual(payload["production"]["stages_compact"][1]["status"], "running")

        self.assertEqual(cached_app_state["production"]["last_run"]["run_id"], "old-run")
        self.assertFalse(cached_app_state["production"]["active"])

    def test_completed_run_always_reports_full_progress(self) -> None:
        run = {"status": "completed"}
        stages = [{"id": "snapshot", "status": "done", "progress_pct": 100}]

        self.assertEqual(backend._production_run_progress(run, stages), 100)


if __name__ == "__main__":
    unittest.main()
