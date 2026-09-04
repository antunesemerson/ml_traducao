from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from quality_pattern_discovery import discover_patterns  # noqa: E402


class QualityPatternDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE source_segments (
              id INTEGER PRIMARY KEY,
              relative_path TEXT,
              source_line_number INTEGER,
              source_key TEXT,
              spanish_text TEXT,
              english_text TEXT,
              old_text TEXT,
              is_active INTEGER
            );
            CREATE TABLE ml_score_runs (
              id INTEGER PRIMARY KEY,
              candidate_text_source TEXT
            );
            CREATE TABLE ml_score_items (
              id INTEGER PRIMARY KEY,
              run_id INTEGER,
              segment_id INTEGER,
              relative_path TEXT,
              source_key TEXT,
              candidate_text TEXT,
              model_safe_probability REAL,
              final_action TEXT,
              risk_class TEXT,
              token_status TEXT,
              issue_count INTEGER,
              issues_json TEXT
            );
            CREATE TABLE quality_epochs (
              id INTEGER PRIMARY KEY,
              epoch_key TEXT,
              scoring_contract_hash TEXT,
              output_score_run_id INTEGER,
              status TEXT
            );
            INSERT INTO ml_score_runs VALUES (10, 'output');
            INSERT INTO quality_epochs VALUES (4, 'epoch-4', 'contract-a', 10, 'scored');
            """
        )

    def tearDown(self) -> None:
        self.conn.close()

    def _add_item(
        self,
        segment_id: int,
        *,
        score: float,
        issue_type: str | None = None,
        severity: str = "high",
        token_status: str = "ok",
        final_action: str = "needs_human",
        risk_class: str = "medium",
        candidate_text: str = "Texto candidato",
    ) -> None:
        relative_path = "dlc/ep1/example_l_spanish.yml"
        self.conn.execute(
            "INSERT INTO source_segments VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (
                segment_id,
                relative_path,
                segment_id,
                f"key_{segment_id}",
                "Texto espanhol",
                "English text",
                candidate_text,
            ),
        )
        issues = (
            [{"code": issue_type, "severity": severity, "message": "evidence"}]
            if issue_type
            else []
        )
        self.conn.execute(
            """
            INSERT INTO ml_score_items VALUES (?, 10, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                segment_id,
                segment_id,
                relative_path,
                f"key_{segment_id}",
                candidate_text,
                score,
                final_action,
                risk_class,
                token_status,
                len(issues),
                json.dumps(issues),
            ),
        )

    def test_discovers_package_wide_evidence_and_ignores_score_only_rows(self) -> None:
        self._add_item(1, score=0.20, issue_type="spanish_residue")
        self._add_item(2, score=0.74, issue_type="spanish_residue")
        self._add_item(3, score=0.88, issue_type="spanish_residue")
        self._add_item(4, score=0.12)
        self._add_item(
            5,
            score=0.31,
            token_status="mismatch",
            final_action="blocked_structure",
            risk_class="critical",
            candidate_text="[ROOT.Char.GetName]texto",
        )
        self._add_item(6, score=0.30, issue_type="space_before_punctuation")
        self.conn.commit()

        result = discover_patterns(
            self.conn,
            score_run_id=10,
            provider_coverage={
                "space_before_punctuation": {
                    "provider_id": "token_punctuation_boundary",
                    "evidence_type": "deterministic_token_punctuation_boundary_repair",
                }
            },
        )

        spanish = next(item for item in result["families"] if item["issue_type"] == "spanish_residue")
        covered = next(
            item for item in result["families"] if item["issue_type"] == "space_before_punctuation"
        )
        structural = next(
            item for item in result["families"] if item["evidence_kind"] == "structural_block"
        )
        self.assertEqual(spanish["segment_count"], 3)
        self.assertEqual(spanish["low_score_count"], 1)
        self.assertEqual(spanish["status"], "new_candidate")
        self.assertEqual(covered["status"], "covered_by_provider")
        self.assertEqual(structural["status"], "monitoring")
        self.assertEqual(result["ignored_score_only_count"], 1)
        self.assertEqual(result["evidence_segment_count"], 5)
        self.assertEqual(result["confirmation_write_count"], 0)
        self.assertEqual(result["output_write_count"], 0)
        self.assertEqual(result["score_write_count"], 0)

    def test_discovery_sample_preserves_full_candidate_text(self) -> None:
        candidate_text = "Texto longo " + ("completo " * 320)
        self._add_item(
            1,
            score=0.20,
            issue_type="embedded_gender_token_fragment",
            candidate_text=candidate_text,
        )
        self.conn.commit()

        result = discover_patterns(self.conn, score_run_id=10, provider_coverage={})

        self.assertEqual(result["families"][0]["samples"][0]["candidate_text"], candidate_text)

    def test_prior_observation_marks_uncovered_family_as_recurring(self) -> None:
        for segment_id in (1, 2, 3):
            self._add_item(segment_id, score=0.4, issue_type="english_residue")
        self.conn.executescript(
            """
            CREATE TABLE ml_quality_pattern_discovery_runs (
              id INTEGER PRIMARY KEY,
              run_key TEXT
            );
            CREATE TABLE ml_quality_pattern_families (
              id INTEGER PRIMARY KEY,
              family_key TEXT,
              status TEXT,
              provider_id TEXT,
              evidence_type TEXT,
              observation_count INTEGER
            );
            CREATE TABLE ml_quality_pattern_observations (
              family_id INTEGER,
              run_id INTEGER
            );
            """
        )
        initial = discover_patterns(
            self.conn,
            score_run_id=10,
            provider_coverage={},
        )
        family_key = initial["families"][0]["family_key"]
        self.conn.execute("INSERT INTO ml_quality_pattern_discovery_runs VALUES (90, 'older-run')")
        self.conn.execute(
            "INSERT INTO ml_quality_pattern_families VALUES (70, ?, 'new_candidate', NULL, NULL, 1)",
            (family_key,),
        )
        self.conn.execute("INSERT INTO ml_quality_pattern_observations VALUES (70, 90)")
        self.conn.commit()

        repeated = discover_patterns(
            self.conn,
            score_run_id=10,
            provider_coverage={},
        )

        self.assertEqual(repeated["families"][0]["status"], "recurring_candidate")
        self.assertEqual(repeated["recurring_family_count"], 1)
        self.assertEqual(repeated["new_family_count"], 0)

    def test_closed_lifecycle_evidence_is_historical_not_actionable(self) -> None:
        for segment_id in (1, 2, 3):
            self._add_item(segment_id, score=0.3, issue_type="english_residue")
        self.conn.executescript(
            """
            CREATE TABLE segment_state_runs (
              id INTEGER PRIMARY KEY,
              finished_at TEXT
            );
            CREATE TABLE segment_state_items (
              run_id INTEGER,
              segment_id INTEGER,
              state_group TEXT,
              locked INTEGER,
              confirmed_matches_output INTEGER,
              needs_output_apply INTEGER,
              is_closed INTEGER
            );
            INSERT INTO segment_state_runs VALUES (20, '2026-07-19T00:00:00Z');
            INSERT INTO segment_state_items VALUES
              (20, 1, 'closed', 1, 1, 0, 1),
              (20, 2, 'closed', 1, 1, 0, 1),
              (20, 3, 'closed', 1, 1, 0, 1);
            """
        )

        result = discover_patterns(self.conn, score_run_id=10, provider_coverage={})

        family = result["families"][0]
        self.assertEqual(family["status"], "closed_observation")
        self.assertEqual(family["segment_count"], 3)
        self.assertEqual(family["operational_segment_count"], 0)
        self.assertEqual(family["closed_segment_count"], 3)
        self.assertEqual(result["closed_family_count"], 1)
        self.assertEqual(result["actionable_family_count"], 0)

    def test_negative_independent_audit_reopens_a_discovery_blind_spot(self) -> None:
        self._add_item(
            1,
            score=0.95,
            candidate_text="Texto aparentemente seguro com erro semântico",
        )
        self.conn.executescript(
            """
            CREATE TABLE output_segments (
              segment_id INTEGER PRIMARY KEY,
              portuguese_text TEXT
            );
            CREATE TABLE local_learning_candidates (
              id INTEGER PRIMARY KEY,
              segment_id INTEGER,
              origin TEXT,
              human_label TEXT,
              local_status TEXT,
              current_output_text TEXT,
              reason TEXT,
              reviewed_at TEXT
            );
            INSERT INTO output_segments VALUES (
              1, 'Texto aparentemente seguro com erro semântico'
            );
            INSERT INTO local_learning_candidates VALUES (
              1, 1, 'manual_low_score_calibration', 'semantic_error', 'reviewed',
              'Texto aparentemente seguro com erro semântico',
              'A auditoria confirmou que o sentido está incorreto.',
              '2026-08-31T00:00:00Z'
            );
            """
        )
        self.conn.commit()

        result = discover_patterns(self.conn, score_run_id=10, provider_coverage={})

        blind_spot = next(
            family
            for family in result["families"]
            if family["evidence_kind"] == "human_audit_blind_spot"
        )
        self.assertEqual(result["audit_blind_spot_count"], 1)
        self.assertEqual(blind_spot["issue_type"], "semantic_error")
        self.assertEqual(blind_spot["status"], "new_candidate")
        self.assertGreaterEqual(blind_spot["confidence"], 0.92)

    def test_current_angular_quote_evidence_overrides_stale_residual_spanish_audit(self) -> None:
        candidate_text = "«Texto em português com aspas angulares»"
        self._add_item(
            1,
            score=0.10,
            issue_type="spanish_angular_quotes",
            severity="medium",
            candidate_text=candidate_text,
        )
        self.conn.executescript(
            """
            CREATE TABLE output_segments (
              segment_id INTEGER PRIMARY KEY,
              portuguese_text TEXT
            );
            CREATE TABLE local_learning_candidates (
              id INTEGER PRIMARY KEY,
              segment_id INTEGER,
              origin TEXT,
              human_label TEXT,
              local_status TEXT,
              current_output_text TEXT,
              reason TEXT,
              reviewed_at TEXT
            );
            INSERT INTO output_segments VALUES (
              1, '«Texto em português com aspas angulares»'
            );
            INSERT INTO local_learning_candidates VALUES (
              1, 1, 'manual_low_score_calibration', 'residual_spanish', 'reviewed',
              '«Texto em português com aspas angulares»', NULL,
              '2026-09-01T00:00:00Z'
            );
            """
        )
        self.conn.commit()

        result = discover_patterns(self.conn, score_run_id=10, provider_coverage={})

        matching = [
            family
            for family in result["families"]
            if family["samples"][0]["segment_id"] == 1
        ]
        self.assertEqual(len(matching), 2)
        self.assertEqual(
            {family["evidence_kind"] for family in matching},
            {"explicit_issue", "human_audit_blind_spot"},
        )
        self.assertEqual(
            {family["issue_type"] for family in matching},
            {"spanish_angular_quotes"},
        )
        blind_spot = next(
            family
            for family in matching
            if family["evidence_kind"] == "human_audit_blind_spot"
        )
        self.assertEqual(blind_spot["status"], "new_candidate")
        self.assertEqual(result["audit_blind_spot_count"], 1)


if __name__ == "__main__":
    unittest.main()
