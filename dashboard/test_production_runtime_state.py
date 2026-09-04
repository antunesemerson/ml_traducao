from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard import backend


class ProductionRuntimeStateTest(unittest.TestCase):
    def test_startup_recovery_closes_orphaned_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = Path(temp_dir) / "production_status.json"
            status_path.write_text(
                json.dumps(
                    {
                        "run_id": "old-run",
                        "status": "running",
                        "started_at": "2026-09-01T20:24:20",
                        "updated_at": "2026-09-01T20:28:22",
                        "last_event_at": "2026-09-01T20:28:21",
                        "finished_at": "",
                        "current_stage": "preflight_sync",
                        "current_label": "Sincronizar indice",
                        "stages": [
                            {"id": "snapshot", "status": "done", "progress_pct": 100},
                            {"id": "preflight_sync", "status": "running", "progress_pct": 28},
                            {"id": "production_report", "status": "pending"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(backend, "PRODUCTION_RUN_STATUS_FILE", status_path),
                patch.object(backend, "_now_iso", return_value="2026-09-03T10:55:00"),
            ):
                recovered = backend._recover_orphaned_production_run_on_startup()

            self.assertEqual(recovered["status"], "cancelled")
            self.assertEqual(recovered["finished_at"], "2026-09-03T10:55:00")
            self.assertEqual(recovered["last_worker_event_at"], "2026-09-01T20:28:21")
            self.assertEqual(recovered["stages"][1]["status"], "cancelled")
            self.assertEqual(recovered["stages"][1]["progress_pct"], 28)
            self.assertEqual(recovered["stages"][2]["status"], "pending")
            persisted = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["status"], "cancelled")

    def test_startup_recovery_preserves_terminal_run(self) -> None:
        terminal = {"run_id": "done-run", "status": "completed", "finished_at": "2026-09-01T20:30:00"}
        with (
            patch.object(backend, "_read_production_run_status", return_value=terminal),
            patch.object(backend, "_write_production_run_status") as writer,
        ):
            recovered = backend._recover_orphaned_production_run_on_startup()

        self.assertIs(recovered, terminal)
        writer.assert_not_called()

    def test_production_status_write_retries_atomic_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = Path(temp_dir) / "production_status.json"
            status_path.write_text('{"run_id":"previous"}\n', encoding="utf-8")
            real_replace = os.replace
            attempts = 0

            def flaky_replace(source: str | Path, destination: str | Path) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise PermissionError("temporary OneDrive lock")
                real_replace(source, destination)

            status = {"run_id": "current", "status": "running"}
            with (
                patch.object(backend, "PRODUCTION_RUN_STATUS_FILE", status_path),
                patch.object(backend.os, "replace", side_effect=flaky_replace),
                patch.object(backend.time, "sleep") as sleep_mock,
            ):
                written = backend._write_production_run_status(status)

            self.assertTrue(written)
            self.assertEqual(attempts, 2)
            sleep_mock.assert_called_once()
            persisted = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["run_id"], "current")
            self.assertEqual(persisted["status"], "running")
            self.assertTrue(persisted["updated_at"])
            self.assertEqual(list(Path(temp_dir).glob("*.tmp")), [])

    def test_production_status_write_failure_is_nonfatal_and_preserves_previous_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = Path(temp_dir) / "production_status.json"
            previous = '{"run_id":"previous","status":"completed"}\n'
            status_path.write_text(previous, encoding="utf-8")
            status = {"run_id": "current", "status": "running"}

            with (
                patch.object(backend, "PRODUCTION_RUN_STATUS_FILE", status_path),
                patch.object(backend, "PRODUCTION_STATUS_WRITE_ATTEMPTS", 2),
                patch.object(backend.os, "replace", side_effect=PermissionError("locked")),
                patch.object(backend.time, "sleep"),
            ):
                written = backend._write_production_run_status(status)

            self.assertFalse(written)
            self.assertEqual(status_path.read_text(encoding="utf-8"), previous)
            self.assertIn("locked", status["status_persistence_warning"])
            self.assertTrue(status["status_persistence_error_at"])

    def test_latest_segmented_shadow_run_id_tracks_dashboard_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "shadow.sqlite"
            con = sqlite3.connect(db_path)
            try:
                con.execute(
                    """
                    CREATE TABLE ml_quality_shadow_runs (
                      id INTEGER PRIMARY KEY,
                      source_rule_version TEXT
                    )
                    """
                )
                con.executemany(
                    """
                    INSERT INTO ml_quality_shadow_runs (id, source_rule_version)
                    VALUES (?, ?)
                    """,
                    [
                        (10, "another_rule"),
                        (11, backend.LOW_SCORE_SEGMENTED_SHADOW_RULE_VERSION),
                        (12, backend.LOW_SCORE_SEGMENTED_SHADOW_RULE_VERSION),
                    ],
                )
                con.commit()
            finally:
                con.close()

            self.assertEqual(
                backend._latest_low_score_segmented_shadow_run_id(db_path),
                12,
            )

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

    def test_materialization_is_published_as_completed_production_run(self) -> None:
        payload = {
            "version_id": 13,
            "version_number": 13,
            "changed_count": 11,
            "item_count": 288100,
            "segment_state_run_id": 817,
            "manifest_path": "docs/package_versions/materialized_v13.json",
        }
        with (
            patch.object(backend, "_now_iso", return_value="2026-09-04T18:34:35"),
            patch.object(backend, "_production_run_id", return_value="20260904_183435"),
        ):
            run = backend._completed_materialization_status(payload)

        self.assertEqual(run["run_id"], "20260904_183435_materialize_v13")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["run_mode"], "publication")
        self.assertEqual(run["next_action"], "await_new_applies")
        self.assertEqual(run["new_segment_state_run_id"], 817)
        self.assertEqual([stage["status"] for stage in run["stages"]], ["done"] * 4)
        self.assertIn("fila Pacote limpa", run["message"])

    def test_failed_publication_is_reconciled_by_newer_closed_segment_state(self) -> None:
        app_state = {
            "cache": {"generated_at": "2026-09-04T10:42:09"},
            "release": {
                "readiness": "ready_for_release",
                "latest_segment_state_run_id": 808,
                "pending_count": 0,
                "needs_apply": 0,
                "operational_closure": {"is_closed": True, "segment_state_run_id": 808},
                "post_release": {"diff_review": {"summary": {"package_diff_count": 8}}},
            },
        }
        failed_run = {
            "run_id": "20260904_095506",
            "status": "failed",
            "mode": "publication_apply_confirmed",
            "failed_stage": "segment_state_after",
            "new_segment_state_run_id": 807,
            "output_changed": True,
            "output_written_count": 5,
            "publication_closure_validation": {
                "segment_state_run_id": 807,
                "blocked_ids": [159168, 160112],
            },
            "failure": {
                "stage": "segment_state_after",
                "message": "Publication lifecycle closure mismatch: closed 3 / expected 5; blocked ids [159168, 160112].",
            },
        }

        display_run, reconciliation = backend._production_run_for_current_state(app_state, failed_run)

        self.assertEqual(display_run["status"], "completed")
        self.assertTrue(display_run["reconciled"])
        self.assertEqual(display_run["next_action"], "materialize_version")
        self.assertEqual(display_run["new_segment_state_run_id"], 808)
        self.assertEqual([stage["status"] for stage in display_run["stages"]], ["done"] * 4)
        self.assertEqual(reconciliation["blocked_ids"], [159168, 160112])

    def test_failed_publication_remains_failed_until_state_is_newer_and_closed(self) -> None:
        failed_run = {
            "run_id": "failed-run",
            "status": "failed",
            "failed_stage": "segment_state_after",
            "publication_closure_validation": {
                "segment_state_run_id": 807,
                "blocked_ids": [159168],
            },
            "failure": {"message": "Publication lifecycle closure mismatch"},
        }
        app_state = {
            "release": {
                "latest_segment_state_run_id": 808,
                "pending_count": 1,
                "needs_apply": 0,
                "operational_closure": {"is_closed": False},
            }
        }

        display_run, reconciliation = backend._production_run_for_current_state(app_state, failed_run)

        self.assertIs(display_run, failed_run)
        self.assertIsNone(reconciliation)

    def test_app_state_preserves_failed_history_while_showing_reconciliation(self) -> None:
        cached_app_state = {
            "cache": {},
            "release": {
                "readiness": "ready_for_release",
                "latest_segment_state_run_id": 808,
                "pending_count": 0,
                "needs_apply": 0,
                "operational_closure": {"is_closed": True},
                "post_release": {"diff_review": {"summary": {"package_diff_count": 3}}},
            },
            "production": {},
        }
        cache = {
            "generated_at": "2026-09-04T10:42:09",
            "source_db_mtime": 10.0,
            "app_state": cached_app_state,
        }
        failed_run = {
            "run_id": "failed-run",
            "status": "failed",
            "failed_stage": "segment_state_after",
            "publication_closure_validation": {
                "segment_state_run_id": 807,
                "blocked_ids": [159168],
            },
            "failure": {"message": "Publication lifecycle closure mismatch"},
        }
        with (
            patch.object(backend, "_get_dashboard_cache", return_value=cache),
            patch.object(backend, "_read_production_run_status", return_value=failed_run),
            patch.object(backend, "_db_mtime", return_value=10.0),
        ):
            payload = backend._app_state_payload(Path("fake.sqlite"))

        self.assertEqual(payload["production"]["last_run"]["status"], "completed")
        self.assertEqual(payload["production"]["last_failed_run"]["run_id"], "failed-run")
        self.assertEqual(payload["production"]["run_reconciliation"]["status"], "resolved")
        self.assertEqual(payload["production"]["progress_pct"], 100)


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
