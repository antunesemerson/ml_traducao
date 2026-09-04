from __future__ import annotations

import sqlite3
import unittest

from dashboard import backend


class ScoreRecalibrationOperationalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        backend._ensure_score_recalibration_operational_schema(self.con)
        self.con.executescript(
            """
            CREATE TABLE ml_score_runs (id INTEGER PRIMARY KEY);
            CREATE TABLE ml_score_items (
              run_id INTEGER NOT NULL,
              segment_id INTEGER NOT NULL,
              candidate_text TEXT,
              issues_json TEXT,
              issue_count INTEGER,
              high_issue_count INTEGER
            );
            CREATE TABLE source_segments (
              id INTEGER PRIMARY KEY,
              english_text TEXT,
              spanish_text TEXT,
              old_text TEXT
            );
            CREATE TABLE output_segments (
              segment_id INTEGER PRIMARY KEY,
              portuguese_text TEXT
            );
            INSERT INTO ml_score_runs VALUES (11);
            INSERT INTO source_segments VALUES (101, 'English A', 'Spanish A', 'Old A');
            INSERT INTO source_segments VALUES (102, 'English B', 'Spanish B', 'Old B');
            INSERT INTO ml_score_items VALUES (11, 101, 'Candidato A', '[]', 0, 0);
            INSERT INTO ml_score_items VALUES (11, 102, 'Candidato B', '[]', 0, 0);
            """
        )
        now = backend._now_iso()
        cursor = self.con.execute(
            """
            INSERT INTO ml_score_calibration_candidate_runs (
              rule_version, shadow_run_id, model_run_id, score_run_id,
              candidate_tree_hash, review_watermark, status, item_count,
              required_review_count, gate_json, summary_json, created_at, updated_at
            ) VALUES (?, 7, 3, 11, 'tree', 'watermark', 'awaiting_review', 2, 2, ?, '{}', ?, ?)
            """,
            (
                backend.SCORE_RECALIBRATION_CANDIDATE_VERSION,
                '{"shadow_qualified": true, "same_score_context": true}',
                now,
                now,
            ),
        )
        self.run_id = int(cursor.lastrowid)
        for rank, segment_id in enumerate((101, 102), start=1):
            self.con.execute(
                """
                INSERT INTO ml_score_calibration_candidate_items (
                  run_id, segment_id, text_hash, relative_path, source_key,
                  complexity, family, raw_probability, current_probability,
                  candidate_probability, delta, raw_band, current_band,
                  candidate_band, review_required, review_rank, created_at
                ) VALUES (?, ?, ?, 'events/example.yml', ?, 'longo_complexo',
                          'event_localization', 0.10, 0.10, 0.80, 0.70,
                          '10-20%', '10-20%', '80-90%', 1, ?, ?)
                """,
                (self.run_id, segment_id, f"hash-{segment_id}", f"key-{segment_id}", rank, now),
            )
        self.con.commit()

    def tearDown(self) -> None:
        self.con.close()

    def test_candidate_stays_blocked_until_required_sample_is_reviewed(self) -> None:
        initial = backend._score_recalibration_gate_state(self.con, self.run_id)
        self.assertFalse(initial["passed"])
        self.assertEqual(initial["pending_review_count"], 2)

        backend._record_score_recalibration_discrepancy_review(
            self.con,
            candidate_run_id=self.run_id,
            segment_id=101,
            decision="coherent_change",
            reason="coerente",
            reviewer="test",
        )
        backend._record_score_recalibration_discrepancy_review(
            self.con,
            candidate_run_id=self.run_id,
            segment_id=102,
            decision="coherent_change",
            reason="coerente",
            reviewer="test",
        )

        final = backend._score_recalibration_gate_state(self.con, self.run_id)
        self.assertTrue(final["passed"])
        self.assertEqual(final["pending_review_count"], 0)

    def test_discrepancy_queue_contains_only_unreviewed_required_items(self) -> None:
        backend._record_score_recalibration_discrepancy_review(
            self.con,
            candidate_run_id=self.run_id,
            segment_id=101,
            decision="coherent_change",
            reason="coerente",
            reviewer="test",
        )

        queue = backend._score_recalibration_discrepancy_items(self.con, self.run_id)

        self.assertEqual([item["segment_id"] for item in queue], [102])
        self.assertEqual(queue[0]["review_rank"], 2)

    def test_incorrect_discrepancy_blocks_promotion(self) -> None:
        backend._record_score_recalibration_discrepancy_review(
            self.con,
            candidate_run_id=self.run_id,
            segment_id=101,
            decision="incorrect_change",
            reason="queda incoerente",
            reviewer="test",
        )
        gate = backend._score_recalibration_gate_state(self.con, self.run_id)

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["incorrect_count"], 1)

    def test_excessive_and_insufficient_movements_remain_distinguishable(self) -> None:
        backend._record_score_recalibration_discrepancy_review(
            self.con,
            candidate_run_id=self.run_id,
            segment_id=101,
            decision="correct_direction_excessive",
            reason="direção correta, magnitude excessiva",
            reviewer="test",
        )
        backend._record_score_recalibration_discrepancy_review(
            self.con,
            candidate_run_id=self.run_id,
            segment_id=102,
            decision="correct_direction_insufficient",
            reason="direção correta, magnitude insuficiente",
            reviewer="test",
        )

        gate = backend._score_recalibration_gate_state(self.con, self.run_id)

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["incorrect_count"], 2)
        self.assertEqual(gate["excessive_count"], 1)
        self.assertEqual(gate["insufficient_count"], 1)

    def test_promotion_and_revert_only_move_registry(self) -> None:
        for segment_id in (101, 102):
            backend._record_score_recalibration_discrepancy_review(
                self.con,
                candidate_run_id=self.run_id,
                segment_id=segment_id,
                decision="coherent_change",
                reason="coerente",
                reviewer="test",
            )

        promoted = backend._promote_score_recalibration_candidate(
            self.con,
            candidate_run_id=self.run_id,
            actor="test",
            reason="candidato validado",
        )
        self.assertEqual(promoted["registry"]["active_candidate_run_id"], self.run_id)
        self.assertEqual(promoted["lifecycle_state"], "active_monitoring")
        self.assertFalse(promoted["actions"]["can_start_new_cycle"])

        reverted = backend._revert_score_recalibration_candidate(
            self.con,
            actor="test",
            reason="teste de reversão",
        )
        self.assertIsNone(reverted["registry"]["active_candidate_run_id"])

    def test_version_history_restores_raw_and_promoted_score(self) -> None:
        for segment_id in (101, 102):
            backend._record_score_recalibration_discrepancy_review(
                self.con,
                candidate_run_id=self.run_id,
                segment_id=segment_id,
                decision="coherent_change",
                reason="coerente",
                reviewer="test",
            )
        promoted = backend._promote_score_recalibration_candidate(
            self.con,
            candidate_run_id=self.run_id,
            actor="test",
            reason="candidato validado",
        )
        self.assertEqual(
            [version["kind"] for version in promoted["score_versions"]],
            ["calibrated", "raw"],
        )
        raw = backend._restore_score_recalibration_version(
            self.con,
            candidate_run_id=None,
            actor="test",
            reason="comparar com o score bruto",
        )
        self.assertEqual(raw["lifecycle_state"], "raw_monitoring")
        self.assertTrue(raw["score_versions"][-1]["active"])
        restored = backend._restore_score_recalibration_version(
            self.con,
            candidate_run_id=self.run_id,
            actor="test",
            reason="restaurar versão validada",
        )
        self.assertEqual(restored["registry"]["active_candidate_run_id"], self.run_id)
        self.assertEqual(restored["lifecycle_state"], "active_monitoring")
        self.assertTrue(restored["score_versions"][0]["active"])

    def test_effective_score_contract_is_versioned_and_score_context_safe(self) -> None:
        self.con.execute(
            "UPDATE ml_score_calibration_candidate_runs SET status = 'promoted' WHERE id = ?",
            (self.run_id,),
        )
        self.con.execute(
            """
            INSERT INTO ml_score_calibration_registry (
              registry_key, active_candidate_run_id, previous_candidate_run_id,
              updated_at, updated_by
            ) VALUES (?, ?, NULL, ?, 'test')
            """,
            (
                backend.SCORE_RECALIBRATION_REGISTRY_KEY,
                self.run_id,
                backend._now_iso(),
            ),
        )
        self.con.commit()

        active = backend._operational_effective_score_contract(self.con, 11)
        stale = backend._operational_effective_score_contract(self.con, 12)

        self.assertTrue(active["active"])
        self.assertEqual(active["source"], "operational_calibrated_score")
        self.assertEqual(active["candidate_run_id"], self.run_id)
        self.assertFalse(stale["active"])
        self.assertEqual(stale["reason"], "score_context_changed")

    def test_shadow_explanation_reconciles_candidate_delta(self) -> None:
        model = {
            "global_rate": 0.75,
            "probability_cap": 0.85,
            "levels": {
                "complexity": {
                    "longo_complexo": {
                        "count": 100,
                        "positive_count": 90,
                        "rate": 0.88,
                        "reliability": 0.8,
                    },
                },
                "family": {
                    "event_localization": {
                        "count": 80,
                        "positive_count": 76,
                        "rate": 0.92,
                        "reliability": 0.7,
                    },
                },
            },
        }
        observation = {
            "probability": 0.10,
            "complexity": "longo_complexo",
            "source": "pacote_atual",
            "family": "event_localization",
        }

        explanation = backend._shadow_calibration_explanation(model, observation)
        calibrated = backend._shadow_calibrated_probability(model, observation)

        self.assertAlmostEqual(explanation["candidate_probability"], calibrated, places=7)
        factor_effect = sum(item["effect_pp"] for item in explanation["factors"])
        reconciled = factor_effect + explanation["cap_adjustment_pp"]
        self.assertAlmostEqual(reconciled, explanation["explained_delta_pp"], places=3)
        self.assertEqual(
            {item["key"] for item in explanation["factors"]},
            {"global", "complexity", "family"},
        )

    def test_discrepancy_feedback_never_extrapolates_candidate_delta(self) -> None:
        model = {
            "global_rate": 0.80,
            "probability_cap": 0.90,
            "levels": {},
            "discrepancy_feedback": {
                "rule_version": backend.DISCREPANCY_FEEDBACK_SHADOW_VERSION,
                "review_count": 2,
                "by_direction": {},
                "by_slice": {
                    "curto_simples|core|10-20%|up": {"count": 1, "multiplier": 0.5},
                    "curto_simples|core|90-100%|down": {"count": 1, "multiplier": 0.0},
                },
            },
        }
        upward = {
            "probability": 0.10,
            "current_probability": 0.10,
            "complexity": "curto_simples",
            "family": "core",
        }
        base_model = {key: value for key, value in model.items() if key != "discrepancy_feedback"}
        base_up = backend._shadow_calibrated_probability(base_model, upward)
        adjusted_up = backend._shadow_calibrated_probability(model, upward)

        self.assertGreater(adjusted_up, upward["current_probability"])
        self.assertLess(adjusted_up, base_up)
        self.assertAlmostEqual(
            adjusted_up,
            upward["current_probability"] + (base_up - upward["current_probability"]) * 0.5,
            places=7,
        )

        downward = {**upward, "probability": 0.10, "current_probability": 0.95}
        adjusted_down = backend._shadow_calibrated_probability(model, downward)
        self.assertAlmostEqual(adjusted_down, downward["current_probability"], places=7)

        unseen = {**upward, "family": "event_localization"}
        adjusted_unseen = backend._shadow_calibrated_probability(model, unseen)
        base_unseen = backend._shadow_calibrated_probability(base_model, unseen)
        self.assertAlmostEqual(adjusted_unseen, base_unseen, places=7)

    def test_blocking_spanish_issue_limits_positive_uplift_and_is_explained(self) -> None:
        model = {
            "global_rate": 0.77,
            "safe_threshold": 0.86,
            "probability_cap": 0.94,
            "raw_probability_weight": 2.0,
            "levels": {
                "family": {
                    "event_localization": {
                        "count": 96,
                        "positive_count": 77,
                        "rate": 0.795,
                        "reliability": 0.75,
                    },
                },
            },
        }
        clean = {
            "probability": 0.0148,
            "complexity": "medio_dinamico",
            "source": "pacote_atual",
            "family": "event_localization",
        }
        issue_profile = backend._calibration_issue_profile(
            '[{"code":"spanish_residue_in_literal","severity":"high"}]'
        )
        blocked = {**clean, **issue_profile, "issue_profile": issue_profile}

        clean_probability = backend._shadow_calibrated_probability(model, clean)
        blocked_probability = backend._shadow_calibrated_probability(model, blocked)
        explanation = backend._shadow_calibration_explanation(model, blocked)

        self.assertLess(blocked_probability, clean_probability)
        self.assertLess(blocked_probability, model["safe_threshold"])
        self.assertTrue(explanation["blocking_issue"])
        guard = next(
            item for item in explanation["factors"]
            if item["key"] == "blocking_issue_guard"
        )
        self.assertLess(guard["effect_pp"], 0)
        reconciled = (
            sum(item["effect_pp"] for item in explanation["factors"])
            + explanation["cap_adjustment_pp"]
        )
        self.assertAlmostEqual(reconciled, explanation["explained_delta_pp"], places=3)

    def test_supported_downward_feedback_shrinks_unseen_downward_slices(self) -> None:
        model = {
            "global_rate": 0.70,
            "probability_cap": 0.95,
            "levels": {},
            "discrepancy_feedback": {
                "rule_version": backend.DISCREPANCY_FEEDBACK_SHADOW_VERSION,
                "direction_policy": {"minimum_down_support": 20},
                "by_slice": {},
                "by_direction": {
                    "down": {"count": 100, "multiplier": 0.0},
                },
            },
        }
        observation = {
            "probability": 0.10,
            "current_probability": 0.95,
            "complexity": "nao_revisado",
            "family": "familia_nova",
        }
        base_model = {key: value for key, value in model.items() if key != "discrepancy_feedback"}
        base_probability = backend._shadow_calibrated_probability(base_model, observation)
        adjusted = backend._shadow_calibrated_probability(model, observation)

        self.assertLess(adjusted, observation["current_probability"])
        self.assertGreater(adjusted, base_probability)
        expected_multiplier = 10 / 110
        self.assertAlmostEqual(
            adjusted,
            observation["current_probability"]
            + (base_probability - observation["current_probability"]) * expected_multiplier,
            places=7,
        )

    def test_stratified_review_sample_balances_direction_and_strata(self) -> None:
        rows = []
        for index in range(40):
            rows.append({
                "segment_id": index + 1,
                "text_hash": f"hash-{index}",
                "complexity": "longo_complexo" if index % 2 else "curto_simples",
                "family": f"family-{index % 4}",
                "current_band": f"{(index % 5) * 10}-{(index % 5 + 1) * 10}%",
                "delta": (0.80 - index * 0.01) if index < 20 else (-0.80 + (index - 20) * 0.01),
            })

        selected = backend._score_recalibration_stratified_review_sample(
            rows,
            previously_reviewed=set(),
            previously_reviewed_hashes=set(),
            target=20,
        )
        selected_ids = {segment_id for segment_id, _ in selected}
        selected_rows = [row for row in rows if row["segment_id"] in selected_ids]

        self.assertEqual(len(selected), 20)
        self.assertEqual(sum(row["delta"] > 0 for row in selected_rows), 10)
        self.assertEqual(sum(row["delta"] < 0 for row in selected_rows), 10)
        self.assertGreaterEqual(len({row["family"] for row in selected_rows}), 4)

    def test_discrepancy_feedback_accumulates_batches_by_evidence_count(self) -> None:
        previous = {
            "slice": {
                "count": 2,
                "multiplier": 0.5,
                "decisions": {"coherent_change": 1, "incorrect_direction": 1},
            },
        }
        current = {
            "slice": {
                "count": 2,
                "multiplier": 1.0,
                "decisions": {"coherent_change": 2},
            },
            "new_slice": {
                "count": 1,
                "multiplier": 0.0,
                "decisions": {"incorrect_direction": 1},
            },
        }

        merged = backend._merge_discrepancy_feedback_aggregates(previous, current)

        self.assertEqual(merged["slice"]["count"], 4)
        self.assertAlmostEqual(merged["slice"]["multiplier"], 0.75)
        self.assertEqual(merged["slice"]["decisions"]["coherent_change"], 3)
        self.assertEqual(merged["new_slice"]["multiplier"], 0.0)

    def test_recalibration_monitoring_waits_for_a_supervised_sample(self) -> None:
        recommendation = backend._score_recalibration_recommendation({
            "calibration_observation_count": 19,
            "score_audit_count": 19,
            "score_mismatch_count": 4,
            "false_safe_count": 0,
        })

        self.assertEqual(recommendation["level"], "monitoring")
        self.assertFalse(recommendation["can_recalibrate"])
        self.assertEqual(recommendation["remaining_observation_count"], 1)

    def test_recalibration_becomes_available_with_enough_new_evidence(self) -> None:
        recommendation = backend._score_recalibration_recommendation({
            "calibration_observation_count": 20,
            "score_audit_count": 12,
            "score_mismatch_count": 3,
            "false_safe_count": 0,
        })

        self.assertEqual(recommendation["level"], "available")
        self.assertEqual(recommendation["tone"], "amber")
        self.assertTrue(recommendation["can_recalibrate"])

    def test_recalibration_is_strongly_recommended_for_sustained_mismatch(self) -> None:
        recommendation = backend._score_recalibration_recommendation({
            "calibration_observation_count": 52,
            "score_audit_count": 40,
            "score_mismatch_count": 9,
            "false_safe_count": 0,
        })

        self.assertEqual(recommendation["level"], "recommended")
        self.assertEqual(recommendation["tone"], "red")
        self.assertTrue(recommendation["strongly_recommended"])


if __name__ == "__main__":
    unittest.main()
