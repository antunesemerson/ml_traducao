from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock


PIPELINE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PIPELINE_DIR.parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

import quality_epoch  # noqa: E402


def _load_dashboard_backend():
    spec = importlib.util.spec_from_file_location(
        "quality_epoch_test_dashboard_backend",
        PROJECT_ROOT / "dashboard" / "backend.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load dashboard backend for tests.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dashboard_backend = _load_dashboard_backend()


FINGERPRINT = {
    "epoch_key": "epoch-1",
    "baseline_tree_hash": "same-tree",
    "output_tree_hash": "same-tree",
    "source_snapshot_id": 1,
    "model_run_id": 358,
}


class QualityEpochReuseTests(unittest.TestCase):
    def test_epoch_uses_registry_active_model_instead_of_latest_candidate(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE ml_model_runs (
              id INTEGER PRIMARY KEY,
              rule_version TEXT,
              model_version TEXT,
              model_path TEXT,
              dataset_run_id INTEGER,
              safe_threshold REAL,
              finished_at TEXT
            );
            CREATE TABLE ml_model_registry (
              model_kind TEXT PRIMARY KEY,
              active_model_run_id INTEGER
            );
            INSERT INTO ml_model_runs VALUES
              (76, 'rule-active', 'active', 'models/active.joblib', 1, 0.9, '2026-05-01'),
              (358, 'rule-candidate', 'candidate', 'models/candidate.joblib', 2, 0.9, '2026-06-01');
            INSERT INTO ml_model_registry VALUES ('risk_action_classifier', 76);
            """
        )

        model = quality_epoch._latest_model(conn)

        self.assertEqual(model["id"], 76)
        self.assertEqual(model["model_selection_source"], "registry_active")
        conn.close()

    def test_identical_package_trees_require_only_output_score(self) -> None:
        plan = quality_epoch._score_plan_for_fingerprint(FINGERPRINT)

        self.assertEqual(plan["required_sources"], ["output"])
        self.assertEqual(plan["source_aliases"], {"old": "output"})
        self.assertTrue(plan["identical_package_trees"])
        self.assertEqual(dashboard_backend._diagnostic_score_sources({"score_plan": plan}), ["output"])

    def test_different_package_trees_keep_both_score_lanes(self) -> None:
        plan = quality_epoch._score_plan_for_fingerprint(
            {**FINGERPRINT, "output_tree_hash": "different-tree"}
        )

        self.assertEqual(plan["required_sources"], ["old", "output"])
        self.assertFalse(plan["identical_package_trees"])

    def test_missing_tree_hashes_never_trigger_deduplication(self) -> None:
        plan = quality_epoch._score_plan_for_fingerprint({})

        self.assertEqual(plan["required_sources"], ["old", "output"])
        self.assertFalse(plan["identical_package_trees"])

    def test_shared_attached_run_counts_as_finished_for_both_lanes(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE ml_score_runs (id INTEGER PRIMARY KEY, finished_at TEXT)"
        )
        conn.execute("INSERT INTO ml_score_runs VALUES (7, '2026-07-18T00:00:00Z')")

        self.assertTrue(quality_epoch._score_run_ids_finished(conn, (7, 7)))
        self.assertFalse(quality_epoch._score_run_ids_finished(conn, (7, 8)))
        conn.close()

    def test_finalize_scoring_attaches_output_run_to_both_identical_lanes(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE quality_epochs (
              id INTEGER PRIMARY KEY,
              epoch_key TEXT,
              old_score_run_id INTEGER,
              output_score_run_id INTEGER,
              policy_run_id INTEGER,
              segment_state_run_id INTEGER,
              scoring_rule_version TEXT,
              model_run_id INTEGER,
              model_version TEXT,
              status TEXT,
              scored_at TEXT,
              updated_at TEXT
            );
            CREATE TABLE ml_score_runs (
              id INTEGER PRIMARY KEY,
              candidate_text_source TEXT,
              candidate_tree_hash TEXT,
              source_snapshot_id INTEGER,
              model_run_id INTEGER,
              model_version TEXT,
              rule_version TEXT,
              finished_at TEXT
            );
            CREATE TABLE ml_policy_runs (
              id INTEGER PRIMARY KEY,
              score_run_id INTEGER,
              finished_at TEXT
            );
            CREATE TABLE segment_state_runs (
              id INTEGER PRIMARY KEY,
              finished_at TEXT
            );
            CREATE TABLE quality_epoch_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              epoch_id INTEGER,
              event_type TEXT,
              event_source TEXT,
              mutation_scope TEXT,
              details_json TEXT,
              created_at TEXT
            );
            INSERT INTO quality_epochs (id, epoch_key, status) VALUES (1, 'epoch-1', 'open');
            INSERT INTO ml_score_runs VALUES (
              12, 'output', 'same-tree', 1, 358, 'model-v1',
              'ml_score_segments_v1', '2026-07-18T00:00:00Z'
            );
            INSERT INTO ml_policy_runs VALUES (22, 12, '2026-07-18T00:01:00Z');
            INSERT INTO segment_state_runs VALUES (32, '2026-07-18T00:02:00Z');
            """
        )

        with (
            mock.patch.object(quality_epoch.db, "load_settings", return_value={}),
            mock.patch.object(quality_epoch.db, "connect", return_value=conn),
            mock.patch.object(quality_epoch.db, "ensure_database"),
            mock.patch.object(quality_epoch, "current_fingerprint", return_value=FINGERPRINT),
        ):
            result = quality_epoch.finalize_scoring(source="test")

        epoch = conn.execute(
            "SELECT old_score_run_id, output_score_run_id, status FROM quality_epochs WHERE id = 1"
        ).fetchone()
        self.assertEqual((epoch[0], epoch[1]), (12, 12))
        self.assertEqual(epoch[2], "scored")
        self.assertTrue(result["shared_score_run"])
        conn.close()


class QualityEpochSegmentStateLinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE quality_epochs (
              id INTEGER PRIMARY KEY,
              epoch_key TEXT,
              old_score_run_id INTEGER,
              output_score_run_id INTEGER,
              policy_run_id INTEGER,
              segment_state_run_id INTEGER,
              status TEXT,
              evaluated_at TEXT,
              published_at TEXT,
              invalidated_at TEXT,
              updated_at TEXT
            );
            CREATE TABLE segment_state_runs (
              id INTEGER PRIMARY KEY,
              finished_at TEXT
            );
            CREATE TABLE quality_epoch_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              epoch_id INTEGER,
              event_type TEXT,
              event_source TEXT,
              mutation_scope TEXT,
              details_json TEXT,
              created_at TEXT
            );
            INSERT INTO quality_epochs (
              id, epoch_key, old_score_run_id, output_score_run_id,
              policy_run_id, segment_state_run_id, status
            ) VALUES (1, 'epoch-1', 10, 11, 12, 20, 'scored');
            INSERT INTO segment_state_runs VALUES (20, '2026-07-18T00:00:00Z');
            INSERT INTO segment_state_runs VALUES (21, '2026-07-18T00:01:00Z');
            INSERT INTO segment_state_runs VALUES (22, NULL);
            """
        )

    def tearDown(self) -> None:
        self.conn.close()

    def current_epoch(self, status: str = "scored") -> dict:
        return {
            "epoch_id": 1,
            "epoch_key": "epoch-1",
            "status": status,
            "old_score_run_id": 10,
            "output_score_run_id": 11,
            "policy_run_id": 12,
            "segment_state_run_id": 20,
            "fingerprint_current": True,
        }

    def test_mark_evaluated_attaches_explicit_finished_segment_state(self) -> None:
        with (
            mock.patch.object(quality_epoch, "validate_current", return_value=self.current_epoch()),
            mock.patch.object(quality_epoch.db, "connect", return_value=self.conn),
            mock.patch.object(quality_epoch.db, "utc_now", return_value="2026-07-18T00:02:00Z"),
        ):
            result = quality_epoch.mark_status(
                "evaluated",
                source="test",
                segment_state_run_id=21,
            )

        epoch = self.conn.execute(
            "SELECT status, segment_state_run_id FROM quality_epochs WHERE id = 1"
        ).fetchone()
        event = self.conn.execute(
            "SELECT details_json FROM quality_epoch_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual((epoch["status"], epoch["segment_state_run_id"]), ("evaluated", 21))
        self.assertEqual(result["segment_state_run_id"], 21)
        self.assertEqual(
            json.loads(event["details_json"]),
            {"previous_segment_state_run_id": 20, "segment_state_run_id": 21},
        )

    def test_mark_evaluated_falls_back_to_latest_finished_segment_state(self) -> None:
        with (
            mock.patch.object(quality_epoch, "validate_current", return_value=self.current_epoch()),
            mock.patch.object(quality_epoch.db, "connect", return_value=self.conn),
            mock.patch.object(quality_epoch.db, "utc_now", return_value="2026-07-18T00:02:00Z"),
        ):
            result = quality_epoch.mark_status("evaluated", source="test")

        self.assertEqual(result["segment_state_run_id"], 21)

    def test_mark_rejects_unfinished_explicit_segment_state(self) -> None:
        with (
            mock.patch.object(quality_epoch, "validate_current", return_value=self.current_epoch()),
            mock.patch.object(quality_epoch.db, "connect", return_value=self.conn),
        ):
            with self.assertRaisesRegex(RuntimeError, "does not exist or is not finished"):
                quality_epoch.mark_status(
                    "evaluated",
                    source="test",
                    segment_state_run_id=22,
                )

    def test_attach_segment_state_updates_epoch_without_changing_status(self) -> None:
        with (
            mock.patch.object(quality_epoch.db, "load_settings", return_value={}),
            mock.patch.object(quality_epoch.db, "connect", return_value=self.conn),
            mock.patch.object(quality_epoch.db, "ensure_database"),
            mock.patch.object(quality_epoch.db, "utc_now", return_value="2026-07-18T00:02:00Z"),
        ):
            result = quality_epoch.attach_segment_state(
                source="diagnostic",
                epoch_id=1,
                segment_state_run_id=21,
            )

        epoch = self.conn.execute(
            "SELECT status, segment_state_run_id FROM quality_epochs WHERE id = 1"
        ).fetchone()
        self.assertEqual((epoch["status"], epoch["segment_state_run_id"]), ("scored", 21))
        self.assertEqual(result["previous_segment_state_run_id"], 20)
        self.assertEqual(result["segment_state_run_id"], 21)


class QualityEpochNoopClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE quality_epochs (
              id INTEGER PRIMARY KEY,
              epoch_key TEXT,
              baseline_tree_hash TEXT,
              output_tree_hash TEXT,
              old_score_run_id INTEGER,
              output_score_run_id INTEGER,
              segment_state_run_id INTEGER,
              status TEXT,
              evaluated_at TEXT,
              updated_at TEXT
            );
            CREATE TABLE segment_state_runs (
              id INTEGER PRIMARY KEY,
              finished_at TEXT
            );
            CREATE TABLE ml_quality_pattern_discovery_runs (
              id INTEGER PRIMARY KEY,
              quality_epoch_id INTEGER,
              status TEXT
            );
            CREATE TABLE ml_pairwise_calibration_policy_decisions (
              id INTEGER PRIMARY KEY,
              quality_epoch_id INTEGER,
              decision TEXT,
              candidate_count INTEGER
            );
            CREATE TABLE quality_epoch_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              epoch_id INTEGER,
              event_type TEXT,
              event_source TEXT,
              mutation_scope TEXT,
              details_json TEXT,
              created_at TEXT
            );
            INSERT INTO quality_epochs VALUES (
              1, 'epoch-1', 'same-tree', 'same-tree', 12, 12, 21,
              'scored', NULL, NULL
            );
            INSERT INTO segment_state_runs VALUES (21, '2026-07-20T01:00:00Z');
            INSERT INTO ml_quality_pattern_discovery_runs VALUES (31, 1, 'completed');
            INSERT INTO ml_pairwise_calibration_policy_decisions VALUES (41, 1, 'skip', 0);
            """
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_identical_scored_epoch_is_evaluated_after_discovery(self) -> None:
        with (
            mock.patch.object(quality_epoch.db, "load_settings", return_value={}),
            mock.patch.object(quality_epoch.db, "connect", return_value=self.conn),
            mock.patch.object(quality_epoch.db, "ensure_database"),
            mock.patch.object(quality_epoch.db, "utc_now", return_value="2026-07-20T01:05:00Z"),
            mock.patch.object(quality_epoch, "current_fingerprint", return_value=FINGERPRINT),
        ):
            result = quality_epoch.close_noop_epoch(source="diagnostic", epoch_id=1)

        epoch = self.conn.execute(
            "SELECT status, evaluated_at, segment_state_run_id FROM quality_epochs WHERE id = 1"
        ).fetchone()
        event = self.conn.execute(
            "SELECT event_type, details_json FROM quality_epoch_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(tuple(epoch), ("evaluated", "2026-07-20T01:05:00Z", 21))
        self.assertTrue(result["noop_evaluated"])
        self.assertEqual(result["reason"], "identical_package_no_candidates")
        self.assertEqual(event["event_type"], "epoch_noop_evaluated")
        self.assertEqual(json.loads(event["details_json"])["candidate_count"], 0)

    def test_noop_closure_skips_when_pairwise_candidates_exist(self) -> None:
        self.conn.execute(
            "UPDATE ml_pairwise_calibration_policy_decisions SET decision = 'required', candidate_count = 2"
        )
        with (
            mock.patch.object(quality_epoch.db, "load_settings", return_value={}),
            mock.patch.object(quality_epoch.db, "connect", return_value=self.conn),
            mock.patch.object(quality_epoch.db, "ensure_database"),
            mock.patch.object(quality_epoch, "current_fingerprint", return_value=FINGERPRINT),
        ):
            result = quality_epoch.close_noop_epoch(source="diagnostic", epoch_id=1)

        status = self.conn.execute("SELECT status FROM quality_epochs WHERE id = 1").fetchone()[0]
        self.assertEqual(status, "scored")
        self.assertFalse(result["noop_evaluated"])
        self.assertEqual(result["reason"], "pairwise_candidates_present")


if __name__ == "__main__":
    unittest.main()
