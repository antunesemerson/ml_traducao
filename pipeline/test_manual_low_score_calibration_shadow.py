from __future__ import annotations

import unittest

from manual_low_score_calibration_shadow import (
    CONTRACT_ES_ARTICLE_PREPOSITION_PATTERN,
    CONTRACT_ES_HELPER_PATTERN,
    CONTRACT_ES_OA_SUFFIX_PATTERN,
    EXACT_SHARED_GLOSSARY_PATTERN,
    OUTSIDE_CUSTOM_GLOSSARY_PATTERN,
    has_segmented_boundary_evidence,
    has_segmented_training_support,
    is_glossary_boundary_sentinel,
    is_segmented_boundary_sentinel,
    matches_segmented_pattern,
    probability_metrics,
    segmented_probability,
    segmented_shadow_status,
    select_control_rows,
)
from ml_train_risk import is_exact_shared_glossary_token, language_features


def make_row(
    candidate_id: int,
    lane: str,
    truth_safe: bool,
    baseline_probability: float,
    candidate_probability: float,
) -> dict:
    return {
        "candidate_id": candidate_id,
        "segment_id": candidate_id + 1000,
        "suggested_hash": f"hash-{candidate_id}",
        "calibration_lane": lane,
        "calibration_group": "group",
        "human_label": "correct" if truth_safe else "semantic_error",
        "truth_safe": truth_safe,
        "baseline_probability": baseline_probability,
        "candidate_probability": candidate_probability,
    }


class ManualLowScoreCalibrationShadowTests(unittest.TestCase):
    def test_exact_spanish_is_blocked_but_shared_glossary_is_safe(self) -> None:
        spanish_row = {
            "relative_path": "domiciles/domicile_yurt_l_spanish.yml",
            "source_key": "herd_welfare_yurt_02_domicile_building",
            "candidate_text": "Almacén de mijo",
            "english_text": "Millet Storage",
            "spanish_text": "Almacén de mijo",
            "old_text": "Almacén de mijo",
            "output_text": "Almacén de mijo",
            "evidence_source": "local_learning_candidate",
            "issue_label": "residual_spanish",
        }
        glossary_text = "[Glossary( 'hellenothreskos', 'HELLENOTHRESKOS_GLOSS' )]"
        glossary_row = {
            **spanish_row,
            "relative_path": "custom_localization/insult_custom_loc_l_spanish.yml",
            "candidate_text": glossary_text,
            "english_text": glossary_text,
            "spanish_text": glossary_text,
            "old_text": glossary_text,
            "output_text": glossary_text,
        }

        self.assertIn("RISK_EXACT_SPANISH_VISIBLE", language_features(spanish_row))
        self.assertTrue(is_exact_shared_glossary_token(glossary_row))
        self.assertNotIn("RISK_EXACT_SPANISH_VISIBLE", language_features(glossary_row))
        self.assertIn("SAFE_EXACT_SHARED_GLOSSARY_TOKEN", language_features(glossary_row))

    def test_control_split_is_deterministic_and_keeps_training_examples(self) -> None:
        rows = [
            make_row(index, lane, truth_safe, 0.05, 0.10)
            for index, (lane, truth_safe) in enumerate(
                [
                    *[("human", True)] * 8,
                    *[("human", False)] * 8,
                    *[("deterministic", True)] * 8,
                    *[("preserved", False)] * 3,
                ],
                start=1,
            )
        ]

        first = select_control_rows(rows, ratio=0.25, seed="stable")
        second = select_control_rows(rows, ratio=0.25, seed="stable")

        self.assertEqual(
            [row["candidate_id"] for row in first],
            [row["candidate_id"] for row in second],
        )
        selected_by_stratum = {
            (row["calibration_lane"], row["truth_safe"])
            for row in first
        }
        self.assertEqual(
            selected_by_stratum,
            {("human", True), ("human", False), ("deterministic", True)},
        )
        self.assertLess(len(first), len(rows))

    def test_probability_metrics_detect_better_calibration(self) -> None:
        rows = [
            make_row(1, "human", True, 0.05, 0.75),
            make_row(2, "human", True, 0.10, 0.80),
            make_row(3, "human", False, 0.05, 0.15),
            make_row(4, "human", False, 0.10, 0.20),
        ]

        baseline = probability_metrics(rows, "baseline_probability")
        candidate = probability_metrics(rows, "candidate_probability")

        self.assertLess(candidate["brier"], baseline["brier"])
        self.assertLess(candidate["log_loss"], baseline["log_loss"])
        self.assertGreater(candidate["separation"], baseline["separation"])

    def test_segmented_glossary_pattern_has_strict_boundary(self) -> None:
        exact = "[Glossary( 'Prasinoi', 'GREEN_PRASINOI_GLOSS' )]"
        exact_row = {
            "relative_path": "custom_localization/example_l_spanish.yml",
            "candidate_text": exact,
            "english_text": exact,
            "spanish_text": exact,
        }
        near_miss = {
            **exact_row,
            "candidate_text": (
                "Pela morte dos fiéis: "
                "[Glossary( 'de los Prasinoi', 'GREEN_PRASINOI_GLOSS' )]"
            ),
        }

        self.assertTrue(matches_segmented_pattern(exact_row))
        self.assertFalse(is_glossary_boundary_sentinel(exact_row))
        self.assertFalse(matches_segmented_pattern(near_miss))
        self.assertTrue(is_glossary_boundary_sentinel(near_miss))

    def test_outside_custom_glossary_has_independent_scope(self) -> None:
        english = "[Glossary( 'Pacta Sunt Servanda', 'PACTA_SUNT_SERVANTA_GLOSS' )]"
        localized = "[Glossary( 'Pacta sunt servanda', 'PACTA_SUNT_SERVANTA_GLOSS' )]"
        outside = {
            "relative_path": "dlc/ep3/example_l_spanish.yml",
            "candidate_text": localized,
            "english_text": english,
            "spanish_text": localized,
        }
        custom = {
            **outside,
            "relative_path": "custom_localization/example_l_spanish.yml",
        }
        outside_near_miss = {
            **outside,
            "candidate_text": (
                "Texto "
                "[Glossary( 'Pacta sunt servanda', 'PACTA_SUNT_SERVANTA_GLOSS' )]"
            ),
        }

        self.assertFalse(
            matches_segmented_pattern(
                outside,
                EXACT_SHARED_GLOSSARY_PATTERN,
            )
        )
        self.assertTrue(
            matches_segmented_pattern(
                outside,
                OUTSIDE_CUSTOM_GLOSSARY_PATTERN,
            )
        )
        self.assertTrue(
            matches_segmented_pattern(
                custom,
                EXACT_SHARED_GLOSSARY_PATTERN,
            )
        )
        self.assertFalse(
            matches_segmented_pattern(
                custom,
                OUTSIDE_CUSTOM_GLOSSARY_PATTERN,
            )
        )
        self.assertTrue(
            is_segmented_boundary_sentinel(
                outside_near_miss,
                OUTSIDE_CUSTOM_GLOSSARY_PATTERN,
            )
        )

    def test_segmented_probability_uses_reviewed_training_only(self) -> None:
        training = [
            {"truth_safe": True}
            for _ in range(11)
        ]

        probability, evidence = segmented_probability(training)

        self.assertAlmostEqual(probability, 12 / 13)
        self.assertEqual(evidence["training_safe_count"], 11)
        self.assertEqual(evidence["training_risky_count"], 0)
        self.assertEqual(evidence["posterior_safe_probability"], probability)

    def test_segmented_probability_penalizes_risky_evidence(self) -> None:
        all_safe_probability, _ = segmented_probability(
            [{"truth_safe": True} for _ in range(8)]
        )
        mixed_probability, evidence = segmented_probability(
            [
                *[{"truth_safe": True} for _ in range(8)],
                {"truth_safe": False},
            ]
        )

        self.assertLess(mixed_probability, all_safe_probability)
        self.assertEqual(evidence["training_risky_count"], 1)

    def test_segmented_shadow_status_distinguishes_sufficient_evidence(self) -> None:
        self.assertEqual(
            segmented_shadow_status(passed=True, limited_evidence=True),
            "shadow_ready_limited",
        )
        self.assertEqual(
            segmented_shadow_status(passed=True, limited_evidence=False),
            "shadow_ready",
        )
        self.assertEqual(
            segmented_shadow_status(passed=False, limited_evidence=False),
            "rejected",
        )

    def test_population_near_miss_can_supply_boundary_evidence(self) -> None:
        self.assertTrue(
            has_segmented_boundary_evidence(
                reviewed_sentinel_count=0,
                population_near_miss_count=4,
            )
        )
        self.assertTrue(
            has_segmented_boundary_evidence(
                reviewed_sentinel_count=1,
                population_near_miss_count=0,
            )
        )
        self.assertFalse(
            has_segmented_boundary_evidence(
                reviewed_sentinel_count=0,
                population_near_miss_count=0,
            )
        )

    def test_exhaustive_small_population_can_supply_limited_support(self) -> None:
        self.assertTrue(
            has_segmented_training_support(
                training_count=6,
                population_match_count=8,
                reviewed_population_match_count=8,
                reviewed_population_all_safe=True,
            )
        )
        self.assertFalse(
            has_segmented_training_support(
                training_count=6,
                population_match_count=8,
                reviewed_population_match_count=7,
                reviewed_population_all_safe=True,
            )
        )
        self.assertFalse(
            has_segmented_training_support(
                training_count=6,
                population_match_count=8,
                reviewed_population_match_count=8,
                reviewed_population_all_safe=False,
            )
        )
        self.assertTrue(
            has_segmented_training_support(
                training_count=8,
                population_match_count=100,
                reviewed_population_match_count=8,
                reviewed_population_all_safe=True,
            )
        )

    def test_contract_helper_pattern_keeps_group_boundary(self) -> None:
        helper_text = "[actor.Custom('ES_ElLa')] texto"
        contract = {
            "relative_path": "contracts/example_l_spanish.yml",
            "calibration_group": "contracts",
            "candidate_text": helper_text,
        }
        non_contract = {
            **contract,
            "relative_path": "dlc/ep3/example_l_spanish.yml",
            "calibration_group": "dlc/ep3",
        }
        contract_without_helper = {
            **contract,
            "candidate_text": "Texto sem helper",
        }

        self.assertTrue(
            matches_segmented_pattern(contract, CONTRACT_ES_HELPER_PATTERN)
        )
        self.assertFalse(
            matches_segmented_pattern(non_contract, CONTRACT_ES_HELPER_PATTERN)
        )
        self.assertTrue(
            is_segmented_boundary_sentinel(
                non_contract,
                CONTRACT_ES_HELPER_PATTERN,
            )
        )
        self.assertTrue(
            is_segmented_boundary_sentinel(
                contract_without_helper,
                CONTRACT_ES_HELPER_PATTERN,
            )
        )

    def test_contract_helper_subgroups_do_not_overlap(self) -> None:
        suffix_only = {
            "relative_path": "contracts/example_l_spanish.yml",
            "calibration_group": "contracts",
            "candidate_text": (
                "Fico benévol[actor.Custom('ES_OA')] e "
                "animad[actor.Custom('ES_OA')]."
            ),
        }
        article_helper = {
            **suffix_only,
            "candidate_text": (
                "[actor.Custom('ES_ElLa')] personagem "
                "animad[actor.Custom('ES_OA')]."
            ),
        }
        detached_oa = {
            **suffix_only,
            "candidate_text": "Texto [actor.Custom('ES_OA')] separado.",
        }
        duplicated_vowel = {
            **suffix_only,
            "candidate_text": "Texto animado[actor.Custom('ES_OA')].",
        }
        invalid_function_word = {
            **suffix_only,
            "candidate_text": "Somos um[actor.Custom('ES_OA')].",
        }
        irregular_adjective = {
            **suffix_only,
            "candidate_text": "Parece bom[actor.Custom('ES_OA')].",
        }

        self.assertTrue(
            matches_segmented_pattern(
                suffix_only,
                CONTRACT_ES_OA_SUFFIX_PATTERN,
            )
        )
        self.assertFalse(
            matches_segmented_pattern(
                suffix_only,
                CONTRACT_ES_ARTICLE_PREPOSITION_PATTERN,
            )
        )
        self.assertFalse(
            matches_segmented_pattern(
                article_helper,
                CONTRACT_ES_OA_SUFFIX_PATTERN,
            )
        )
        self.assertTrue(
            matches_segmented_pattern(
                article_helper,
                CONTRACT_ES_ARTICLE_PREPOSITION_PATTERN,
            )
        )
        self.assertFalse(
            matches_segmented_pattern(
                detached_oa,
                CONTRACT_ES_OA_SUFFIX_PATTERN,
            )
        )
        self.assertFalse(
            matches_segmented_pattern(
                duplicated_vowel,
                CONTRACT_ES_OA_SUFFIX_PATTERN,
            )
        )
        self.assertFalse(
            matches_segmented_pattern(
                invalid_function_word,
                CONTRACT_ES_OA_SUFFIX_PATTERN,
            )
        )
        self.assertFalse(
            matches_segmented_pattern(
                irregular_adjective,
                CONTRACT_ES_OA_SUFFIX_PATTERN,
            )
        )

    def test_reviewed_risk_boundaries_become_blocking_features(self) -> None:
        base_row = {
            "relative_path": "contracts/task_contracts_l_spanish.yml",
            "source_key": "example",
            "english_text": "Example",
            "spanish_text": "Ejemplo",
            "old_text": "",
            "output_text": "",
            "evidence_source": "local_learning_candidate",
        }
        redundant = {
            **base_row,
            "candidate_text": (
                "[Select_CString(actor.IsLocalPlayer, 'continuará', 'continuará')]"
            ),
        }
        residual = {
            **base_row,
            "candidate_text": "Por favor, #EMP hacédmelo#! agora.",
        }

        self.assertIn(
            "RISK_REDUNDANT_SELECT_CSTRING_OPTIONS",
            language_features(redundant),
        )
        self.assertIn(
            "RISK_ACCENT_SENSITIVE_SPANISH_RESIDUE",
            language_features(residual),
        )


if __name__ == "__main__":
    unittest.main()
