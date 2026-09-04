from __future__ import annotations

import sqlite3
import unittest

from dashboard import backend


class SupervisedCalibrationSummaryTest(unittest.TestCase):
    def test_exact_shadow_observations_keep_only_latest_decision(self) -> None:
        rows = backend._deduplicate_calibration_observations(
            [
                {
                    "segment_id": 42,
                    "text_hash": "hash",
                    "source": "falha_confirmada",
                    "outcome": 0,
                    "reviewed_at": "2026-08-30T10:00:00",
                },
                {
                    "segment_id": 42,
                    "text_hash": "hash",
                    "source": "falha_confirmada",
                    "outcome": 0,
                    "reviewed_at": "2026-08-31T10:00:00",
                },
            ]
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reviewed_at"], "2026-08-31T10:00:00")

    def test_ece_and_brier_use_human_outcome_against_probability(self) -> None:
        summary = backend._calibration_summary(
            [
                {"probability": 0.10, "outcome": 1},
                {"probability": 0.90, "outcome": 1},
            ]
        )

        self.assertEqual(summary["observation_count"], 2)
        self.assertEqual(summary["positive_count"], 2)
        self.assertEqual(summary["negative_count"], 0)
        self.assertAlmostEqual(summary["mean_confidence"], 0.50)
        self.assertAlmostEqual(summary["observed_safe_rate"], 1.0)
        self.assertAlmostEqual(summary["calibration_gap"], -0.50)
        self.assertAlmostEqual(summary["ece"], 0.50)
        self.assertAlmostEqual(summary["brier"], 0.41)

    def test_complexity_separates_long_token_heavy_text(self) -> None:
        text = (
            "Texto longo " * 30
            + "[ROOT.Char.GetName] [Select_CString(scope.IsFemale, 'ela', 'ele')] "
            + "[target.GetTitle] [host.Custom('ES_ElLa')]"
        )

        bucket, text_length, dynamic_tokens = backend._calibration_complexity(text)

        self.assertEqual(bucket, "longo_complexo")
        self.assertGreaterEqual(text_length, 280)
        self.assertGreaterEqual(dynamic_tokens, 4)

    def test_spanish_residual_issue_becomes_blocking_calibration_evidence(self) -> None:
        profile = backend._calibration_issue_profile(
            '[{"code":"spanish_residue_in_literal","severity":"high","matches":["el","la"]}]'
        )

        self.assertEqual(profile["issue_family"], "spanish_residual")
        self.assertEqual(profile["issue_severity"], "high")
        self.assertTrue(profile["blocking_issue"])
        self.assertEqual(profile["issue_codes"], ["spanish_residue_in_literal"])

    def test_slices_keep_sample_size_visible(self) -> None:
        rows = backend._calibration_slices(
            [
                {"family": "core", "probability": 0.20, "outcome": 1},
                {"family": "core", "probability": 0.80, "outcome": 1},
                {"family": "events", "probability": 0.10, "outcome": 0},
            ],
            "family",
        )

        self.assertEqual(rows[0]["label"], "core")
        self.assertEqual(rows[0]["observation_count"], 2)
        self.assertEqual(rows[1]["label"], "events")
        self.assertEqual(rows[1]["observation_count"], 1)

    def test_shadow_split_keeps_same_segment_in_one_fold(self) -> None:
        observations = [
            {"segment_id": 1, "outcome": 1},
            {"segment_id": 1, "outcome": 0},
            {"segment_id": 2, "outcome": 1},
            {"segment_id": 3, "outcome": 0},
        ]

        assignment = backend._shadow_fold_by_segment(observations, fold_count=3)

        self.assertEqual(set(assignment), {1, 2, 3})
        self.assertIn(assignment[1], {0, 1, 2})

    def test_shadow_calibration_is_oof_and_safety_is_measurable(self) -> None:
        observations = []
        for index in range(120):
            outcome = 1 if index % 3 else 0
            observations.append(
                {
                    "segment_id": index + 1,
                    "text_hash": f"hash-{index}",
                    "source": "baixo_score_manual",
                    "family": "events" if index % 2 else "core",
                    "complexity": "longo_complexo" if index % 2 else "predominantemente_dinamico",
                    "probability": 0.05 + (index % 5) * 0.01,
                    "outcome": outcome,
                }
            )

        shadow = backend._stratified_calibration_shadow(
            observations,
            safe_threshold=0.86,
        )

        self.assertTrue(shadow["available"])
        self.assertEqual(shadow["candidate"]["observation_count"], 120)
        self.assertLess(shadow["candidate"]["brier"], shadow["current"]["brier"])
        self.assertGreater(shadow["contract"]["probability_cap"], 0.86)
        self.assertGreater(shadow["guardrails"]["candidate_high_confidence_audit"]["count"], 0)
        self.assertIn("safe_threshold_measurable", shadow["guardrails"])
        self.assertIn("high_confidence_audit_clean", shadow["gate_reasons"])

    def test_dynamic_holdout_quota_covers_small_strata_and_target(self) -> None:
        counts = {
            ("human", "0-10%"): 5,
            ("human", "10-20%"): 100,
            ("preserved", "10-20%"): 200,
        }

        quotas = backend._dynamic_holdout_quotas(counts, 30)

        self.assertEqual(sum(quotas.values()), 30)
        self.assertGreaterEqual(quotas[("human", "0-10%")], 3)
        self.assertTrue(all(quotas[key] <= counts[key] for key in counts))

    def test_dynamic_holdout_selection_round_robins_families(self) -> None:
        rows = [
            {"segment_id": index, "text_hash": f"h-{index}", "calibration_group": "core"}
            for index in range(1, 11)
        ] + [
            {"segment_id": 50, "text_hash": "h-50", "calibration_group": "events"},
            {"segment_id": 51, "text_hash": "h-51", "calibration_group": "dlc/ep3"},
        ]

        selected = backend._dynamic_holdout_select_stratum(rows, 6)

        self.assertEqual(len(selected), 6)
        self.assertEqual(
            {row["calibration_group"] for row in selected[:3]},
            {"core", "events", "dlc/ep3"},
        )

    def test_dynamic_holdout_reuses_same_score_after_state_only_refresh(self) -> None:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(
            """
            CREATE TABLE ml_dynamic_score_holdout_runs (
                id INTEGER PRIMARY KEY,
                rule_version TEXT NOT NULL,
                score_run_id INTEGER NOT NULL,
                segment_state_run_id INTEGER NOT NULL
            );
            """
        )
        con.executemany(
            "INSERT INTO ml_dynamic_score_holdout_runs VALUES (?, ?, ?, ?)",
            (
                (1, backend.DYNAMIC_SCORE_HOLDOUT_RULE_VERSION, 405, 785),
                (2, backend.DYNAMIC_SCORE_HOLDOUT_RULE_VERSION, 404, 786),
            ),
        )

        reused = backend._one(
            con,
            """
            SELECT * FROM ml_dynamic_score_holdout_runs
            WHERE rule_version = ? AND score_run_id = ?
            ORDER BY CASE WHEN segment_state_run_id = ? THEN 0 ELSE 1 END, id DESC
            LIMIT 1
            """,
            (backend.DYNAMIC_SCORE_HOLDOUT_RULE_VERSION, 405, 786),
        )

        self.assertEqual(reused["id"], 1)
        self.assertEqual(reused["segment_state_run_id"], 785)


if __name__ == "__main__":
    unittest.main()
