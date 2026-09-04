from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from dashboard import backend


class PublicationApplyPopulationTests(unittest.TestCase):
    def test_output_apply_population_is_loaded_beyond_preview_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "test.sqlite"
            con = sqlite3.connect(db_path)
            try:
                con.executescript(
                    """
                    CREATE TABLE source_segments (
                      id INTEGER PRIMARY KEY,
                      is_active INTEGER NOT NULL
                    );
                    CREATE TABLE segment_state_items (
                      run_id INTEGER NOT NULL,
                      segment_id INTEGER NOT NULL,
                      needs_output_apply INTEGER NOT NULL,
                      priority_score REAL,
                      relative_path TEXT,
                      source_line_number INTEGER
                    );
                    INSERT INTO source_segments VALUES (1, 1), (2, 1), (3, 1);
                    INSERT INTO segment_state_items VALUES
                      (9, 1, 1, 10, 'a.yml', 1),
                      (9, 2, 1, 5, 'b.yml', 2),
                      (9, 3, 0, 1, 'c.yml', 3);
                    """
                )
                con.commit()
            finally:
                con.close()
            payload = {
                "app_state": {
                    "release": {
                        "post_release": {
                            "diff_review": {
                                "apply_segments": [
                                    {
                                        "segment_id": 1,
                                        "apply_kind": "output_write",
                                    },
                                    {
                                        "segment_id": 3,
                                        "apply_kind": "lifecycle_closure",
                                    },
                                ]
                            }
                        }
                    }
                }
            }

            segment_ids = backend._publication_apply_segment_ids(
                payload,
                db_path=db_path,
                state_run_id=9,
                output_apply_count=2,
            )

        self.assertEqual(segment_ids, [3, 1, 2])

    def test_human_lifecycle_population_excludes_pairwise_and_unlocked_confirmations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "test.sqlite"
            con = sqlite3.connect(db_path)
            try:
                con.executescript(
                    """
                    CREATE TABLE segment_confirmations (
                      segment_id INTEGER,
                      confirmation_level TEXT,
                      confirmation_source TEXT,
                      locked INTEGER
                    );
                    INSERT INTO segment_confirmations VALUES
                      (1, 'human_confirmed', 'local_learning', 1),
                      (2, 'human_confirmed', 'pairwise_monotonic_repair', 1),
                      (3, 'human_confirmed', 'post_apply_human_correction', 0),
                      (4, 'human_confirmed', 'codex_release_promotion_review', 1);
                    """
                )
                con.commit()
            finally:
                con.close()

            segment_ids = backend._publication_human_lifecycle_segment_ids(db_path, [1, 2, 3, 4])

        self.assertEqual(segment_ids, [1, 4])

    def test_closure_validation_distinguishes_write_failure_from_lifecycle_block(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "test.sqlite"
            con = sqlite3.connect(db_path)
            try:
                con.executescript(
                    """
                    CREATE TABLE segment_state_runs (
                      id INTEGER PRIMARY KEY,
                      finished_at TEXT
                    );
                    CREATE TABLE source_segments (
                      id INTEGER PRIMARY KEY,
                      relative_path TEXT,
                      source_key TEXT
                    );
                    CREATE TABLE segment_state_items (
                      run_id INTEGER,
                      segment_id INTEGER,
                      state_group TEXT,
                      final_state TEXT,
                      needs_output_apply INTEGER
                    );
                    CREATE TABLE output_segments (
                      segment_id INTEGER,
                      portuguese_text TEXT
                    );
                    CREATE TABLE segment_confirmations (
                      segment_id INTEGER,
                      confirmed_text TEXT,
                      confirmation_source TEXT,
                      confirmation_label TEXT
                    );
                    INSERT INTO segment_state_runs VALUES (10, '2026-09-04T12:00:00');
                    INSERT INTO source_segments VALUES
                      (1, 'a.yml', 'a'), (2, 'b.yml', 'b'), (3, 'c.yml', 'c');
                    INSERT INTO segment_state_items VALUES
                      (10, 1, 'closed', 'closed_ok', 0),
                      (10, 2, 'pending', 'reopen_auto_confirmed_autofix', 0),
                      (10, 3, 'pending', 'pending_apply', 1);
                    INSERT INTO output_segments VALUES
                      (1, 'ok'), (2, '\\"applied\\"'), (3, 'old');
                    INSERT INTO segment_confirmations VALUES
                      (1, 'ok', 'local_learning', 'correct'),
                      (2, '"applied"', 'local_learning', 'semantic_error'),
                      (3, 'new', 'local_learning', 'semantic_error');
                    """
                )
                con.commit()
            finally:
                con.close()

            result = backend._validate_publication_segments_closed(db_path, [1, 2, 3])

        self.assertEqual(result["closed_ids"], [1])
        self.assertEqual(result["lifecycle_blocked_ids"], [2])
        self.assertEqual(result["write_failed_ids"], [3])
        self.assertEqual(result["blocked_ids"], [3, 2])

    def test_publication_apply_has_explicit_human_lifecycle_stages(self) -> None:
        status = backend._new_publication_apply_status("test-run")
        stage_ids = [stage["id"] for stage in status["stages"]]

        self.assertIn("segment_state_after_write", stage_ids)
        self.assertIn("human_lifecycle_dry_run", stage_ids)
        self.assertIn("human_lifecycle_bridge", stage_ids)
        self.assertNotIn("materialize_version", stage_ids)

    def test_human_lifecycle_readiness_uses_latest_finished_ledger_for_intermediate_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "test.sqlite"
            con = sqlite3.connect(db_path)
            try:
                con.executescript(
                    """
                    CREATE TABLE segment_state_runs (
                      id INTEGER PRIMARY KEY,
                      finished_at TEXT
                    );
                    CREATE TABLE segment_state_items (
                      run_id INTEGER,
                      segment_id INTEGER,
                      state_group TEXT,
                      final_state TEXT,
                      needs_output_apply INTEGER,
                      confirmed_matches_output INTEGER,
                      relative_path TEXT,
                      source_key TEXT,
                      source_line_number INTEGER
                    );
                    CREATE TABLE source_segments (
                      id INTEGER PRIMARY KEY,
                      relative_path TEXT,
                      source_key TEXT,
                      source_line_number INTEGER
                    );
                    CREATE TABLE output_segments (
                      segment_id INTEGER PRIMARY KEY,
                      portuguese_text TEXT
                    );
                    CREATE TABLE segment_confirmations (
                      id INTEGER PRIMARY KEY,
                      segment_id INTEGER,
                      confirmed_text TEXT,
                      confirmation_level TEXT,
                      confirmation_source TEXT,
                      confirmation_label TEXT,
                      locked INTEGER,
                      candidate_id INTEGER,
                      updated_at TEXT
                    );
                    CREATE TABLE local_learning_candidates (
                      id INTEGER PRIMARY KEY,
                      run_id INTEGER,
                      origin TEXT,
                      match_type TEXT
                    );
                    CREATE TABLE ml_issue_ledger_runs (
                      id INTEGER PRIMARY KEY,
                      segment_state_run_id INTEGER,
                      finished_at TEXT
                    );
                    CREATE TABLE ml_issue_ledger_items (
                      run_id INTEGER,
                      segment_id INTEGER,
                      issue_family TEXT,
                      issue_kind TEXT,
                      issue_severity TEXT,
                      agent_key TEXT,
                      status TEXT,
                      route_status TEXT,
                      proposed_action TEXT,
                      validation_status TEXT
                    );
                    CREATE TABLE auto_confirmation_reopen_lifecycle_policy_runs (
                      id INTEGER PRIMARY KEY,
                      policy_name TEXT,
                      policy_status TEXT
                    );
                    CREATE TABLE auto_confirmation_reopen_lifecycle_policy_items (
                      id INTEGER PRIMARY KEY,
                      run_id INTEGER,
                      segment_id INTEGER,
                      policy_action TEXT,
                      policy_allowed INTEGER
                    );

                    INSERT INTO segment_state_runs VALUES (815, '2026-09-04T18:00:00');
                    INSERT INTO segment_state_items VALUES
                      (815, 159520, 'pending', 'reopen_auto_confirmed_autofix', 0, 1,
                       'sample.yml', 'sample.key', 10);
                    INSERT INTO source_segments VALUES (159520, 'sample.yml', 'sample.key', 10);
                    INSERT INTO output_segments VALUES (159520, 'texto corrigido');
                    INSERT INTO local_learning_candidates VALUES
                      (99, 1764, 'regenerative_discovery_review', 'discovery:test');
                    INSERT INTO segment_confirmations VALUES
                      (100, 159520, 'texto corrigido', 'human_confirmed', 'local_learning',
                       'semantic_error', 1, 99, '2026-09-04T18:00:00');
                    -- O ledger concluido e anterior e nao pertence ao snapshot intermediario 815.
                    INSERT INTO ml_issue_ledger_runs VALUES (77, 374, '2026-06-19T18:58:50');
                    """
                )
                con.commit()
            finally:
                con.close()

            result = backend._publication_lifecycle_apply_readiness(
                db_path,
                state_run_id=815,
                segment_ids=[159520],
            )

        self.assertTrue(result["available"])
        self.assertEqual(result["ledger_run_id"], 77)
        self.assertEqual(result["ready_ids"], [159520])
        self.assertEqual(result["blocked"], 0)


if __name__ == "__main__":
    unittest.main()
