from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

import db  # noqa: E402
from quality_closed_observation_audit import (  # noqa: E402
    audit_closed_observations,
    persist_audit,
)


class QualityClosedObservationAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.ensure_database(self.conn)
        now = "2026-07-20T00:00:00Z"
        self.conn.execute(
            """
            INSERT INTO ml_score_runs (
                id, rule_version, model_run_id, model_version, scored_count,
                started_at, finished_at, updated_at
            ) VALUES (10, 'test', 1, 'test', 4, ?, ?, ?)
            """,
            (now, now, now),
        )
        self.conn.execute(
            """
            INSERT INTO segment_state_runs (
                id, rule_version, active_score_run_id, candidate_score_run_id,
                total_segments, closed_count, pending_count,
                output_apply_pending_count, started_at, finished_at, updated_at
            ) VALUES (20, 'test', 10, 10, 4, 3, 1, 0, ?, ?, ?)
            """,
            (now, now, now),
        )
        self.conn.execute(
            """
            INSERT INTO ml_quality_pattern_discovery_runs (
                id, run_key, rule_version, score_run_id, active_segment_count,
                evidence_segment_count, family_count, status, summary_json,
                started_at, finished_at, updated_at
            ) VALUES (7, 'discovery-7', 'test', 10, 4, 4, 4,
                      'completed', '{}', ?, ?, ?)
            """,
            (now, now, now),
        )
        cases = [
            (1, "accepted", "distinct_candidate", "Texto aceito", "Texto antigo", "closed", 1, 1, 0),
            (2, "baseline", "equals_old", "Texto antigo", "Texto antigo", "closed", 0, 0, 0),
            (3, "review", "distinct_candidate", "Texto suspeito", "Texto antigo", "closed", 0, 0, 0),
            (4, "inconsistent", "distinct_candidate", "Texto pendente", "Texto antigo", "pending", 0, 0, 0),
        ]
        for segment_id, issue_type, relation, output_text, old_text, state_group, locked, matches, needs_apply in cases:
            relative_path = f"test/{segment_id}_l_spanish.yml"
            source_key = f"key_{segment_id}"
            self.conn.execute(
                """
                INSERT INTO source_segments (
                    id, relative_path, source_line_number, source_key,
                    spanish_text, old_text, is_active, first_indexed_at, last_indexed_at
                ) VALUES (?, ?, 1, ?, ?, ?, 1, ?, ?)
                """,
                (segment_id, relative_path, source_key, old_text, old_text, now, now),
            )
            self.conn.execute(
                """
                INSERT INTO output_segments (
                    segment_id, relative_path, portuguese_text, last_indexed_at
                ) VALUES (?, ?, ?, ?)
                """,
                (segment_id, relative_path, output_text, now),
            )
            self.conn.execute(
                """
                INSERT INTO ml_score_items (
                    run_id, segment_id, relative_path, source_key, candidate_text,
                    model_action, final_action, risk_class, model_safe_probability,
                    token_status, issue_count, created_at
                ) VALUES (10, ?, ?, ?, ?, 'needs_human', 'needs_human', 'high',
                          0.3, 'ok', 1, ?)
                """,
                (segment_id, relative_path, source_key, output_text, now),
            )
            is_closed = 1 if state_group == "closed" else 0
            self.conn.execute(
                """
                INSERT INTO segment_state_items (
                    run_id, segment_id, relative_path, source_key, final_state,
                    state_group, output_state, review_state, apply_state, locked,
                    has_output, confirmed_matches_output, needs_output_apply,
                    is_closed, created_at
                ) VALUES (20, ?, ?, ?, ?, ?, 'present', 'reviewed', 'none', ?,
                          1, ?, ?, ?, ?)
                """,
                (
                    segment_id, relative_path, source_key,
                    "closed_confirmed" if is_closed else "pending_review",
                    state_group, locked, matches, needs_apply, is_closed, now,
                ),
            )
            if segment_id == 1:
                self.conn.execute(
                    """
                    INSERT INTO segment_confirmations (
                        segment_id, confirmation_level, confirmed_text,
                        confirmation_source, confirmation_label, locked,
                        confirmed_at, updated_at
                    ) VALUES (1, 'human_confirmed', ?, 'human_review', 'accepted',
                              1, ?, ?)
                    """,
                    (output_text, now, now),
                )
            sample = json.dumps(
                [{"segment_id": segment_id, "relative_path": relative_path, "source_key": source_key}]
            )
            self.conn.execute(
                """
                INSERT INTO ml_quality_pattern_families (
                    id, family_key, evidence_kind, issue_type, token_context,
                    file_family, text_relation, status, first_run_id, last_run_id,
                    first_score_run_id, last_score_run_id, samples_json,
                    metadata_json, first_seen_at, last_seen_at, updated_at
                ) VALUES (?, ?, 'explicit_issue', ?, 'plain_text', 'test', ?,
                          'closed_observation', 7, 7, 10, 10, ?, '{}', ?, ?, ?)
                """,
                (segment_id, f"family-{segment_id}", issue_type, relation, sample, now, now, now),
            )
            self.conn.execute(
                """
                INSERT INTO ml_quality_pattern_observations (
                    run_id, family_id, segment_count, occurrence_count,
                    low_score_count, status, samples_json, metrics_json, created_at
                ) VALUES (7, ?, 1, 1, 1, 'closed_observation', ?, '{}', ?)
                """,
                (segment_id, sample, now),
            )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_classifies_governed_baseline_review_and_inconsistent_closures(self) -> None:
        result = audit_closed_observations(self.conn, discovery_run_id=7)

        self.assertEqual(result["sampled_family_count"], 4)
        self.assertEqual(result["sampled_segment_count"], 4)
        self.assertEqual(result["sampled_item_count"], 4)
        self.assertEqual(result["accepted_count"], 1)
        self.assertEqual(result["baseline_watch_count"], 1)
        self.assertEqual(result["review_required_count"], 1)
        self.assertEqual(result["inconsistent_count"], 1)
        self.assertEqual(result["duplicate_count"], 0)
        self.assertEqual(result["orphan_count"], 0)
        self.assertEqual(result["confirmation_write_count"], 0)
        self.assertEqual(result["output_write_count"], 0)

    def test_persistence_is_idempotent_at_discovery_and_state_grain(self) -> None:
        result = audit_closed_observations(self.conn, discovery_run_id=7)

        first_run_id = persist_audit(self.conn, result)
        second_run_id = persist_audit(self.conn, result)

        self.assertEqual(first_run_id, second_run_id)
        item_count = self.conn.execute(
            "SELECT COUNT(*) FROM ml_quality_closed_observation_audit_items WHERE run_id = ?",
            (first_run_id,),
        ).fetchone()[0]
        self.assertEqual(item_count, 4)


if __name__ == "__main__":
    unittest.main()
