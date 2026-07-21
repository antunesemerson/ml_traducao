from __future__ import annotations

import sqlite3
import unittest

from dashboard import backend


class OperationalClosurePayloadTest(unittest.TestCase):
    def test_closed_package_is_independent_from_quality_debt(self) -> None:
        payload = backend._operational_closure_payload(
            {
                "run_id": 42,
                "total_segments": 100,
                "closed_count": 100,
                "pending_count": 0,
                "needs_apply": 0,
            },
            99.93,
        )

        self.assertTrue(payload["is_closed"])
        self.assertEqual(payload["status"], "closed")
        self.assertEqual(payload["blocking_reasons"], [])


class CurrentLedgerSemanticsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE segment_state_runs (
              id INTEGER PRIMARY KEY,
              pending_count INTEGER,
              output_apply_pending_count INTEGER,
              finished_at TEXT
            );
            CREATE TABLE ml_issue_ledger_runs (
              id INTEGER PRIMARY KEY,
              segment_state_run_id INTEGER,
              finished_at TEXT
            );
            CREATE TABLE segment_state_items (run_id INTEGER, segment_id INTEGER);
            CREATE TABLE ml_issue_ledger_items (
              run_id INTEGER,
              segment_id INTEGER,
              state_group TEXT,
              status TEXT,
              issue_family TEXT,
              issue_kind TEXT,
              issue_severity TEXT,
              agent_key TEXT,
              proposed_action TEXT
            );
            INSERT INTO segment_state_runs VALUES
              (76, 5, 0, '2026-07-19T00:00:00Z'),
              (77, 0, 0, '2026-07-20T00:00:00Z');
            INSERT INTO ml_issue_ledger_runs VALUES
              (8, 76, '2026-07-19T00:01:00Z');
            """
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_stale_ledger_is_not_attributed_to_current_closed_snapshot(self) -> None:
        self.assertEqual(backend._latest_ledger_run_id(self.conn, 77), 0)
        payload = backend._pending_actionability_payload(self.conn, 77)

        self.assertEqual(payload["status"], "not_applicable")
        self.assertEqual(payload["ledger_run_id"], 0)
        self.assertEqual(payload["instrumented_pending"], 0)
        focus = backend._pending_next_focus_payload({}, [], payload, 0)
        self.assertEqual(focus["label"], "quality_debt_clear")


class PromotionProviderHealthPayloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE ml_quality_pattern_families (
              id INTEGER PRIMARY KEY,
              provider_id TEXT
            );
            CREATE TABLE ml_quality_pattern_observations (
              id INTEGER PRIMARY KEY,
              run_id INTEGER,
              family_id INTEGER,
              status TEXT,
              segment_count INTEGER,
              metrics_json TEXT
            );
            CREATE TABLE ml_quality_shadow_runs (
              id INTEGER PRIMARY KEY,
              source_rule_version TEXT,
              score_run_id INTEGER,
              record_count INTEGER,
              eligible_count INTEGER,
              status TEXT,
              finished_at TEXT
            );
            CREATE TABLE ml_pairwise_quality_runs (
              id INTEGER PRIMARY KEY,
              evidence_type TEXT,
              source_rule_version TEXT,
              source_score_run_id INTEGER,
              candidate_count INTEGER,
              inserted_count INTEGER,
              reused_count INTEGER,
              training_eligible_count INTEGER,
              promotion_eligible_count INTEGER,
              finished_at TEXT
            );
            CREATE TABLE ml_pairwise_quality_evidence (
              id INTEGER PRIMARY KEY,
              segment_id INTEGER,
              evidence_type TEXT,
              last_run_id INTEGER,
              promotion_eligible INTEGER,
              baseline_text TEXT,
              candidate_text TEXT
            );
            CREATE TABLE output_segments (
              segment_id INTEGER PRIMARY KEY,
              portuguese_text TEXT
            );

            INSERT INTO ml_quality_pattern_families VALUES
              (1, 'provider_one'),
              (2, NULL),
              (3, 'provider_one');
            INSERT INTO ml_quality_pattern_observations VALUES
              (1, 9, 1, 'covered_by_provider', 4, '{"operational_segment_count": 4}'),
              (2, 9, 2, 'new_candidate', 3, '{"operational_segment_count": 3}'),
              (3, 9, 3, 'closed_observation', 8, '{"operational_segment_count": 0}');
            INSERT INTO ml_quality_shadow_runs VALUES
              (7, 'quality_provider_one_shadow_v1', 20, 10, 2, 'completed', '2026-07-20T00:00:00Z');
            INSERT INTO ml_pairwise_quality_runs VALUES
              (11, 'evidence_one', 'quality_provider_one_shadow_v1', 20,
               2, 2, 0, 1, 1, '2026-07-20T00:01:00Z');
            INSERT INTO ml_pairwise_quality_evidence VALUES
              (15, 101, 'evidence_one', 11, 1, 'old', 'new');
            INSERT INTO output_segments VALUES (101, 'old');
            """
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_current_epoch_funnel_and_pattern_coverage_are_db_backed(self) -> None:
        payload = backend._quality_promotion_provider_health_payload(
            self.conn,
            {"instrumented": True, "status": "completed", "run_id": 9, "score_run_id": 20},
            {"id": 13, "output_score_run_id": 20},
            provider_manifests=[
                {
                    "provider_id": "provider_one",
                    "label": "Provider one",
                    "priority": 100,
                    "evidence_type": "evidence_one",
                    "shadow_script": "pipeline/quality_provider_one_shadow.py",
                }
            ],
        )

        self.assertEqual(payload["status"], "healthy")
        self.assertEqual(payload["executed_provider_count"], 1)
        self.assertEqual(payload["inspected_count"], 10)
        self.assertEqual(payload["shadow_eligible_count"], 2)
        self.assertEqual(payload["evidence_count"], 2)
        self.assertEqual(payload["promotion_ready_count"], 1)
        self.assertEqual(payload["uncovered_actionable_family_count"], 1)
        self.assertEqual(payload["uncovered_actionable_reach"], 3)
        self.assertEqual(payload["closed_family_count"], 1)
        self.assertEqual(payload["coverage_rate"], 50.0)


class QualityProviderProposalsPayloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE ml_quality_provider_proposal_runs (
              id INTEGER PRIMARY KEY,
              run_key TEXT,
              rule_version TEXT,
              quality_epoch_id INTEGER,
              discovery_run_id INTEGER,
              score_run_id INTEGER,
              actionable_family_count INTEGER,
              proposal_count INTEGER,
              positive_case_count INTEGER,
              negative_case_count INTEGER,
              boundary_case_count INTEGER,
              status TEXT,
              summary_json TEXT,
              finished_at TEXT,
              updated_at TEXT
            );
            CREATE TABLE ml_quality_provider_proposals (
              id INTEGER PRIMARY KEY,
              run_id INTEGER,
              proposal_key TEXT,
              provider_id TEXT,
              evidence_type TEXT,
              label TEXT,
              issue_type TEXT,
              token_context TEXT,
              status TEXT,
              priority REAL,
              confidence REAL,
              family_count INTEGER,
              segment_count INTEGER,
              manifest_draft_json TEXT,
              contract_json TEXT
            );
            CREATE TABLE ml_quality_provider_proposal_cases (
              id INTEGER PRIMARY KEY,
              proposal_id INTEGER,
              case_kind TEXT,
              segment_id INTEGER,
              relative_path TEXT,
              source_key TEXT,
              input_text TEXT,
              expected_behavior TEXT,
              assertions_json TEXT
            );
            INSERT INTO ml_quality_provider_proposal_runs VALUES
              (3, 'run-3', 'proposal-v1', 13, 9, 20, 2, 1, 1, 1, 1,
               'completed', '{}', '2026-07-20T00:00:00Z', '2026-07-20T00:00:00Z');
            INSERT INTO ml_quality_provider_proposals VALUES
              (4, 3, 'proposal-4', 'spacing_issue', 'deterministic_spacing_issue_repair',
               'Spacing issue', 'spacing_issue', 'token_boundary',
               'draft_review_required', 88, 0.84, 2, 7,
               '{"enabled": false}',
               '{"selector": {"file_families": ["dlc"]}}');
            INSERT INTO ml_quality_provider_proposal_cases VALUES
              (1, 4, 'positive', 10, 'dlc/example.yml', 'key', 'text',
               'resolve_issue_or_reject_with_explicit_reason', '["issue_removed"]'),
              (2, 4, 'negative', 11, 'dlc/example.yml', 'clean', 'clean',
               'leave_unchanged', '["candidate_equals_input"]'),
              (3, 4, 'boundary', 10, 'dlc/example.yml', 'key', 'text',
               'deterministic_and_token_safe_or_explicitly_rejected', '["idempotent"]');
            """
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_latest_generation_exposes_disabled_draft_and_case_counts(self) -> None:
        payload = backend._quality_provider_proposals_payload(self.conn)

        self.assertTrue(payload["instrumented"])
        self.assertEqual(payload["run_id"], 3)
        self.assertEqual(payload["draft_count"], 1)
        proposal = payload["proposals"][0]
        self.assertFalse(proposal["manifest_draft"]["enabled"])
        self.assertEqual(proposal["positive_case_count"], 1)
        self.assertEqual(proposal["negative_case_count"], 1)
        self.assertEqual(proposal["boundary_case_count"], 1)
        self.assertEqual(len(proposal["sample_cases"]), 3)


class QualityDebtPayloadTest(unittest.TestCase):
    def test_only_specific_unresolved_evidence_counts_as_debt(self) -> None:
        payload = backend._quality_debt_payload(
            {
                "instrumented": True,
                "status": "completed",
                "ignored_score_only_count": 400,
            },
            {
                "instrumented": True,
                "status": "healthy",
                "uncovered_actionable_family_count": 0,
                "uncovered_actionable_reach": 0,
                "promotion_ready_count": 0,
                "monitoring_family_count": 0,
                "closed_family_count": 12,
            },
            {
                "summary": {"effective_score_regressions": 0},
                "low_score_cohorts": {
                    "actionable": 0,
                    "evidence_flagged": 7,
                    "lifecycle_closed": 7,
                    "lifecycle_locked": 3,
                    "informational": 393,
                },
                "calibration_review": {"policy_decision": "skip", "pending_count": 0},
            },
            {"summary": {"known_open_findings": 0}},
        )

        self.assertEqual(payload["status"], "clear")
        self.assertFalse(payload["has_actionable_debt"])
        self.assertEqual(payload["actionable_signal_count"], 0)
        self.assertEqual(payload["risk_watch"]["actionable_low_score_count"], 0)
        self.assertEqual(payload["risk_watch"]["evidence_flagged_low_score_count"], 7)
        self.assertEqual(payload["risk_watch"]["lifecycle_closed_low_score_count"], 7)
        self.assertEqual(payload["risk_watch"]["locked_low_score_count"], 3)
        self.assertFalse(payload["risk_watch"]["counted_as_quality_debt"])
        self.assertFalse(payload["blocks_operational_closure"])


if __name__ == "__main__":
    unittest.main()
