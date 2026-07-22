from __future__ import annotations

import sys
import sqlite3
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

import local_quality_validator  # noqa: E402
from apply_safe_output_updates import protected_tokens  # noqa: E402
from ck3_dynamic_expression import iter_expression_spans, iter_string_literal_spans  # noqa: E402
from quality_promotion_cycle import load_providers  # noqa: E402
from quality_spanish_dynamic_literal_pairwise_evidence import (  # noqa: E402
    EVIDENCE_TYPE,
    prepare_evidence,
    reconcile_active_evidence,
)
from quality_spanish_dynamic_literal_shadow import (  # noqa: E402
    PROMOTION_SAFE_LITERAL_SOURCES,
    protected_tokens_after_intentional_elision,
    repair_dynamic_literals,
    requires_sentence_composition,
    source_token_status_with_intentional_elision,
)


class SpanishDynamicLiteralShadowTests(unittest.TestCase):
    def test_translates_local_player_copula_pair(self) -> None:
        original = "[Select_CString( actor.IsLocalPlayer, 'eres', 'es' )] responsável"

        candidate, repairs = repair_dynamic_literals(original)

        self.assertEqual(
            candidate,
            "[Select_CString( actor.IsLocalPlayer, 'é', 'é' )] responsável",
        )
        self.assertEqual(len(repairs), 2)
        self.assertEqual(protected_tokens(original), protected_tokens(candidate))
        self.assertNotIn(
            "spanish_residue_in_literal",
            {item["code"] for item in local_quality_validator.validate_text(candidate)["issues"]},
        )

    def test_translates_nested_concept_literals_without_changing_concept_key(self) -> None:
        original = "[Concept('vassal',Select_CString(actor.IsFemale,'vasalla','vasallo'))|E]"

        candidate, repairs = repair_dynamic_literals(original)

        self.assertEqual(
            candidate,
            "[Concept('vassal',Select_CString(actor.IsFemale,'vassala','vassalo'))|E]",
        )
        self.assertEqual([item["literal_index"] for item in repairs], [2, 3])
        self.assertEqual(protected_tokens(original), protected_tokens(candidate))

    def test_parser_keeps_nested_brackets_and_quoted_closing_bracket_in_one_expression(self) -> None:
        original = (
            "prefix [Concept('vassal]label',Select_CString(actor.HasFlag('[x]'),"
            "'vasalla','vasallo'))|E] suffix"
        )

        expressions = list(iter_expression_spans(original))
        literals = list(iter_string_literal_spans(expressions[0].text))

        self.assertEqual(len(expressions), 1)
        self.assertEqual(expressions[0].text, original[7:-7])
        self.assertEqual(
            [item.value for item in literals],
            ["vassal]label", "[x]", "vasalla", "vasallo"],
        )

    def test_translates_high_confidence_past_tense_pair(self) -> None:
        original = "[Select_CString( actor.IsLocalPlayer, 'diste', 'dio' )] um presente"

        candidate, repairs = repair_dynamic_literals(original)

        self.assertEqual(
            candidate,
            "[Select_CString( actor.IsLocalPlayer, 'deu', 'deu' )] um presente",
        )
        self.assertEqual(len(repairs), 2)

    def test_removes_exact_redundant_dynamic_verb_without_double_space(self) -> None:
        original = "[actor.GetName] [Select_CString(actor.IsLocalPlayer,'ganaste','ganó')] ganhou ouro"

        candidate, repairs = repair_dynamic_literals(original)

        self.assertEqual(candidate, "[actor.GetName] ganhou ouro")
        self.assertEqual(
            {item["action"] for item in repairs},
            {"remove_redundant_dynamic_literal"},
        )
        self.assertTrue(
            protected_tokens_after_intentional_elision(original, candidate, repairs)
        )

    def test_removes_only_allowlisted_semantically_redundant_dynamic_verbs(self) -> None:
        cases = {
            "[actor.GetName] [Select_CString(actor.IsLocalPlayer,'ganaste','ganó')] adquiriu ouro": (
                "[actor.GetName] adquiriu ouro"
            ),
            "[actor.GetName] [Select_CString(actor.IsLocalPlayer,'hiciste','hizo')] trapaceou": (
                "[actor.GetName] trapaceou"
            ),
            "[actor.GetName] [Select_CString(actor.IsLocalPlayer,'tuviste','tuvo')] foi obrigado": (
                "[actor.GetName] foi obrigado"
            ),
        }

        for original, expected in cases.items():
            with self.subTest(original=original):
                candidate, repairs = repair_dynamic_literals(original)
                self.assertEqual(candidate, expected)
                self.assertEqual(
                    {item["action"] for item in repairs},
                    {"remove_semantically_redundant_dynamic_literal"},
                )
                self.assertTrue(
                    protected_tokens_after_intentional_elision(original, candidate, repairs)
                )

    def test_intentional_elision_rejects_any_unrelated_token_loss(self) -> None:
        original = (
            "[actor.GetName] [Select_CString(actor.IsLocalPlayer,'ganaste','ganó')] adquiriu ouro"
        )
        candidate, repairs = repair_dynamic_literals(original)

        self.assertFalse(
            protected_tokens_after_intentional_elision(
                original,
                candidate.replace("[actor.GetName] ", ""),
                repairs,
            )
        )
        self.assertEqual(
            source_token_status_with_intentional_elision(original, candidate, repairs),
            "intentional_elision",
        )

    def test_marks_partial_pair_as_unresolved(self) -> None:
        original = "[Select_CString(actor.IsLocalPlayer,'desconocida','ganó')] ouro"

        candidate, repairs = repair_dynamic_literals(original)

        self.assertIn("'desconocida'", candidate)
        self.assertEqual(repairs[0]["unresolved_literals"], ["desconocida"])

    def test_leaves_context_sensitive_pronouns_for_composition_review(self) -> None:
        original = "[Select_CString( actor.IsLocalPlayer, 'te', 'le' )] contou"

        candidate, repairs = repair_dynamic_literals(original)

        self.assertEqual(candidate, original)
        self.assertEqual(repairs, [])

    def test_does_not_translate_technical_literal(self) -> None:
        original = "[GetTitleByKey('c_toledo').GetName]"

        candidate, repairs = repair_dynamic_literals(original)

        self.assertEqual(candidate, original)
        self.assertEqual(repairs, [])

    def test_provider_is_registered_without_package_version(self) -> None:
        provider = next(item for item in load_providers() if item.provider_id == "spanish_dynamic_literal")

        self.assertEqual(provider.evidence_type, "deterministic_spanish_dynamic_literal_repair")
        self.assertNotRegex(provider.shadow_script.name.casefold(), r"(^|[_-])v\d+($|[_-])")
        self.assertNotRegex(provider.evidence_script.name.casefold(), r"(^|[_-])v\d+($|[_-])")

    def test_context_guarded_past_tense_mapping_enters_automatic_scope(self) -> None:
        self.assertIn("hiciste", PROMOTION_SAFE_LITERAL_SOURCES)
        self.assertIn("vasallo", PROMOTION_SAFE_LITERAL_SOURCES)

    def test_allows_past_tense_pair_with_nominal_complement(self) -> None:
        original = (
            "[actor.GetShortUIName|U] "
            "[Select_CString(actor.IsLocalPlayer,'hiciste','hizo')] preparativos"
        )

        candidate, repairs = repair_dynamic_literals(original)

        self.assertIn("'fez','fez'", candidate.replace(" ", ""))
        self.assertFalse(any(requires_sentence_composition(item) for item in repairs))

    def test_composes_allowlisted_translated_verb_before_existing_finite_verb(self) -> None:
        original = (
            "[actor.GetShortUIName|U] "
            "[Select_CString(actor.IsLocalPlayer,'ganaste','ganó')] adquiriu experiência"
        )

        candidate, repairs = repair_dynamic_literals(original)

        self.assertEqual(candidate, "[actor.GetShortUIName|U] adquiriu experiência")
        self.assertFalse(any(requires_sentence_composition(item) for item in repairs))

    def test_allows_safe_object_pronoun_after_preposition(self) -> None:
        original = "trapaceou contra [LocalPlayerString('ti',actor.GetName)]"

        candidate, repairs = repair_dynamic_literals(original)

        self.assertEqual(candidate, "trapaceou contra [LocalPlayerString('você',actor.GetName)]")
        self.assertFalse(any(requires_sentence_composition(item) for item in repairs))

    def test_blocks_translated_verb_after_incompatible_preposition(self) -> None:
        original = (
            "foi decapitado por "
            "[Select_CString(actor.IsLocalPlayer,'hiciste','hizo')] e usou a cabeça"
        )

        _, repairs = repair_dynamic_literals(original)

        self.assertTrue(any(requires_sentence_composition(item) for item in repairs))

    def test_does_not_semantically_elide_causative_or_prepositional_context(self) -> None:
        originals = [
            (
                "[target.GetName] lhe "
                "[Select_CString(target.IsLocalPlayer,'hiciste','hizo')] matar de fome"
            ),
            (
                "foi decapitado por "
                "[Select_CString(actor.IsLocalPlayer,'hiciste','hizo')] deu um presente"
            ),
        ]

        for original in originals:
            with self.subTest(original=original):
                candidate, repairs = repair_dynamic_literals(original)
                self.assertIn("Select_CString", candidate)
                self.assertTrue(any(requires_sentence_composition(item) for item in repairs))

    def test_blocks_empty_and_reflexive_finite_complements(self) -> None:
        _, empty_repairs = repair_dynamic_literals(
            "[actor.GetName] [Select_CString(actor.IsLocalPlayer,'hiciste','hizo')]"
        )
        _, reflexive_repairs = repair_dynamic_literals(
            "[actor.GetName] [LocalPlayerString('recibiste','recibió')] se machucou"
        )

        self.assertTrue(any(requires_sentence_composition(item) for item in empty_repairs))
        self.assertTrue(any(requires_sentence_composition(item) for item in reflexive_repairs))

    def test_blocks_causative_starvation_composition(self) -> None:
        original = (
            "[target.GetName] lhe "
            "[Select_CString(target.IsLocalPlayer,'hiciste','hizo')] matar de fome"
        )

        _, repairs = repair_dynamic_literals(original)

        self.assertTrue(any(requires_sentence_composition(item) for item in repairs))

    def test_reconciles_only_current_provider_evidence_as_active(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE ml_pairwise_quality_evidence (
                evidence_type TEXT,
                last_run_id INTEGER,
                training_eligible INTEGER,
                promotion_eligible INTEGER
            )
            """
        )
        conn.executemany(
            "INSERT INTO ml_pairwise_quality_evidence VALUES (?, ?, 1, 1)",
            [(EVIDENCE_TYPE, 10), (EVIDENCE_TYPE, 11), ("other_provider", 10)],
        )

        reconcile_active_evidence(conn, 11)

        rows = conn.execute(
            "SELECT evidence_type, last_run_id, training_eligible, promotion_eligible "
            "FROM ml_pairwise_quality_evidence ORDER BY evidence_type, last_run_id"
        ).fetchall()
        self.assertEqual(rows[0], (EVIDENCE_TYPE, 10, 0, 0))
        self.assertEqual(rows[1], (EVIDENCE_TYPE, 11, 1, 0))
        self.assertEqual(rows[2], ("other_provider", 10, 1, 1))

        reconcile_active_evidence(conn, None)
        active_count = conn.execute(
            "SELECT COUNT(*) FROM ml_pairwise_quality_evidence "
            "WHERE evidence_type = ? AND training_eligible = 1",
            (EVIDENCE_TYPE,),
        ).fetchone()[0]
        self.assertEqual(active_count, 0)
        conn.close()

    def test_pairwise_evidence_accepts_only_the_audited_dynamic_token_elision(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE ml_score_runs (id INTEGER, model_run_id INTEGER)")
        conn.execute(
            "CREATE TABLE ml_score_items ("
            "run_id INTEGER, segment_id INTEGER, candidate_text TEXT, "
            "model_safe_probability REAL)"
        )
        baseline = (
            "[actor.GetName] "
            "[Select_CString(actor.IsLocalPlayer,'ganaste','ganó')] adquiriu ouro"
        )
        conn.execute("INSERT INTO ml_score_runs VALUES (390, 17)")
        conn.execute("INSERT INTO ml_score_items VALUES (390, 2733, ?, 0.01)", (baseline,))
        shadow_rows = [
            {
                "score_run_id": 390,
                "segment_id": 2733,
                "relative_path": "events/test_l_spanish.yml",
                "source_key": "test_key",
                "blockers": [],
                "token_integrity_ok": True,
                "calibrated_candidate_score": 0.03,
                "raw_candidate_score": 0.02,
            }
        ]

        evidence = prepare_evidence(conn, shadow_rows)

        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["candidate_text"], "[actor.GetName] adquiriu ouro")
        self.assertAlmostEqual(evidence[0]["pairwise_delta"], 0.02)
        conn.close()


if __name__ == "__main__":
    unittest.main()
