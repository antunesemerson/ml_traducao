from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard import backend


class ProductionRuntimeStateTest(unittest.TestCase):
    def test_snapshot_sources_exclude_temporary_and_derived_artifacts(self) -> None:
        sources = backend._production_snapshot_sources()

        self.assertEqual(list(sources), ["source", "output"])
        self.assertNotIn("reports", sources)
        self.assertNotIn("logs", sources)
        self.assertNotIn("memory_light", sources)
        self.assertNotIn("models", sources)

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


class ProductionStatusSnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "production.sqlite"
        self.con = sqlite3.connect(self.db_path)
        self.con.row_factory = sqlite3.Row
        self.original_cache = backend.PRODUCTION_STATUS_CACHE
        backend.PRODUCTION_STATUS_CACHE = {"key": None, "snapshot": None}
        self.con.executescript(
            """
            CREATE TABLE segment_state_runs (
              id INTEGER PRIMARY KEY,
              rule_version TEXT,
              active_score_run_id INTEGER,
              candidate_score_run_id INTEGER,
              policy_run_id INTEGER,
              total_segments INTEGER,
              closed_count INTEGER,
              pending_count INTEGER,
              output_apply_pending_count INTEGER,
              blank_valid_count INTEGER,
              reopen_count INTEGER,
              finished_at TEXT
            );
            CREATE TABLE segment_state_items (
              id INTEGER PRIMARY KEY,
              run_id INTEGER,
              segment_id INTEGER,
              final_state TEXT,
              state_group TEXT,
              output_state TEXT,
              apply_state TEXT,
              confirmed_matches_output INTEGER,
              needs_output_apply INTEGER
            );
            CREATE TABLE issues (
              id INTEGER PRIMARY KEY,
              segment_id INTEGER,
              severity TEXT
            );
            CREATE TABLE quality_epochs (
              id INTEGER PRIMARY KEY,
              status TEXT,
              segment_state_run_id INTEGER,
              updated_at TEXT
            );
            CREATE TABLE segment_output_apply_runs (
              id INTEGER PRIMARY KEY,
              apply INTEGER,
              candidates_inspected INTEGER,
              applied_count INTEGER,
              files_touched_count INTEGER,
              report_path TEXT,
              started_at TEXT,
              updated_at TEXT
            );
            CREATE TABLE ml_composite_gate_registry (
              gate_key TEXT PRIMARY KEY,
              active_checkpoint_id INTEGER,
              active_guarded_checkpoint_id INTEGER,
              active_overlay_run_id INTEGER,
              active_policy_run_id INTEGER,
              auto_apply_allowed INTEGER,
              updated_at TEXT
            );
            CREATE TABLE ml_composite_guarded_overlay_checkpoints (
              id INTEGER PRIMARY KEY,
              guarded_release_count INTEGER,
              invalid_release_count INTEGER,
              updated_at TEXT
            );
            CREATE TABLE ml_issue_select_cstring_governed_bridge_proposal_items (
              id INTEGER PRIMARY KEY,
              run_id INTEGER,
              segment_id INTEGER
            );
            """
        )
        self._insert_state_run(1, closed=2, pending=2, finished_at="2026-07-22T10:00:00Z")
        self.con.executemany(
            """
            INSERT INTO segment_state_items (
              id, run_id, segment_id, final_state, state_group, output_state,
              apply_state, confirmed_matches_output, needs_output_apply
            ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, 101, "closed_confirmed", "closed", "output_present", "applied", 1, 0),
                (2, 102, "pending_output_missing_real", "pending", "output_missing", "pending", 0, 1),
                (3, 103, "reopen_auto_confirmed_autofix", "closed", "output_present", "applied", 1, 0),
                (4, 104, "pending_observation", "pending", "output_present", "pending", 0, 0),
            ],
        )
        self.con.execute("INSERT INTO issues VALUES (1, 104, 'high')")
        self.con.execute("INSERT INTO quality_epochs VALUES (1, 'evaluated', 1, '2026-07-22T10:01:00Z')")
        self.con.execute(
            "INSERT INTO segment_output_apply_runs VALUES "
            "(7, 1, 4, 2, 1, 'report.txt', '2026-07-22T10:02:00Z', '2026-07-22T10:02:30Z')"
        )
        self.con.execute(
            "INSERT INTO ml_composite_guarded_overlay_checkpoints VALUES "
            "(5, 12, 0, '2026-07-22T10:02:45Z')"
        )
        self.con.execute(
            "INSERT INTO ml_composite_gate_registry VALUES "
            "('segment_token_composite_review_gate', 4, 5, 6, 7, 0, '2026-07-22T10:03:00Z')"
        )
        self.con.commit()

    def tearDown(self) -> None:
        self.con.close()
        backend.PRODUCTION_STATUS_CACHE = self.original_cache
        self.temp_dir.cleanup()

    def _insert_state_run(self, run_id: int, *, closed: int, pending: int, finished_at: str) -> None:
        self.con.execute(
            """
            INSERT INTO segment_state_runs VALUES (
              ?, 'state-v1', 10, 11, 12, 2000, ?, ?, 0, 3, 0, ?
            )
            """,
            (run_id, closed, pending, finished_at),
        )

    def test_compact_snapshot_preserves_operational_production_values(self) -> None:
        snapshot = backend._build_production_status_snapshot(self.con)

        self.assertEqual(snapshot["summary"]["run_id"], 1)
        self.assertEqual(snapshot["taxonomy"]["actionable_pending"], 2)
        self.assertEqual(snapshot["taxonomy"]["model_suspicion_watch"], 1)
        self.assertEqual(snapshot["applied_segments"], 2)
        self.assertEqual(snapshot["last_apply"]["applied_count"], 2)
        self.assertEqual(snapshot["active_gate"]["guarded_release_count"], 12)
        self.assertEqual(snapshot["active_gate"]["invalid_release_count"], 0)

    def test_production_payload_does_not_build_full_lifecycle_or_agent_payloads(self) -> None:
        with (
            patch.object(backend, "_lifecycle_payload", side_effect=AssertionError("full lifecycle used")),
            patch.object(backend, "_agents_payload", side_effect=AssertionError("full agents used")),
            patch.object(backend, "_pending_taxonomy_payload", side_effect=AssertionError("full taxonomy used")),
            patch.object(backend, "_training_lock_payload", return_value={}),
            patch.object(
                backend,
                "_learning_status_payload",
                return_value={"can_start_production": True, "production_safe": True},
            ),
            patch.object(backend, "_read_production_run_status", return_value={}),
        ):
            payload = backend._production_payload(self.con)

        self.assertEqual(payload["summary"]["active_segments"], 2000)
        self.assertEqual(payload["summary"]["actionable_pending"], 2)
        self.assertEqual(payload["summary"]["model_suspicion_watch"], 1)
        self.assertEqual(payload["gate"]["guarded_releases"], 12)

    def test_cache_reuses_snapshot_and_invalidates_on_new_state_epoch(self) -> None:
        with patch.object(
            backend,
            "_build_production_status_snapshot",
            wraps=backend._build_production_status_snapshot,
        ) as builder:
            first = backend._production_status_snapshot(self.con)
            second = backend._production_status_snapshot(self.con)
            self.assertIs(first, second)
            self.assertEqual(builder.call_count, 1)

            self._insert_state_run(2, closed=2000, pending=0, finished_at="2026-07-22T11:00:00Z")
            self.con.execute("INSERT INTO quality_epochs VALUES (2, 'evaluated', 2, '2026-07-22T11:01:00Z')")
            self.con.commit()

            refreshed = backend._production_status_snapshot(self.con)

        self.assertEqual(builder.call_count, 2)
        self.assertEqual(refreshed["summary"]["run_id"], 2)


if __name__ == "__main__":
    unittest.main()
