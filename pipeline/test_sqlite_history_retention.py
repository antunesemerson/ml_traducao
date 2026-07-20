from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

import sqlite_history_retention as retention  # noqa: E402


def make_database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE ml_score_runs (
            id INTEGER PRIMARY KEY,
            candidate_text_source TEXT,
            scored_count INTEGER NOT NULL,
            finished_at TEXT
        );
        CREATE TABLE ml_score_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL REFERENCES ml_score_runs(id) ON DELETE CASCADE
        );
        CREATE TABLE package_versions (
            id INTEGER PRIMARY KEY,
            score_run_id INTEGER REFERENCES ml_score_runs(id) ON DELETE SET NULL,
            segment_state_run_id INTEGER REFERENCES segment_state_runs(id) ON DELETE SET NULL
        );
        CREATE TABLE segment_state_runs (
            id INTEGER PRIMARY KEY,
            total_segments INTEGER NOT NULL,
            finished_at TEXT
        );
        CREATE TABLE segment_state_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL REFERENCES segment_state_runs(id) ON DELETE CASCADE
        );
        CREATE TABLE quality_epochs (
            id INTEGER PRIMARY KEY,
            old_score_run_id INTEGER REFERENCES ml_score_runs(id) ON DELETE SET NULL,
            output_score_run_id INTEGER REFERENCES ml_score_runs(id) ON DELETE SET NULL,
            segment_state_run_id INTEGER REFERENCES segment_state_runs(id) ON DELETE SET NULL
        );
        """
    )
    for run_id in range(1, 8):
        lane = "old" if run_id % 2 else "output"
        finished = None if run_id == 1 else "2026-01-01T00:00:00Z"
        conn.execute(
            "INSERT INTO ml_score_runs VALUES (?, ?, 2, ?)",
            (run_id, lane, finished),
        )
        conn.executemany(
            "INSERT INTO ml_score_items(id, run_id) VALUES (?, ?)",
            ((run_id * 10 + 1, run_id), (run_id * 10 + 2, run_id)),
        )
    for run_id in range(1, 8):
        finished = None if run_id == 1 else "2026-01-01T00:00:00Z"
        conn.execute("INSERT INTO segment_state_runs VALUES (?, 3, ?)", (run_id, finished))
        conn.executemany(
            "INSERT INTO segment_state_items(id, run_id) VALUES (?, ?)",
            ((run_id * 10 + 1, run_id), (run_id * 10 + 2, run_id), (run_id * 10 + 3, run_id)),
        )
    conn.execute("INSERT INTO package_versions VALUES (1, 2, 2)")
    conn.execute("INSERT INTO quality_epochs VALUES (1, 3, 4, 3)")
    conn.commit()
    return conn


class RetentionPlanTests(unittest.TestCase):
    def test_schema_reconciles_an_interrupted_committed_batch(self) -> None:
        conn = make_database()
        retention.ensure_retention_schema(conn)
        cursor = conn.execute(
            f"""
            INSERT INTO {retention.RETENTION_RUNS_TABLE} (
                policy_version, plan_hash, plan_json, status,
                database_bytes_before, reusable_bytes_before, started_at
            ) VALUES (?, ?, ?, 'running', 100, 10, ?)
            """,
            (retention.POLICY_VERSION, "fixture", "{}", retention.utc_now()),
        )
        retention_run_id = int(cursor.lastrowid)
        conn.execute(
            f"""
            INSERT INTO {retention.RETENTION_SCOPES_TABLE} (
                retention_run_id, scope, source_run_id,
                estimated_item_count, deleted_item_count,
                cascaded_changes, pruned_at
            ) VALUES (?, ?, 99, 3, 3, 0, ?)
            """,
            (retention_run_id, retention.STATE_SCOPE, retention.utc_now()),
        )
        conn.commit()

        retention.ensure_retention_schema(conn)

        recovered = conn.execute(
            f"SELECT * FROM {retention.RETENTION_RUNS_TABLE} WHERE id = ?",
            (retention_run_id,),
        ).fetchone()
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(recovered["deleted_detail_items"], 3)
        self.assertEqual(recovered["cascaded_changes"], 0)
        self.assertIsNotNone(recovered["finished_at"])
        self.assertIn("audited and preserved", recovered["error_text"])

    def test_delete_foreign_keys_receive_covering_indexes(self) -> None:
        conn = make_database()
        conn.executescript(
            """
            CREATE TABLE policy_item_fixture (
                id INTEGER PRIMARY KEY,
                score_item_id INTEGER REFERENCES ml_score_items(id) ON DELETE CASCADE
            );
            """
        )
        created = retention.ensure_retention_delete_indexes(conn)

        self.assertEqual(len(created), 1)
        self.assertTrue(
            retention._index_covers_column(conn, "policy_item_fixture", "score_item_id")
        )

    def test_plan_protects_references_unfinished_and_recent_runs(self) -> None:
        conn = make_database()
        plan = retention.build_retention_plan(
            conn,
            keep_latest_per_lane=1,
            keep_latest_state_runs=1,
        )
        score = plan["scopes"][retention.SCORE_SCOPE]
        state = plan["scopes"][retention.STATE_SCOPE]

        self.assertEqual(score["candidate_run_ids"], [5])
        self.assertEqual(score["estimated_detail_items"], 2)
        self.assertEqual(state["candidate_run_ids"], [4, 5, 6])
        self.assertEqual(state["estimated_detail_items"], 9)
        self.assertTrue(plan["execution_token"].startswith("PRUNE-"))

    def test_apply_prunes_details_but_preserves_parent_summaries(self) -> None:
        conn = make_database()
        plan = retention.build_retention_plan(
            conn,
            keep_latest_per_lane=1,
            keep_latest_state_runs=1,
        )
        result = retention.apply_retention_plan(
            conn,
            plan,
            confirmation_token=plan["execution_token"],
            project_root=retention.PROJECT_ROOT / "test-fixture-without-locks",
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["deleted_detail_items"], 11)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ml_score_runs").fetchone()[0], 7)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM segment_state_runs").fetchone()[0], 7)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM ml_score_items WHERE run_id = 5").fetchone()[0],
            0,
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM segment_state_items WHERE run_id IN (4, 5, 6)"
            ).fetchone()[0],
            0,
        )

    def test_invalid_confirmation_does_not_write(self) -> None:
        conn = make_database()
        plan = retention.build_retention_plan(
            conn,
            keep_latest_per_lane=1,
            keep_latest_state_runs=1,
        )
        with self.assertRaises(ValueError):
            retention.apply_retention_plan(
                conn,
                plan,
                confirmation_token="PRUNE-WRONG",
                project_root=retention.PROJECT_ROOT / "test-fixture-without-locks",
            )
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ml_score_items").fetchone()[0], 14)

    def test_scope_and_detail_budget_bound_the_batch(self) -> None:
        conn = make_database()
        plan = retention.build_retention_plan(
            conn,
            keep_latest_per_lane=1,
            keep_latest_state_runs=1,
        )
        result = retention.apply_retention_plan(
            conn,
            plan,
            confirmation_token=plan["execution_token"],
            project_root=retention.PROJECT_ROOT / "test-fixture-without-locks",
            scopes=(retention.STATE_SCOPE,),
            max_detail_items=3,
        )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["stop_reason"], "max_detail_items")
        self.assertEqual(result["selected_scopes"], [retention.STATE_SCOPE])
        self.assertEqual(result["processed_runs"], 1)
        self.assertEqual(result["deleted_detail_items"], 3)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM segment_state_items WHERE run_id = 4"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM segment_state_items WHERE run_id = 5"
            ).fetchone()[0],
            3,
        )
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ml_score_items").fetchone()[0], 14)

    def test_empty_scope_is_rejected_without_writes(self) -> None:
        conn = make_database()
        plan = retention.build_retention_plan(
            conn,
            keep_latest_per_lane=1,
            keep_latest_state_runs=1,
        )

        with self.assertRaises(ValueError):
            retention.apply_retention_plan(
                conn,
                plan,
                confirmation_token=plan["execution_token"],
                project_root=retention.PROJECT_ROOT / "test-fixture-without-locks",
                scopes=(),
            )

        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ml_score_items").fetchone()[0], 14)


if __name__ == "__main__":
    unittest.main()
