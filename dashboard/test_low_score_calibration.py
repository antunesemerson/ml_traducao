from __future__ import annotations

import json
import hashlib
import sqlite3
import unittest

from dashboard import backend


class ManualLowScoreCalibrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE source_segments (
              id INTEGER PRIMARY KEY,
              relative_path TEXT,
              source_key TEXT,
              source_line_number INTEGER,
              english_text TEXT,
              spanish_text TEXT,
              old_text TEXT,
              is_active INTEGER
            );
            CREATE TABLE output_segments (
              segment_id INTEGER PRIMARY KEY,
              portuguese_text TEXT
            );
            CREATE TABLE ml_score_items (
              run_id INTEGER,
              segment_id INTEGER,
              candidate_text TEXT,
              model_safe_probability REAL,
              model_confidence REAL,
              final_action TEXT,
              risk_class TEXT,
              token_status TEXT,
              issue_count INTEGER,
              high_issue_count INTEGER
            );
            CREATE TABLE segment_state_items (
              run_id INTEGER,
              segment_id INTEGER,
              is_closed INTEGER,
              needs_output_apply INTEGER
            );
            CREATE TABLE segment_confirmations (
              segment_id INTEGER PRIMARY KEY,
              confirmation_level TEXT,
              confirmed_text TEXT
            );
            CREATE TABLE local_learning_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              mode TEXT,
              limit_count INTEGER,
              auto_confidence_threshold REAL,
              candidate_count INTEGER,
              high_confidence_count INTEGER,
              pending_human_count INTEGER,
              status TEXT,
              notes TEXT,
              started_at TEXT,
              finished_at TEXT,
              updated_at TEXT
            );
            CREATE TABLE local_learning_candidates (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id INTEGER,
              feedback_id INTEGER,
              suggestion_id INTEGER,
              offline_proposal_id INTEGER,
              segment_id INTEGER,
              relative_path TEXT,
              source_key TEXT,
              source_line_number INTEGER,
              english_text TEXT,
              spanish_text TEXT,
              old_text TEXT,
              current_output_text TEXT,
              suggested_text TEXT,
              suggested_hash TEXT,
              queue_source TEXT,
              focus_group TEXT,
              source_language TEXT,
              origin TEXT,
              match_type TEXT,
              match_score REAL,
              token_status TEXT,
              suggestion_status TEXT,
              local_confidence_score REAL,
              local_status TEXT,
              human_label TEXT,
              corrected_text TEXT,
              reason TEXT,
              reviewer TEXT,
              reviewed_at TEXT,
              reasons_json TEXT,
              created_at TEXT,
              updated_at TEXT
            );

            INSERT INTO source_segments VALUES
              (1, 'dlc/ep3/events_l_spanish.yml', 'human', 1, 'Good', 'Bueno', 'Antigo', 1),
              (2, 'contracts/tasks_l_spanish.yml', 'safe', 2, 'Safe', 'Seguro', 'Antigo', 1),
              (3, 'interactions_l_spanish.yml', 'preserved', 3, 'Keep', 'Conservar', 'Preservado', 1),
              (4, 'events/bad_l_spanish.yml', 'issue', 4, 'Bad', 'Malo', 'Ruim', 1),
              (5, 'events/token_l_spanish.yml', 'token', 5, 'Token', 'Token', 'Token', 1),
              (6, 'events/open_l_spanish.yml', 'open', 6, 'Open', 'Abierto', 'Aberto', 1),
              (7, 'events/stale_l_spanish.yml', 'stale', 7, 'Stale', 'Viejo', 'Antigo', 1);

            INSERT INTO output_segments VALUES
              (1, 'Bom'),
              (2, 'Seguro'),
              (3, 'Preservado'),
              (4, 'Ruim'),
              (5, 'Token'),
              (6, 'Aberto'),
              (7, 'Saida atual');

            INSERT INTO ml_score_items VALUES
              (10, 1, 'Bom', 0.02, 0.9, 'needs_human', 'medium', 'ok', 0, 0),
              (10, 2, 'Seguro', 0.03, 0.9, 'auto_safe', 'low', 'ok', 0, 0),
              (10, 3, 'Preservado', 0.04, 0.8, 'needs_human', 'medium', 'ok', 0, 0),
              (10, 4, 'Ruim', 0.01, 0.9, 'needs_autofix', 'high', 'ok', 1, 1),
              (10, 5, 'Token', 0.01, 0.9, 'blocked_structure', 'high', 'mismatch', 0, 0),
              (10, 6, 'Aberto', 0.01, 0.9, 'auto_safe', 'low', 'ok', 0, 0),
              (10, 7, 'Saida antiga', 0.01, 0.9, 'auto_safe', 'low', 'ok', 0, 0);

            INSERT INTO segment_state_items VALUES
              (20, 1, 1, 0),
              (20, 2, 1, 0),
              (20, 3, 1, 0),
              (20, 4, 1, 0),
              (20, 5, 1, 0),
              (20, 6, 0, 0),
              (20, 7, 1, 0);

            INSERT INTO segment_confirmations VALUES
              (1, 'human_confirmed', 'Bom');
            """
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_dynamic_holdout_payload_includes_old_baseline_text(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE ml_dynamic_score_holdout_runs (
              id INTEGER PRIMARY KEY,
              rule_version TEXT,
              model_run_id INTEGER,
              score_run_id INTEGER,
              segment_state_run_id INTEGER,
              population_count INTEGER,
              excluded_reviewed_count INTEGER,
              excluded_training_count INTEGER,
              target_count INTEGER,
              selected_count INTEGER,
              status TEXT,
              selection_json TEXT,
              created_at TEXT,
              updated_at TEXT
            );
            CREATE TABLE ml_dynamic_score_holdout_items (
              id INTEGER PRIMARY KEY,
              run_id INTEGER,
              display_order INTEGER,
              segment_id INTEGER,
              text_hash TEXT,
              calibration_lane TEXT,
              calibration_group TEXT,
              probability_band TEXT,
              text_length INTEGER,
              dynamic_token_count INTEGER,
              stratum_population INTEGER,
              stratum_selected INTEGER,
              sampling_weight REAL,
              created_at TEXT
            );
            INSERT INTO ml_dynamic_score_holdout_items VALUES (
              1, 1, 1, 1, 'hash-1', 'human_confirmed_low_score', 'dlc/ep3',
              '0-10%', 3, 0, 1, 1, 1.0, '2026-08-26T00:00:00'
            );
            """
        )
        self.conn.execute(
            """
            INSERT INTO ml_dynamic_score_holdout_runs VALUES (
              1, ?, 1, 10, 20, 1, 0, 0, 1, 1, 'ready_for_review', '{}',
              '2026-08-26T00:00:00', '2026-08-26T00:00:00'
            )
            """,
            (backend.DYNAMIC_SCORE_HOLDOUT_RULE_VERSION,),
        )
        self.conn.commit()

        payload = backend._dynamic_score_holdout_payload(
            self.conn,
            score_run_id=10,
            segment_state_run_id=20,
        )

        self.assertEqual(payload["items"][0]["old_text"], "Antigo")

    def test_payload_only_samples_clean_current_closed_outputs(self) -> None:
        payload = backend._manual_low_score_calibration_payload(
            self.conn,
            score_run_id=10,
            segment_state_run_id=20,
            per_lane_limit=10,
        )

        self.assertTrue(payload["instrumented"])
        self.assertEqual(payload["eligible_count"], 3)
        self.assertEqual(
            payload["lane_counts"],
            {
                "deterministic_safe_low_score": 1,
                "human_confirmed_low_score": 1,
                "preserved_text_low_score": 1,
            },
        )
        self.assertEqual(payload["eligible_lane_counts"], payload["lane_counts"])
        self.assertEqual(
            payload["lane_sample_counts"],
            {
                "deterministic_safe_low_score": 1,
                "human_confirmed_low_score": 1,
                "preserved_text_low_score": 1,
            },
        )
        self.assertEqual(payload["lane_pending_counts"], payload["lane_sample_counts"])
        self.assertEqual({item["segment_id"] for item in payload["items"]}, {1, 2, 3})
        self.assertEqual(payload["pending_count"], 3)

    def test_payload_separates_eligible_population_from_sample_and_pending(self) -> None:
        self.conn.executescript(
            """
            INSERT INTO source_segments VALUES
              (8, 'custom_localization/a_l_spanish.yml', 'safe_2', 8, 'A', 'A', 'A', 1),
              (9, 'custom_localization/b_l_spanish.yml', 'safe_3', 9, 'B', 'B', 'B', 1);
            INSERT INTO output_segments VALUES
              (8, 'A'),
              (9, 'B');
            INSERT INTO ml_score_items VALUES
              (10, 8, 'A', 0.01, 0.9, 'auto_safe', 'low', 'ok', 0, 0),
              (10, 9, 'B', 0.02, 0.9, 'auto_safe', 'low', 'ok', 0, 0);
            INSERT INTO segment_state_items VALUES
              (20, 8, 1, 0),
              (20, 9, 1, 0);
            """
        )
        self.conn.commit()

        payload = backend._manual_low_score_calibration_payload(
            self.conn,
            score_run_id=10,
            segment_state_run_id=20,
            per_lane_limit=1,
        )

        self.assertEqual(payload["eligible_lane_counts"]["deterministic_safe_low_score"], 3)
        self.assertEqual(payload["lane_sample_counts"]["deterministic_safe_low_score"], 1)
        self.assertEqual(payload["lane_pending_counts"]["deterministic_safe_low_score"], 1)
        self.assertEqual(payload["sample_count"], 3)
        self.assertEqual(payload["pending_count"], 3)

    def test_es_helper_repair_queue_is_read_only_and_excludes_stale_rows(self) -> None:
        helper_text = (
            "[actor.Custom('ES_ElLa')] "
            "[Select_CString(actor.IsFemale, 'la anciana', 'el anciano')]"
        )
        self.conn.executemany(
            "INSERT INTO source_segments VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            [
                (10, "contracts/a_l_spanish.yml", "repair", 10, "A", "A", "A"),
                (11, "contracts/b_l_spanish.yml", "clean", 11, "B", "B", "B"),
                (12, "events/c_l_spanish.yml", "outside", 12, "C", "C", "C"),
                (13, "contracts/d_l_spanish.yml", "stale", 13, "D", "D", "D"),
                (14, "contracts/e_l_spanish.yml", "apply", 14, "E", "E", "E"),
            ],
        )
        self.conn.executemany(
            "INSERT INTO output_segments VALUES (?, ?)",
            [(segment_id, helper_text) for segment_id in range(10, 15)],
        )
        self.conn.executemany(
            "INSERT INTO ml_score_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (10, 10, helper_text, 0.02, 0.9, "needs_autofix", "high", "ok", 1, 1),
                (10, 11, helper_text, 0.03, 0.9, "auto_safe", "low", "ok", 0, 0),
                (10, 12, helper_text, 0.04, 0.9, "needs_autofix", "high", "ok", 1, 1),
                (10, 13, "candidate antigo", 0.05, 0.9, "needs_autofix", "high", "ok", 1, 1),
                (10, 14, helper_text, 0.06, 0.9, "needs_autofix", "high", "ok", 1, 1),
            ],
        )
        self.conn.executemany(
            "INSERT INTO segment_state_items VALUES (?, ?, ?, ?)",
            [
                (20, 10, 1, 0),
                (20, 11, 1, 0),
                (20, 12, 1, 0),
                (20, 13, 1, 0),
                (20, 14, 1, 1),
            ],
        )
        self.conn.commit()

        payload = backend._manual_es_helper_repair_payload(
            self.conn,
            score_run_id=10,
            segment_state_run_id=20,
        )

        self.assertTrue(payload["instrumented"])
        self.assertFalse(payload["operational_writes"])
        self.assertEqual(payload["total_count"], 1)
        self.assertEqual(payload["helper_counts"], {"ES_ElLa": 1})
        self.assertEqual(payload["route_counts"], {"spanish_literal": 1})
        self.assertEqual(payload["items"][0]["segment_id"], 10)
        self.assertEqual(payload["items"][0]["repair_priority"], "high")
        self.assertEqual(
            payload["items"][0]["detected_issues"][0]["code"],
            "spanish_residue_in_literal",
        )
        self.assertFalse(payload["items"][0]["operational_writes"])

    def test_decision_is_idempotent_and_does_not_change_operational_data(self) -> None:
        first = backend._record_manual_low_score_calibration_decision(
            self.conn,
            segment_id=1,
            review_label="correct",
            review_reason="Texto natural e semanticamente correto.",
            reviewer="test_reviewer",
            score_run_id=10,
            segment_state_run_id=20,
        )
        second = backend._record_manual_low_score_calibration_decision(
            self.conn,
            segment_id=1,
            review_label="contextual_exception",
            review_reason="Construção válida apenas neste contexto.",
            reviewer="test_reviewer",
            score_run_id=10,
            segment_state_run_id=20,
        )

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["candidate_id"], second["candidate_id"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM local_learning_candidates").fetchone()[0],
            1,
        )
        decision = self.conn.execute(
            "SELECT human_label, queue_source, origin FROM local_learning_candidates"
        ).fetchone()
        self.assertEqual(decision["human_label"], "contextual_exception")
        self.assertEqual(decision["queue_source"], backend.LOW_SCORE_CALIBRATION_QUEUE_SOURCE)
        self.assertEqual(decision["origin"], backend.LOW_SCORE_CALIBRATION_ORIGIN)
        self.assertEqual(
            self.conn.execute(
                "SELECT portuguese_text FROM output_segments WHERE segment_id = 1"
            ).fetchone()[0],
            "Bom",
        )
        state = self.conn.execute(
            "SELECT is_closed, needs_output_apply FROM segment_state_items WHERE segment_id = 1"
        ).fetchone()
        self.assertEqual(tuple(state), (1, 0))

    def test_reason_is_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "motivo"):
            backend._record_manual_low_score_calibration_decision(
                self.conn,
                segment_id=1,
                review_label="correct",
                review_reason="",
                reviewer="test_reviewer",
                score_run_id=10,
                segment_state_run_id=20,
            )

    def test_independent_audit_accepts_clean_high_score_segment(self) -> None:
        self.conn.execute(
            "INSERT INTO source_segments VALUES (8, 'events/high_l_spanish.yml', 'high', 8, 'Good', 'Bueno', 'Antigo', 1)"
        )
        self.conn.execute("INSERT INTO output_segments VALUES (8, 'Texto fluente')")
        self.conn.execute(
            "INSERT INTO ml_score_items VALUES (10, 8, 'Texto fluente', 0.95, 0.99, 'needs_human', 'low', 'ok', 0, 0)"
        )
        self.conn.execute("INSERT INTO segment_state_items VALUES (20, 8, 1, 0)")
        self.conn.commit()

        result = backend._record_manual_low_score_calibration_decision(
            self.conn,
            segment_id=8,
            review_label="semantic_error",
            review_reason="Texto fluente, mas com sentido incorreto no contexto.",
            reviewer="test_reviewer",
            score_run_id=10,
            segment_state_run_id=20,
        )

        self.assertTrue(result["created"])
        self.assertIn("discovery_blind_spot", result["learning_destinations"])
        self.assertTrue(result["calibration_lane"].startswith("independent_audit_"))

    def test_segmented_shadow_summary_is_read_from_sqlite(self) -> None:
        metadata = {
            "status": "shadow_ready_limited",
            "pattern": "exact_shared_glossary_token",
            "limited_evidence": True,
            "reviewed_training_match_count": 11,
            "reviewed_control_match_count": 1,
            "reviewed_boundary_sentinel_count": 1,
            "boundary_sentinel_changed_count": 0,
            "population_match_count": 38,
            "population_near_miss_count": 4,
            "population_match_issue_count": 0,
            "population_match_auto_safe_count": 38,
            "posterior": {"posterior_safe_probability": 12 / 13},
            "delta": {
                "brier": -0.052,
                "log_loss": -0.153,
                "separation": 0.084,
            },
            "gates": {"boundary_sentinel_unchanged": True},
            "operational_writes": False,
            "evaluated_at": "2026-07-24T11:44:05",
        }
        self.conn.execute(
            """
            CREATE TABLE ml_quality_shadow_runs (
              id INTEGER PRIMARY KEY,
              source_rule_version TEXT,
              score_run_id INTEGER,
              status TEXT,
              metadata_json TEXT,
              finished_at TEXT
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO ml_quality_shadow_runs (
              id, source_rule_version, score_run_id, status,
              metadata_json, finished_at
            ) VALUES (127, ?, 10, 'shadow_ready_limited', ?, ?)
            """,
            (
                backend.LOW_SCORE_SEGMENTED_SHADOW_RULE_VERSION,
                json.dumps(metadata),
                metadata["evaluated_at"],
            ),
        )
        self.conn.commit()

        payload = backend._manual_low_score_segmented_shadow_payload(
            self.conn,
            score_run_id=10,
        )

        self.assertTrue(payload["available"])
        self.assertEqual(payload["run_id"], 127)
        self.assertEqual(payload["population_match_count"], 38)
        self.assertEqual(payload["population_near_miss_count"], 4)
        self.assertAlmostEqual(payload["posterior_safe_probability"], 12 / 13)
        self.assertEqual(payload["boundary_sentinel_changed_count"], 0)
        self.assertFalse(payload["operational_writes"])
        history = backend._manual_low_score_segmented_shadows_payload(
            self.conn,
            score_run_id=10,
        )
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["run_id"], 127)

    def test_es_helper_repair_dry_run_is_read_only_and_loaded_from_sqlite(
        self,
    ) -> None:
        metadata = {
            "status": "completed",
            "proposal_ready_count": 1,
            "blocked_count": 1,
            "repair_count": 2,
            "post_validation_clean_count": 1,
            "token_integrity_ok_count": 1,
            "ready_for_apply_count": 0,
            "apply_count": 0,
            "operational_writes": False,
            "blocker_counts": {
                "no_allowlisted_literal_replacement": 1,
            },
        }
        proposal = {
            "segment_id": 10,
            "lane": "proposal_ready",
            "candidate_text": (
                "[Select_CString(actor.IsFemale, 'a anciã', 'o ancião')]"
            ),
            "repair_count": 2,
            "blockers": [],
            "token_integrity_ok": True,
            "ready_for_apply": False,
            "output_changed": False,
            "operational_writes": False,
        }
        blocked = {
            "segment_id": 11,
            "lane": "blocked_or_context",
            "candidate_text": "vossa tutora",
            "repair_count": 0,
            "blockers": [
                "no_allowlisted_literal_replacement",
                "no_change",
            ],
            "token_integrity_ok": False,
            "ready_for_apply": False,
            "output_changed": False,
            "operational_writes": False,
        }
        self.conn.executescript(
            """
            CREATE TABLE ml_quality_shadow_runs (
              id INTEGER PRIMARY KEY,
              source_rule_version TEXT,
              score_run_id INTEGER,
              record_count INTEGER,
              eligible_count INTEGER,
              status TEXT,
              metadata_json TEXT,
              finished_at TEXT
            );
            CREATE TABLE ml_quality_shadow_items (
              id INTEGER PRIMARY KEY,
              run_id INTEGER,
              segment_id INTEGER,
              lane TEXT,
              payload_json TEXT
            );
            """
        )
        self.conn.execute(
            """
            INSERT INTO ml_quality_shadow_runs (
              id, source_rule_version, score_run_id, record_count,
              eligible_count, status, metadata_json, finished_at
            ) VALUES (128, ?, 10, 2, 1, 'completed', ?, ?)
            """,
            (
                backend.ES_HELPER_REPAIR_DRY_RUN_RULE_VERSION,
                json.dumps(metadata),
                "2026-07-26T12:00:00",
            ),
        )
        self.conn.executemany(
            """
            INSERT INTO ml_quality_shadow_items (
              id, run_id, segment_id, lane, payload_json
            ) VALUES (?, 128, ?, ?, ?)
            """,
            [
                (1, 10, proposal["lane"], json.dumps(proposal)),
                (2, 11, blocked["lane"], json.dumps(blocked)),
            ],
        )
        self.conn.commit()

        payload = backend._es_helper_repair_dry_run_payload(
            self.conn,
            score_run_id=10,
        )

        self.assertTrue(payload["available"])
        self.assertEqual(payload["run_id"], 128)
        self.assertEqual(payload["record_count"], 2)
        self.assertEqual(payload["proposal_ready_count"], 1)
        self.assertEqual(payload["blocked_count"], 1)
        self.assertEqual(payload["repair_count"], 2)
        self.assertEqual(len(payload["items"]), 2)
        self.assertFalse(payload["operational_writes"])
        self.assertEqual(payload["ready_for_apply_count"], 0)
        self.assertEqual(payload["apply_count"], 0)
        self.assertFalse(payload["items"][0]["ready_for_apply"])

    def test_training_patterns_separate_context_from_real_defects(self) -> None:
        contract_helper = backend._manual_low_score_training_patterns(
            {
                "relative_path": "contracts/task_contracts_l_spanish.yml",
                "output_text": "[actor.Custom('ES_ElLa')] texto natural",
            }
        )
        redundant = backend._manual_low_score_training_patterns(
            {
                "relative_path": "activities/example_l_spanish.yml",
                "output_text": (
                    "[Select_CString(actor.IsLocalPlayer, 'continuará', 'continuará')]"
                ),
            }
        )
        residual = backend._manual_low_score_training_patterns(
            {
                "relative_path": "contracts/example_l_spanish.yml",
                "output_text": "Texto em #EMP estudio#! e #EMP esto#!.",
            }
        )
        glossary_text = "[Glossary( 'hellenothreskos', 'HELLENOTHRESKOS_GLOSS' )]"
        glossary = backend._manual_low_score_training_patterns(
            {
                "relative_path": "custom_localization/example_l_spanish.yml",
                "output_text": glossary_text,
                "english_text": glossary_text,
                "spanish_text": glossary_text,
            }
        )
        outside_glossary = backend._manual_low_score_training_patterns(
            {
                "relative_path": "dlc/ep3/example_l_spanish.yml",
                "output_text": glossary_text,
                "english_text": glossary_text,
                "spanish_text": glossary_text,
            }
        )
        suffix_only = backend._manual_low_score_training_patterns(
            {
                "relative_path": "contracts/example_l_spanish.yml",
                "output_text": "benévol[actor.Custom('ES_OA')]",
            }
        )

        self.assertEqual(
            contract_helper,
            [
                "contract_es_helper",
                "contract_es_article_preposition_helper",
            ],
        )
        self.assertEqual(
            suffix_only,
            [
                "contract_es_helper",
                "contract_es_oa_suffix_only",
            ],
        )
        self.assertEqual(redundant, ["redundant_select_cstring"])
        self.assertEqual(
            residual,
            ["accent_sensitive_spanish"],
        )
        self.assertEqual(glossary, ["exact_shared_glossary_token"])
        self.assertEqual(
            outside_glossary,
            ["outside_custom_glossary_token"],
        )


class GlossaryDisplayCaseCalibrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE output_segments (
              segment_id INTEGER PRIMARY KEY,
              portuguese_text TEXT
            );
            CREATE TABLE ml_quality_shadow_runs (
              id INTEGER PRIMARY KEY,
              source_rule_version TEXT,
              score_run_id INTEGER,
              record_count INTEGER,
              eligible_count INTEGER,
              status TEXT,
              metadata_json TEXT,
              finished_at TEXT
            );
            CREATE TABLE ml_quality_shadow_items (
              id INTEGER PRIMARY KEY,
              run_id INTEGER,
              segment_id INTEGER,
              lane TEXT,
              payload_json TEXT
            );
            CREATE TABLE glossary_display_case_policies (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              glossary_key TEXT NOT NULL UNIQUE COLLATE NOCASE,
              policy TEXT NOT NULL,
              reason TEXT NOT NULL,
              reviewer TEXT NOT NULL,
              source_shadow_run_id INTEGER,
              source_score_run_id INTEGER,
              evidence_segment_count INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            INSERT INTO ml_quality_shadow_runs (
              id, source_rule_version, score_run_id, record_count,
              eligible_count, status, metadata_json, finished_at
            ) VALUES (
              242, 'quality_glossary_display_case_shadow_v1', 405, 2,
              2, 'completed', '{}', '2026-07-30T12:00:00'
            );
            INSERT INTO output_segments(segment_id, portuguese_text)
            VALUES (1, 'texto um'), (2, 'texto dois');
            """
        )
        for item_id, segment_id in enumerate((1, 2), start=1):
            payload = {
                "segment_id": segment_id,
                "relative_path": "events/example_l_spanish.yml",
                "source_key": f"example.{segment_id}",
                "human_locked": False,
                "glossary_keys": ["ROMAIOS_GLOSS"],
                "policies": [
                    {
                        "glossary_key": "ROMAIOS_GLOSS",
                        "policy": "unknown",
                    }
                ],
                "occurrences": [
                    {
                        "glossary_key": "ROMAIOS_GLOSS",
                        "english_display": "Romaioi",
                        "output_display": "romaioi",
                        "canonical_heading": "Rom?ioi",
                    }
                ],
                "original_preview": "romaioi",
                "candidate_preview": "Romaioi",
            }
            self.conn.execute(
                """
                INSERT INTO ml_quality_shadow_items (
                  id, run_id, segment_id, lane, payload_json
                ) VALUES (?, 242, ?, 'review_required', ?)
                """,
                (item_id, segment_id, json.dumps(payload)),
            )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_key_policy_resolves_every_segment_without_output_write(self) -> None:
        before = [
            tuple(row)
            for row in self.conn.execute(
                "SELECT segment_id, portuguese_text FROM output_segments ORDER BY segment_id"
            ).fetchall()
        ]
        initial = backend._glossary_display_case_calibration_payload(
            self.conn,
            score_run_id=405,
        )

        result = backend._record_glossary_display_case_policy(
            self.conn,
            glossary_key="romaios_gloss",
            policy="case_sensitive",
            reason="Nome próprio; a maiúscula faz parte da forma canônica.",
            reviewer="dashboard_human_review",
            shadow_run_id=242,
        )
        updated = backend._glossary_display_case_calibration_payload(
            self.conn,
            score_run_id=405,
        )
        after = [
            tuple(row)
            for row in self.conn.execute(
                "SELECT segment_id, portuguese_text FROM output_segments ORDER BY segment_id"
            ).fetchall()
        ]

        self.assertEqual(initial["pending_key_count"], 1)
        self.assertEqual(initial["pending_segment_count"], 2)
        self.assertEqual(
            initial["groups"][0]["segments"][0]["canonical_heading"],
            "Romaioi",
        )
        self.assertEqual(result["evidence_segment_count"], 2)
        self.assertFalse(result["operational_writes"])
        self.assertEqual(updated["pending_key_count"], 0)
        self.assertEqual(updated["pending_segment_count"], 0)
        self.assertEqual(updated["lane_counts"], {"confirmed_case_sensitive": 2})
        self.assertEqual(before, after)

    def test_key_policy_change_invalidates_release_diff_cache(self) -> None:
        before = backend._release_diff_review_cache_key(self.conn, None)

        backend._record_glossary_display_case_policy(
            self.conn,
            glossary_key="ROMAIOS_GLOSS",
            policy="case_sensitive",
            reason="Nome próprio; a maiúscula faz parte da forma canônica.",
            reviewer="dashboard_human_review",
            shadow_run_id=242,
        )
        after = backend._release_diff_review_cache_key(self.conn, None)

        self.assertNotEqual(before, after)
        self.assertIn('"glossary_display_case_policy_count":1', after)
        self.assertIn('"glossary_display_case_shadow_id":242', after)


class MojibakeLexiconReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE source_segments (
              id INTEGER PRIMARY KEY,
              relative_path TEXT,
              source_key TEXT,
              english_text TEXT,
              spanish_text TEXT
            );
            CREATE TABLE output_segments (
              segment_id INTEGER PRIMARY KEY,
              portuguese_text TEXT
            );
            CREATE TABLE ml_quality_shadow_runs (
              id INTEGER PRIMARY KEY,
              source_rule_version TEXT,
              score_run_id INTEGER,
              record_count INTEGER,
              eligible_count INTEGER,
              status TEXT,
              metadata_json TEXT,
              finished_at TEXT
            );
            CREATE TABLE ml_quality_shadow_items (
              id INTEGER PRIMARY KEY,
              run_id INTEGER,
              segment_id INTEGER,
              lane TEXT,
              payload_json TEXT
            );
            CREATE TABLE mojibake_lexicon_review_decisions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              segment_id INTEGER NOT NULL UNIQUE,
              decision TEXT NOT NULL,
              reason TEXT NOT NULL,
              reviewer TEXT NOT NULL,
              source_shadow_run_id INTEGER NOT NULL,
              source_score_run_id INTEGER NOT NULL,
              baseline_hash TEXT NOT NULL,
              candidate_hash TEXT,
              candidate_text TEXT,
              patterns_json TEXT NOT NULL DEFAULT '[]',
              resolution_scope TEXT NOT NULL DEFAULT 'manual',
              remaining_signal_count INTEGER NOT NULL DEFAULT 0,
              residual_findings_json TEXT NOT NULL DEFAULT '[]',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE mojibake_lexicon_replacement_decisions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              segment_id INTEGER NOT NULL,
              pattern_key TEXT NOT NULL,
              original_text TEXT NOT NULL,
              replacement_text TEXT NOT NULL,
              outcome TEXT NOT NULL,
              reason TEXT NOT NULL,
              reviewer TEXT NOT NULL,
              source_shadow_run_id INTEGER NOT NULL,
              source_score_run_id INTEGER NOT NULL,
              evidence_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(segment_id, pattern_key)
            );
            CREATE TABLE mojibake_boundary_pattern_decisions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              normalized_pattern TEXT NOT NULL UNIQUE COLLATE NOCASE,
              display_pattern TEXT NOT NULL,
              decision TEXT NOT NULL,
              canonical_replacement TEXT,
              reason TEXT NOT NULL,
              reviewer TEXT NOT NULL,
              source_shadow_run_id INTEGER NOT NULL,
              source_score_run_id INTEGER NOT NULL,
              evidence_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            INSERT INTO source_segments VALUES (
              101, 'events/example_l_spanish.yml', 'example.101',
              'mutual behavior', 'comportamiento mutuo'
            );
            INSERT INTO output_segments VALUES (101, 'comportamento m?tuo');
            INSERT INTO ml_quality_shadow_runs VALUES (
              228, 'quality_mojibake_lexicon_shadow_v5', 405, 1, 0,
              'completed', '{}', '2026-07-31T19:19:20Z'
            );
            """
        )
        baseline = "comportamento m?tuo"
        candidate = "comportamento mútuo"
        payload = {
            "segment_id": 101,
            "human_locked": False,
            "baseline_hash": hashlib.sha256(baseline.encode("utf-8")).hexdigest(),
            "candidate_hash": hashlib.sha256(candidate.encode("utf-8")).hexdigest(),
            "replacements": [
                {"original": "m?tuo", "replacement": "mútuo", "corpus_support": 5}
            ],
            "blockers": [],
            "token_integrity_ok": True,
            "candidate_complete": True,
            "residual_word_signals": [],
            "unresolved_signals": [],
            "raw_current_score": 0.08,
        }
        self.conn.execute(
            """
            INSERT INTO ml_quality_shadow_items (
              id, run_id, segment_id, lane, payload_json
            ) VALUES (1, 228, 101, 'blocked_or_context', ?)
            """,
            (json.dumps(payload),),
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_review_records_candidate_without_writing_output(self) -> None:
        initial = backend._mojibake_lexicon_review_payload(
            self.conn,
            score_run_id=405,
        )
        before = self.conn.execute(
            "SELECT portuguese_text FROM output_segments WHERE segment_id = 101"
        ).fetchone()[0]

        result = backend._record_mojibake_lexicon_review_decision(
            self.conn,
            segment_id=101,
            decision="accept_suggestion",
            reason="ReconstruÃ§Ã£o lexical confirmada pelo contexto.",
            reviewer="dashboard_human_review",
            shadow_run_id=228,
        )
        updated = backend._mojibake_lexicon_review_payload(
            self.conn,
            score_run_id=405,
        )
        after = self.conn.execute(
            "SELECT portuguese_text FROM output_segments WHERE segment_id = 101"
        ).fetchone()[0]

        self.assertEqual(initial["pending_count"], 1)
        self.assertEqual(initial["internal_suggestion_count"], 1)
        self.assertEqual(initial["items"][0]["candidate_preview"], "comportamento mútuo")
        self.assertTrue(result["candidate_recorded"])
        self.assertFalse(result["operational_writes"])
        self.assertEqual(updated["pending_count"], 0)
        self.assertEqual(updated["reviewed_count"], 1)
        self.assertEqual(updated["items"][0]["decision"], "accept_suggestion")
        replacement = self.conn.execute(
            "SELECT outcome FROM mojibake_lexicon_replacement_decisions WHERE segment_id = 101"
        ).fetchone()
        self.assertEqual(replacement["outcome"], "accepted_complete")
        self.assertEqual(before, after)

    def test_focused_reviewer_returns_unabridged_reference_texts(self) -> None:
        detail = backend._mojibake_lexicon_segment_detail(
            self.conn,
            segment_id=101,
            shadow_run_id=228,
        )

        self.assertEqual(detail["segment_id"], 101)
        self.assertEqual(detail["english_text"], "mutual behavior")
        self.assertEqual(detail["spanish_text"], "comportamiento mutuo")
        self.assertEqual(detail["old_text"], "comportamiento mutuo")
        self.assertEqual(detail["output_text"], "comportamento m?tuo")
        self.assertEqual(detail["candidate_text"], "comportamento mútuo")
        self.assertTrue(detail["candidate_available"])
        self.assertFalse(detail["stale"])

    def test_current_validator_promotes_stale_tu_false_positive_to_complete(self) -> None:
        baseline = (
            "Tu não amas [marriage_vassal.GetFirstNameNoTooltip], "
            "s? queres que eu #EMP sofra!#!"
        )
        candidate = baseline.replace("s?", "só")
        self.conn.execute(
            "UPDATE output_segments SET portuguese_text = ? WHERE segment_id = 101",
            (baseline,),
        )
        start = baseline.index("s?")
        payload = {
            "segment_id": 101,
            "human_locked": False,
            "baseline_hash": hashlib.sha256(baseline.encode("utf-8")).hexdigest(),
            "candidate_hash": hashlib.sha256(candidate.encode("utf-8")).hexdigest(),
            "replacements": [
                {
                    "original": "s?",
                    "replacement": "só",
                    "start": start,
                    "end": start + 2,
                    "corpus_support": 4,
                }
            ],
            "blockers": ["other_issue_codes", "post_validation_issue"],
            "token_integrity_ok": True,
            "candidate_complete": False,
            "residual_word_signals": [],
            "unresolved_signals": [],
            "raw_current_score": 0.10,
            "pre_issue_codes": [
                "replacement_question_mark_mojibake",
                "spanish_residue",
            ],
            "post_issue_codes": ["spanish_residue"],
        }
        self.conn.execute(
            "UPDATE ml_quality_shadow_items SET payload_json = ? WHERE id = 1",
            (json.dumps(payload, ensure_ascii=False),),
        )
        self.conn.commit()

        review = backend._mojibake_lexicon_review_payload(
            self.conn,
            score_run_id=405,
        )
        item = review["items"][0]

        self.assertTrue(item["candidate_complete"])
        self.assertTrue(item["focus_candidate_complete"])
        self.assertEqual(item["family"], "complete_suggestion")
        self.assertNotIn("other_issue_codes", item["blockers"])
        self.assertNotIn("post_validation_issue", item["blockers"])

        result = backend._record_mojibake_lexicon_review_decision(
            self.conn,
            segment_id=101,
            decision="accept_suggestion",
            reason="A proposta atual não deixa resíduos.",
            reviewer="dashboard_human_review",
            shadow_run_id=228,
        )
        self.assertEqual(result["decision"], "accept_suggestion")

    def test_partial_correction_is_learned_without_being_marked_complete(self) -> None:
        baseline = "comportamento m?tuo e est?o"
        candidate = "comportamento mútuo e est?o"
        self.conn.execute(
            "UPDATE output_segments SET portuguese_text = ? WHERE segment_id = 101",
            (baseline,),
        )
        payload = {
            "segment_id": 101,
            "human_locked": False,
            "baseline_hash": hashlib.sha256(baseline.encode("utf-8")).hexdigest(),
            "candidate_hash": hashlib.sha256(candidate.encode("utf-8")).hexdigest(),
            "replacements": [
                {
                    "original": "m?tuo",
                    "replacement": "mútuo",
                    "corpus_support": 5,
                    "selection_reason": "unique_portuguese_character",
                }
            ],
            "blockers": ["residual_question_mark_signal"],
            "token_integrity_ok": True,
            "candidate_complete": False,
            "residual_word_signals": ["est?o"],
            "unresolved_signals": [
                {"token": "est?o", "reason": "accented_candidate_ambiguous"}
            ],
            "raw_current_score": 0.08,
        }
        self.conn.execute(
            "UPDATE ml_quality_shadow_items SET payload_json = ? WHERE id = 1",
            (json.dumps(payload, ensure_ascii=False),),
        )
        self.conn.commit()

        with self.assertRaises(ValueError):
            backend._record_mojibake_lexicon_review_decision(
                self.conn,
                segment_id=101,
                decision="accept_suggestion",
                reason="Não pode ser integral.",
                reviewer="dashboard_human_review",
                shadow_run_id=228,
            )

        result = backend._record_mojibake_lexicon_review_decision(
            self.conn,
            segment_id=101,
            decision="partial_correction",
            reason="m?tuo foi corrigido, mas est?o ainda exige contexto.",
            reviewer="dashboard_human_review",
            shadow_run_id=228,
        )
        decision = self.conn.execute(
            """
            SELECT decision, resolution_scope, remaining_signal_count
            FROM mojibake_lexicon_review_decisions
            WHERE segment_id = 101
            """
        ).fetchone()
        replacement = self.conn.execute(
            "SELECT outcome FROM mojibake_lexicon_replacement_decisions WHERE segment_id = 101"
        ).fetchone()
        payload_after = backend._mojibake_lexicon_review_payload(
            self.conn,
            score_run_id=405,
        )

        self.assertEqual(result["decision"], "partial_correction")
        self.assertEqual(decision["decision"], "manual_review")
        self.assertEqual(decision["resolution_scope"], "partial")
        self.assertEqual(decision["remaining_signal_count"], 1)
        self.assertEqual(replacement["outcome"], "accepted_partial")
        self.assertEqual(payload_after["items"][0]["decision"], "partial_correction")
        self.assertEqual(
            self.conn.execute(
                "SELECT portuguese_text FROM output_segments WHERE segment_id = 101"
            ).fetchone()[0],
            baseline,
        )

    def test_no_candidate_with_residuals_can_confirm_mapped_residues(self) -> None:
        baseline = "Qualquer uma destas: o honesto ? o justo ? o paciente ? um defensor"
        self.conn.execute(
            "UPDATE output_segments SET portuguese_text = ? WHERE segment_id = 101",
            (baseline,),
        )
        payload = {
            "segment_id": 101,
            "human_locked": False,
            "baseline_hash": hashlib.sha256(baseline.encode("utf-8")).hexdigest(),
            "candidate_hash": None,
            "replacements": [],
            "blockers": ["human_context_required", "no_supported_replacement"],
            "token_integrity_ok": True,
            "candidate_complete": False,
            "residual_word_signals": ["?", "?", "?", "?"],
            "unresolved_signals": [
                {"token": "?", "reason": "human_context_required", "signal_index": index}
                for index in range(4)
            ],
            "raw_current_score": 0.079,
        }
        self.conn.execute(
            "UPDATE ml_quality_shadow_items SET payload_json = ? WHERE id = 1",
            (json.dumps(payload, ensure_ascii=False),),
        )
        self.conn.commit()

        result = backend._record_mojibake_lexicon_review_decision(
            self.conn,
            segment_id=101,
            decision="partial_correction",
            reason="Quatro resíduos estruturados confirmados; sem proposta automática.",
            reviewer="dashboard_human_review",
            shadow_run_id=228,
        )
        stored = self.conn.execute(
            """
            SELECT resolution_scope, remaining_signal_count, candidate_text
            FROM mojibake_lexicon_review_decisions
            WHERE segment_id = 101
            """
        ).fetchone()

        self.assertEqual(result["decision"], "partial_correction")
        self.assertFalse(result["candidate_recorded"])
        self.assertEqual(stored["resolution_scope"], "partial")
        self.assertEqual(stored["remaining_signal_count"], 4)
        self.assertIsNone(stored["candidate_text"])
        self.assertIsNone(
            self.conn.execute(
                "SELECT outcome FROM mojibake_lexicon_replacement_decisions WHERE segment_id = 101"
            ).fetchone()
        )

    def test_no_candidate_without_residuals_can_accept_current_text(self) -> None:
        baseline = "Texto atual correto e sem resíduos estruturados."
        self.conn.execute(
            "UPDATE output_segments SET portuguese_text = ? WHERE segment_id = 101",
            (baseline,),
        )
        payload = {
            "segment_id": 101,
            "human_locked": False,
            "baseline_hash": hashlib.sha256(baseline.encode("utf-8")).hexdigest(),
            "candidate_hash": None,
            "replacements": [],
            "blockers": [],
            "token_integrity_ok": True,
            "candidate_complete": False,
            "residual_word_signals": [],
            "unresolved_signals": [],
            "raw_current_score": 0.25,
        }
        self.conn.execute(
            "UPDATE ml_quality_shadow_items SET payload_json = ? WHERE id = 1",
            (json.dumps(payload, ensure_ascii=False),),
        )
        self.conn.commit()

        result = backend._record_mojibake_lexicon_review_decision(
            self.conn,
            segment_id=101,
            decision="accept_suggestion",
            reason="Texto atual correto; nenhuma substituição necessária.",
            reviewer="dashboard_human_review",
            shadow_run_id=228,
        )

        self.assertEqual(result["decision"], "accept_suggestion")
        self.assertFalse(result["candidate_recorded"])
        self.assertEqual(result["remaining_signal_count"], 0)

    def test_partial_correction_preserves_structured_semantic_residuals(self) -> None:
        findings = [
            {
                "issue_family": "dynamic_gender_possessive",
                "observed_text": "do meu",
                "controller": "couture_spouse",
                "expected_strategy": "select_cstring",
                "expected_expression": "da minha | do meu",
            },
            {
                "issue_family": "dynamic_gender_possessive",
                "observed_text": "minha",
                "controller": "couture_guest",
                "expected_strategy": "select_cstring",
                "expected_expression": "minha | meu",
            },
        ]
        result = backend._record_mojibake_lexicon_review_decision(
            self.conn,
            segment_id=101,
            decision="partial_correction",
            reason="Foco Unicode correto; restam dois possessivos sem g\u00eanero din\u00e2mico.",
            reviewer="dashboard_human_review",
            shadow_run_id=228,
            residual_findings=findings,
        )
        stored = self.conn.execute(
            """
            SELECT resolution_scope, residual_findings_json
            FROM mojibake_lexicon_review_decisions
            WHERE segment_id = 101
            """
        ).fetchone()
        updated = backend._mojibake_lexicon_review_payload(
            self.conn,
            score_run_id=405,
        )
        item = updated["items"][0]

        self.assertEqual(result["decision"], "partial_correction")
        self.assertEqual(result["residual_finding_count"], 2)
        self.assertEqual(stored["resolution_scope"], "partial")
        self.assertEqual(len(json.loads(stored["residual_findings_json"])), 2)
        self.assertEqual(item["family"], "partial_suggestion")
        self.assertEqual(item["residual_finding_count"], 2)
        self.assertEqual(item["residual_routes"], ["semantic_gender"])
        self.assertTrue(item["known_residual_issue"])
        self.assertFalse(item["candidate_complete"])

    def test_valid_punctuation_is_recorded_per_occurrence_and_carried(self) -> None:
        baseline = "Não? Ainda há ? sua carga e família...?"
        candidate = "Não? Ainda há ? sua carga e família...?"
        self.conn.execute(
            "UPDATE output_segments SET portuguese_text = ? WHERE segment_id = 101",
            (baseline,),
        )
        payload = {
            "segment_id": 101,
            "human_locked": False,
            "baseline_hash": hashlib.sha256(baseline.encode("utf-8")).hexdigest(),
            "candidate_hash": hashlib.sha256(candidate.encode("utf-8")).hexdigest(),
            "replacements": [],
            "blockers": ["residual_question_mark_signal"],
            "token_integrity_ok": True,
            "candidate_complete": False,
            "residual_word_signals": ["Não?", "? sua", "família...?"],
            "unresolved_signals": [
                {"token": "Não?", "reason": "boundary_question_mark_ambiguous", "context_family": "sentence_end"},
                {"token": "? sua", "reason": "human_context_required", "context_family": "inline_singleton"},
                {"token": "família...?", "reason": "boundary_question_mark_ambiguous", "context_family": "sentence_end"},
            ],
            "raw_current_score": 0.08,
        }
        self.conn.execute(
            "UPDATE ml_quality_shadow_items SET payload_json = ? WHERE id = 1",
            (json.dumps(payload, ensure_ascii=False),),
        )
        self.conn.commit()

        result = backend._record_mojibake_lexicon_review_decision(
            self.conn,
            segment_id=101,
            decision="valid_question_mark",
            reason="Não? e família...? são pontuação legítima.",
            reviewer="dashboard_human_review",
            shadow_run_id=228,
            valid_punctuation_signals=[
                {"signal_index": 0, "token": "Não?"},
                {"signal_index": 2, "token": "família...?"},
            ],
        )
        item = backend._mojibake_lexicon_review_payload(
            self.conn,
            score_run_id=405,
        )["items"][0]

        self.assertEqual(result["resolution_scope"], "occurrence")
        self.assertEqual(item["remaining_signal_count"], 1)
        self.assertEqual(item["residual_word_signals"], ["? sua"])
        self.assertEqual(item["validated_punctuation_count"], 2)
        self.assertEqual(item["review_status"], "pending")

        self.conn.execute(
            """
            INSERT INTO ml_quality_shadow_runs VALUES (
              229, 'quality_mojibake_lexicon_shadow_v5', 405, 1, 0,
              'completed', '{}', '2026-08-01T14:29:00Z'
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO ml_quality_shadow_items VALUES (2, 229, 101, 'blocked_or_context', ?)
            """,
            (json.dumps(payload, ensure_ascii=False),),
        )
        self.conn.commit()
        carried_item = backend._mojibake_lexicon_review_payload(
            self.conn,
            score_run_id=405,
        )["items"][0]

        self.assertEqual(carried_item["review_origin"], "carried")
        self.assertEqual(carried_item["remaining_signal_count"], 1)
        self.assertEqual(carried_item["residual_word_signals"], ["? sua"])

    def test_unchanged_candidate_carries_review_into_next_shadow(self) -> None:
        backend._record_mojibake_lexicon_review_decision(
            self.conn,
            segment_id=101,
            decision="accept_suggestion",
            reason="Reconstrução integral confirmada.",
            reviewer="dashboard_human_review",
            shadow_run_id=228,
        )
        payload_json = self.conn.execute(
            "SELECT payload_json FROM ml_quality_shadow_items WHERE id = 1"
        ).fetchone()[0]
        self.conn.execute(
            """
            INSERT INTO ml_quality_shadow_runs VALUES (
              229, 'quality_mojibake_lexicon_shadow_v5', 405, 1, 0,
              'completed', '{}', '2026-08-01T14:29:00Z'
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO ml_quality_shadow_items (
              id, run_id, segment_id, lane, payload_json
            ) VALUES (2, 229, 101, 'blocked_or_context', ?)
            """,
            (payload_json,),
        )
        self.conn.commit()

        updated = backend._mojibake_lexicon_review_payload(
            self.conn,
            score_run_id=405,
        )

        self.assertEqual(updated["shadow_run_id"], 229)
        self.assertEqual(updated["previous_shadow_run_id"], 228)
        self.assertEqual(updated["reviewed_count"], 1)
        self.assertEqual(updated["pending_count"], 0)
        self.assertEqual(updated["carried_review_count"], 1)
        self.assertEqual(updated["changed_segment_count"], 0)
        self.assertEqual(updated["items"][0]["review_origin"], "carried")

    def test_manual_residual_stays_partial_when_focused_repair_is_complete(self) -> None:
        result = backend._record_mojibake_lexicon_review_decision(
            self.conn,
            segment_id=101,
            decision="manual_review",
            reason="O mojibake foi corrigido, mas ainda existe outra palavra incorreta.",
            reviewer="dashboard_human_review",
            shadow_run_id=228,
        )
        self.conn.execute(
            """
            UPDATE mojibake_lexicon_review_decisions
            SET candidate_hash = NULL, candidate_text = NULL
            WHERE segment_id = 101
            """
        )
        payload_json = self.conn.execute(
            "SELECT payload_json FROM ml_quality_shadow_items WHERE id = 1"
        ).fetchone()[0]
        self.conn.execute(
            """
            INSERT INTO ml_quality_shadow_runs VALUES (
              229, 'quality_mojibake_lexicon_shadow_v5', 405, 1, 0,
              'completed', '{}', '2026-08-01T14:29:00Z'
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO ml_quality_shadow_items (
              id, run_id, segment_id, lane, payload_json
            ) VALUES (2, 229, 101, 'blocked_or_context', ?)
            """,
            (payload_json,),
        )
        self.conn.commit()

        updated = backend._mojibake_lexicon_review_payload(
            self.conn,
            score_run_id=405,
        )
        item = updated["items"][0]

        self.assertTrue(result["candidate_recorded"])
        self.assertEqual(updated["pending_count"], 0)
        self.assertEqual(updated["reviewed_count"], 1)
        self.assertEqual(updated["carried_review_count"], 1)
        self.assertEqual(item["decision"], "manual_review")
        self.assertEqual(item["review_origin"], "carried")
        self.assertEqual(item["family"], "partial_suggestion")
        self.assertTrue(item["focus_candidate_complete"])
        self.assertTrue(item["known_residual_issue"])
        self.assertFalse(item["candidate_complete"])

    def test_boundary_pattern_review_trains_pattern_without_writing_output(self) -> None:
        baseline = "h? sinais"
        self.conn.execute(
            "UPDATE output_segments SET portuguese_text = ? WHERE segment_id = 101",
            (baseline,),
        )
        payload = {
            "segment_id": 101,
            "human_locked": False,
            "baseline_hash": hashlib.sha256(baseline.encode("utf-8")).hexdigest(),
            "candidate_hash": hashlib.sha256(baseline.encode("utf-8")).hexdigest(),
            "replacements": [],
            "blockers": ["boundary_question_mark_ambiguous", "no_supported_replacement"],
            "token_integrity_ok": True,
            "candidate_complete": False,
            "residual_word_signals": ["h?"],
            "unresolved_signals": [
                {
                    "token": "h?",
                    "reason": "boundary_question_mark_ambiguous",
                    "alternatives": [
                        {"text": "há", "corpus_support": 6031},
                        {"text": "ha", "corpus_support": 357},
                    ],
                }
            ],
            "raw_current_score": 0.08,
        }
        self.conn.execute(
            "UPDATE ml_quality_shadow_items SET payload_json = ? WHERE id = 1",
            (json.dumps(payload, ensure_ascii=False),),
        )
        self.conn.commit()

        initial = backend._mojibake_lexicon_review_payload(self.conn, score_run_id=405)
        result = backend._record_mojibake_boundary_pattern_decision(
            self.conn,
            pattern="h?",
            decision="canonical_replacement",
            canonical_replacement="há",
            reason="Os exemplos confirmam o verbo haver com acento.",
            reviewer="dashboard_human_review",
            shadow_run_id=228,
        )
        updated = backend._mojibake_lexicon_review_payload(self.conn, score_run_id=405)

        self.assertEqual(initial["boundary_pattern_count"], 1)
        self.assertEqual(initial["boundary_pattern_pending_count"], 1)
        self.assertFalse(result["operational_writes"])
        self.assertEqual(result["affected_segment_count"], 1)
        self.assertEqual(updated["boundary_pattern_reviewed_count"], 1)
        self.assertEqual(
            updated["boundary_patterns"][0]["canonical_replacement"],
            "há",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT portuguese_text FROM output_segments WHERE segment_id = 101"
            ).fetchone()[0],
            baseline,
        )

        with self.assertRaises(ValueError):
            backend._record_mojibake_boundary_pattern_decision(
                self.conn,
                pattern="h?",
                decision="canonical_replacement",
                canonical_replacement="já",
                reason="Forma incompatível.",
                reviewer="dashboard_human_review",
                shadow_run_id=228,
            )

    def test_positioned_rebuild_does_not_replace_an_earlier_valid_question_mark(self) -> None:
        baseline = "Dias?\\n\\n? um desafio"
        target = baseline.rfind("?")

        rebuilt = backend._rebuild_mojibake_candidate(
            baseline,
            [
                {
                    "original": "?",
                    "replacement": "É",
                    "start": target,
                    "end": target + 1,
                }
            ],
        )

        self.assertEqual(rebuilt, "Dias?\\n\\nÉ um desafio")

    def test_boundary_alternatives_require_an_accented_shape_match(self) -> None:
        self.assertTrue(backend._mojibake_accented_alternative_matches("h?", "há"))
        self.assertFalse(backend._mojibake_accented_alternative_matches("h?", "ha"))
        self.assertFalse(backend._mojibake_accented_alternative_matches("h?", "hu"))
        self.assertFalse(backend._mojibake_accented_alternative_matches("\\n?", "é"))


if __name__ == "__main__":
    unittest.main()
