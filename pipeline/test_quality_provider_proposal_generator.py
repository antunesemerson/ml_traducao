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
import local_quality_validator  # noqa: E402
from quality_provider_proposal_generator import (  # noqa: E402
    _correction_lane,
    _discovery_review_by_family,
    generate_provider_proposals,
    persist_provider_proposals,
)
from quality_pattern_discovery import _refine_human_issue_type  # noqa: E402
from suggest_translations import remove_spanish_angular_quotes  # noqa: E402


class QualityProviderProposalGeneratorTests(unittest.TestCase):
    def test_angular_quotes_are_routed_to_ptbr_punctuation(self) -> None:
        issue_type = _refine_human_issue_type(
            "residual_spanish",
            "«Texto em português»",
            "aspas espanholas",
        )
        self.assertEqual(issue_type, "spanish_angular_quotes")
        self.assertEqual(_correction_lane(issue_type, "plain_text")["id"], "ptbr_punctuation")
        self.assertEqual(
            remove_spanish_angular_quotes("«Texto» e Â«outroÂ»"),
            '"Texto" e "outro"',
        )
        issue_codes = {
            issue["code"]
            for issue in local_quality_validator.validate_text("«Texto em português»")["issues"]
        }
        self.assertIn("spanish_angular_quotes", issue_codes)
        self.assertNotIn("spanish_punctuation", issue_codes)

    def test_real_residual_spanish_stays_in_semantic_lane(self) -> None:
        issue_type = _refine_human_issue_type(
            "residual_spanish",
            "Todavía queda texto español.",
            "conteúdo espanhol residual",
        )
        self.assertEqual(issue_type, "residual_spanish")
        self.assertEqual(_correction_lane(issue_type, "plain_text")["id"], "spanish_residual")

    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.ensure_database(self.conn)
        now = "2026-07-20T00:00:00Z"
        self.conn.execute(
            """
            INSERT INTO ml_quality_pattern_discovery_runs (
                id, run_key, rule_version, quality_epoch_id, score_run_id,
                scoring_contract_hash, active_segment_count, evidence_segment_count,
                family_count, new_family_count, recurring_family_count,
                covered_family_count, actionable_family_count,
                ignored_score_only_count, status, summary_json,
                started_at, finished_at, updated_at
            ) VALUES (7, 'discovery-7', 'test', NULL, 10, 'contract', 100,
                      7, 2, 1, 1, 0, 2, 0, 'completed', '{}', ?, ?, ?)
            """,
            (now, now, now),
        )
        families = [
            (1, "family-a", "dlc/example", "new_candidate", 4, 88.0, 0.84, 1),
            (2, "family-b", "event_localization/events", "recurring_candidate", 3, 76.0, 0.80, 2),
        ]
        for family_id, family_key, file_family, status, count, priority, confidence, segment_id in families:
            sample = [
                {
                    "segment_id": segment_id,
                    "relative_path": f"{file_family}/sample_l_spanish.yml",
                    "source_key": f"sample_{segment_id}",
                    "candidate_text": "Texto [token] , com fronteira",
                    "score": 0.31,
                }
            ]
            self.conn.execute(
                """
                INSERT INTO ml_quality_pattern_families (
                    id, family_key, evidence_kind, issue_type, token_context,
                    file_family, text_relation, provider_id, evidence_type, status,
                    first_run_id, last_run_id, first_score_run_id, last_score_run_id,
                    observation_count, latest_priority, latest_confidence,
                    latest_reach, latest_severity, latest_segment_count,
                    samples_json, metadata_json, first_seen_at, last_seen_at, updated_at
                ) VALUES (?, ?, 'explicit_issue', 'new_spacing_issue', 'token_boundary',
                          ?, 'distinct_candidate', NULL, NULL, ?, 7, 7, 10, 10,
                          1, ?, ?, 0.5, 0.65, ?, ?, '{}', ?, ?, ?)
                """,
                (
                    family_id, family_key, file_family, status, priority,
                    confidence, count, json.dumps(sample), now, now, now,
                ),
            )
            self.conn.execute(
                """
                INSERT INTO ml_quality_pattern_observations (
                    run_id, family_id, segment_count, occurrence_count,
                    low_score_count, low_score_share, active_package_share,
                    average_score, minimum_score, maximum_score, severity,
                    confidence, novelty, reach, priority, status,
                    samples_json, metrics_json, created_at
                ) VALUES (7, ?, ?, ?, ?, 1, 0.01, 0.31, 0.31, 0.31,
                          0.65, ?, 1, 0.5, ?, ?, ?, ?, ?)
                """,
                (
                    family_id, count, count, count, confidence, priority, status,
                    json.dumps(sample),
                    json.dumps({"operational_segment_count": count}),
                    now,
                ),
            )
        self.conn.execute(
            """
            INSERT INTO ml_score_items (
                run_id, segment_id, relative_path, source_key, candidate_text,
                model_action, final_action, risk_class, model_safe_probability,
                token_status, issue_count, created_at
            ) VALUES (10, 90, 'dlc/example/clean_l_spanish.yml', 'clean_control',
                      'Texto limpo [token].', 'auto_safe', 'auto_safe', 'low',
                      0.99, 'ok', 0, ?)
            """,
            (now,),
        )
        for decision_id, family_key in enumerate(("family-a", "family-b"), start=1):
            self.conn.execute(
                """
                INSERT INTO ml_regenerative_review_decisions (
                    id, queue_type, item_key, segment_id, snapshot_id,
                    evidence_hash, decision, reason, reviewer,
                    contract_version, evidence_json, is_active,
                    created_at, updated_at
                ) VALUES (?, 'discovery', ?, ?, 7, ?, 'supports_pattern', NULL,
                          'test', 'test-v1', ?, 1, ?, ?)
                """,
                (
                    decision_id,
                    f"family:{family_key}:segment:{decision_id}",
                    decision_id,
                    f"{'a' if decision_id == 1 else 'b'}" * 64,
                    json.dumps({"family_key": family_key}),
                    now,
                    now,
                ),
            )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_groups_families_and_drafts_disabled_manifest_with_boundary_cases(self) -> None:
        result = generate_provider_proposals(self.conn, discovery_run_id=7)

        self.assertEqual(result["actionable_family_count"], 2)
        self.assertEqual(result["proposal_count"], 1)
        self.assertEqual(result["confirmation_write_count"], 0)
        self.assertEqual(result["output_write_count"], 0)
        proposal = result["proposals"][0]
        self.assertEqual(proposal["family_count"], 2)
        self.assertEqual(proposal["segment_count"], 7)
        self.assertFalse(proposal["manifest_draft"]["enabled"])
        self.assertTrue(result["supervised_routing"])
        self.assertEqual(result["correction_lane_count"], 1)
        self.assertEqual(proposal["correction_lane"]["id"], "structure_tokens")
        self.assertEqual(proposal["human_review"]["supports_pattern"], 2)
        self.assertEqual(
            proposal["contract"]["transformation"]["implementation_status"],
            "not_implemented",
        )
        kinds = {item["case_kind"] for item in proposal["cases"]}
        self.assertEqual(kinds, {"positive", "negative", "boundary"})

    def test_persistence_is_idempotent_for_the_same_discovery_run(self) -> None:
        result = generate_provider_proposals(self.conn, discovery_run_id=7)

        first_run_id = persist_provider_proposals(self.conn, result)
        second_run_id = persist_provider_proposals(self.conn, result)

        self.assertEqual(first_run_id, second_run_id)
        proposal_count = self.conn.execute(
            "SELECT COUNT(*) FROM ml_quality_provider_proposals WHERE run_id = ?",
            (first_run_id,),
        ).fetchone()[0]
        case_count = self.conn.execute(
            """
            SELECT COUNT(*)
            FROM ml_quality_provider_proposal_cases item
            JOIN ml_quality_provider_proposals proposal ON proposal.id = item.proposal_id
            WHERE proposal.run_id = ?
            """,
            (first_run_id,),
        ).fetchone()[0]
        self.assertEqual(proposal_count, 1)
        self.assertEqual(case_count, len(result["proposals"][0]["cases"]))

    def test_unreviewed_or_rejected_families_do_not_create_correction_proposals(self) -> None:
        self.conn.execute("DELETE FROM ml_regenerative_review_decisions")
        self.conn.execute(
            """
            INSERT INTO ml_regenerative_review_decisions (
                queue_type, item_key, segment_id, snapshot_id, evidence_hash,
                decision, reviewer, contract_version, evidence_json,
                is_active, created_at, updated_at
            ) VALUES ('discovery', 'family:family-a:segment:1', 1, 7, ?,
                      'contradicts_pattern', 'test', 'test-v1', ?, 1, ?, ?)
            """,
            (
                "c" * 64,
                json.dumps({"family_key": "family-a"}),
                "2026-07-20T00:00:00Z",
                "2026-07-20T00:00:00Z",
            ),
        )
        self.conn.commit()

        result = generate_provider_proposals(self.conn, discovery_run_id=7)

        self.assertEqual(result["actionable_family_count"], 0)
        self.assertEqual(result["proposal_count"], 0)

    def test_previous_snapshot_votes_feed_the_next_analysis_run(self) -> None:
        reviews = _discovery_review_by_family(self.conn, discovery_run_id=8)

        self.assertEqual(reviews["family-a"]["supports_pattern"], 1)
        self.assertEqual(reviews["family-b"]["supports_pattern"], 1)
        self.assertTrue(reviews["family-a"]["route_to_correction"])


if __name__ == "__main__":
    unittest.main()
