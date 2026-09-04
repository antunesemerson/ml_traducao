from __future__ import annotations

import sys
import unittest
from collections import Counter, defaultdict
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from quality_mojibake_lexicon_shadow import repair_text_contextual
from local_quality_validator import validate_text


def lexicon(*entries: tuple[str, int]):
    words_by_length: dict[int, set[str]] = defaultdict(set)
    support: Counter[str] = Counter()
    for word, count in entries:
        normalized = word.casefold()
        words_by_length[len(normalized)].add(normalized)
        support[normalized] = count
    return dict(words_by_length), support


class MojibakeContextualRepairTests(unittest.TestCase):
    def test_repair_uses_portuguese_character_instead_of_ascii_false_positive(self) -> None:
        words, support = lexicon(("furto", 300), ("furão", 40))

        candidate, replacements, blockers, unresolved = repair_text_contextual(
            "um fur?o veloz",
            words,
            support,
            2,
        )

        self.assertEqual(candidate, "um furão veloz")
        self.assertEqual(replacements[0]["selection_reason"], "unique_portuguese_character")
        self.assertEqual(blockers, [])
        self.assertEqual(unresolved, [])

    def test_ascii_only_candidate_remains_unresolved(self) -> None:
        words, support = lexicon(("furto", 300))

        candidate, replacements, blockers, unresolved = repair_text_contextual(
            "um fur?o veloz",
            words,
            support,
            2,
        )

        self.assertEqual(candidate, "um fur?o veloz")
        self.assertEqual(replacements, [])
        self.assertIn("portuguese_candidate_missing", blockers)
        self.assertEqual(unresolved[0]["token"], "fur?o")

    def test_boundary_question_mark_is_not_rewritten_from_corpus_frequency(self) -> None:
        words, support = lexicon(("saberá", 400), ("saberes", 50))

        candidate, replacements, blockers, unresolved = repair_text_contextual(
            "Você quer saber?",
            words,
            support,
            2,
        )

        self.assertEqual(candidate, "Você quer saber?")
        self.assertEqual(replacements, [])
        self.assertIn("boundary_question_mark_ambiguous", blockers)
        self.assertEqual(unresolved[0]["token"], "saber?")

    def test_plural_suffix_after_gender_token_is_not_rewritten_as_accented_word(self) -> None:
        words, support = lexicon(("só", 4680), ("sé", 75), ("sí", 63))
        decisions = {
            "s?": {
                "decision": "canonical_replacement",
                "canonical_replacement": "só",
            }
        }
        output = (
            "Diga-me, tod[other_family_relative.Custom('ES_OA')] s "
            "[other_family_relative.GetWomenMen] são tão "
            "bel[other_family_relative.Custom('ES_OA')] s?"
        )

        candidate, replacements, blockers, unresolved = repair_text_contextual(
            output,
            words,
            support,
            2,
            {},
            decisions,
        )

        self.assertEqual(candidate, output)
        self.assertEqual(replacements, [])
        self.assertIn("gender_token_plural_suffix_spacing", blockers)
        self.assertNotIn("boundary_question_mark_ambiguous", blockers)
        self.assertEqual(unresolved[0]["lexical_token"], "s?")
        self.assertEqual(
            unresolved[0]["context_family"],
            "gender_token_plural_suffix_spacing",
        )

        issue_set = {item["code"] for item in validate_text(output)["issues"]}
        self.assertIn("gender_token_suffix_separated", issue_set)
        self.assertNotIn("replacement_question_mark_mojibake", issue_set)

    def test_repairs_multiple_confident_tokens_and_keeps_residual_signal(self) -> None:
        words, support = lexicon(
            ("bônus", 800),
            ("estão", 4600),
            ("estáo", 20),
            ("praça", 250),
            ("praia", 150),
        )

        candidate, replacements, blockers, unresolved = repair_text_contextual(
            "B?nus est?o na pra?a?",
            words,
            support,
            2,
        )

        self.assertEqual(candidate, "Bônus estão na pra?a?")
        self.assertEqual(
            [(item["original"], item["replacement"]) for item in replacements],
            [("B?nus", "Bônus"), ("est?o", "estão")],
        )
        self.assertIn("boundary_question_mark_ambiguous", blockers)
        self.assertEqual(unresolved[0]["token"], "pra?a?")

    def test_supervised_pattern_requires_two_distinct_confirmations(self) -> None:
        words, support = lexicon(("tem", 10000), ("têm", 2200), ("tám", 1000))

        one_vote = {"t?m": {"têm": 1}}
        candidate, replacements, _, _ = repair_text_contextual(
            "eles t?m",
            words,
            support,
            2,
            one_vote,
        )
        self.assertEqual(candidate, "eles t?m")
        self.assertEqual(replacements, [])

        two_votes = {"t?m": {"têm": 2}}
        candidate, replacements, blockers, unresolved = repair_text_contextual(
            "eles t?m",
            words,
            support,
            2,
            two_votes,
        )
        self.assertEqual(candidate, "eles têm")
        self.assertEqual(replacements[0]["selection_reason"], "supervised_pattern")
        self.assertEqual(blockers, [])
        self.assertEqual(unresolved, [])

    def test_boundary_pattern_decision_repairs_repeated_boundary_tokens(self) -> None:
        words, support = lexicon(("há", 6031), ("ha", 357))
        decisions = {
            "h?": {
                "decision": "canonical_replacement",
                "canonical_replacement": "há",
            }
        }

        candidate, replacements, blockers, unresolved = repair_text_contextual(
            "H? sinais e h? evidências",
            words,
            support,
            2,
            {},
            decisions,
        )

        self.assertEqual(candidate, "Há sinais e há evidências")
        self.assertEqual(len(replacements), 2)
        self.assertTrue(
            all(
                item["selection_reason"] == "supervised_boundary_pattern"
                for item in replacements
            )
        )
        self.assertEqual(blockers, [])
        self.assertEqual(unresolved, [])

    def test_context_required_boundary_decision_remains_unresolved(self) -> None:
        words, support = lexicon(("só", 4663), ("se", 46563))
        decisions = {"s?": {"decision": "context_required"}}

        candidate, replacements, blockers, unresolved = repair_text_contextual(
            "s? depois",
            words,
            support,
            2,
            {},
            decisions,
        )

        self.assertEqual(candidate, "s? depois")
        self.assertEqual(replacements, [])
        self.assertIn("human_context_required", blockers)
        self.assertEqual(unresolved[0]["reason"], "human_context_required")

    def test_boundary_review_only_exposes_accented_unicode_candidates(self) -> None:
        words, support = lexicon(
            ("há", 6031),
            ("ha", 357),
            ("hu", 162),
            ("hé", 54),
        )

        candidate, replacements, blockers, unresolved = repair_text_contextual(
            "h? sinais",
            words,
            support,
            2,
        )

        self.assertEqual(candidate, "h? sinais")
        self.assertEqual(replacements, [])
        self.assertIn("boundary_question_mark_ambiguous", blockers)
        self.assertEqual(
            [item["text"] for item in unresolved[0]["alternatives"]],
            ["há", "hé"],
        )
        self.assertEqual(unresolved[0]["candidate_count"], 2)
        self.assertEqual(unresolved[0]["diagnostic_candidate_count"], 4)

    def test_literal_newline_escape_is_not_misread_as_ne(self) -> None:
        words, support = lexicon(("é", 12000), ("né", 671), ("no", 20718))
        output = "Introdução\\n\\n? um desafio aberto"
        spanish = "Introducción\\n\\nEs un desafío abierto"

        candidate, replacements, blockers, unresolved = repair_text_contextual(
            output,
            words,
            support,
            2,
            {},
            {},
            "Introduction\\n\\nIt is an open challenge",
            spanish,
        )

        self.assertEqual(candidate, "Introdução\\n\\nÉ um desafio aberto")
        self.assertEqual(replacements[0]["original"], "?")
        self.assertEqual(replacements[0]["replacement"], "É")
        self.assertEqual(
            replacements[0]["selection_reason"],
            "structural_newline_aligned_spanish_copula",
        )
        self.assertEqual(replacements[0]["context_family"], "newline_sentence_start")
        self.assertEqual(output[replacements[0]["start"] : replacements[0]["end"]], "?")
        self.assertEqual(blockers, [])
        self.assertEqual(unresolved, [])

    def test_actual_newline_uses_the_same_structural_context(self) -> None:
        words, support = lexicon(("é", 12000))

        candidate, replacements, blockers, unresolved = repair_text_contextual(
            "Introdução\n\n? melhor aguardar",
            words,
            support,
            2,
            {},
            {},
            "Introduction\n\nIt is better to wait",
            "Introducción\n\nEs mejor esperar",
        )

        self.assertEqual(candidate, "Introdução\n\nÉ melhor aguardar")
        self.assertEqual(len(replacements), 1)
        self.assertEqual(blockers, [])
        self.assertEqual(unresolved, [])

    def test_unconfirmed_newline_context_remains_structural_and_unresolved(self) -> None:
        words, support = lexicon(("é", 12000), ("à", 5000), ("né", 671))

        candidate, replacements, blockers, unresolved = repair_text_contextual(
            "Introdução\\n\\n? talvez",
            words,
            support,
            2,
            {},
            {},
            "Introduction\\n\\nMaybe",
            "Introducción\\n\\nQuizá",
        )

        self.assertEqual(candidate, "Introdução\\n\\n? talvez")
        self.assertEqual(replacements, [])
        self.assertIn("boundary_question_mark_ambiguous", blockers)
        self.assertEqual(unresolved[0]["token"], "\\n?")
        self.assertEqual(unresolved[0]["lexical_token"], "?")
        self.assertEqual(unresolved[0]["context_family"], "newline_sentence_start")
        self.assertNotIn("né", [item["text"] for item in unresolved[0]["alternatives"]])

    def test_question_mark_attached_to_ck3_token_is_not_a_newline_candidate(self) -> None:
        words, support = lexicon(("é", 12000), ("à", 5000))

        candidate, replacements, blockers, unresolved = repair_text_contextual(
            "Introdução\\n\\n[person.GetName]?",
            words,
            support,
            2,
            {},
            {},
            "Introduction\\n\\n[person.GetName]?",
            "Introducción\\n\\n¿[person.GetName]?",
        )

        self.assertEqual(candidate, "Introdução\\n\\n[person.GetName]?")
        self.assertEqual(replacements, [])
        self.assertIn("boundary_question_mark_ambiguous", blockers)
        self.assertEqual(unresolved[0]["token"], "[estrutura]?")
        self.assertEqual(unresolved[0]["context_family"], "format_token_punctuation")
        self.assertEqual(unresolved[0]["alternatives"], [])


if __name__ == "__main__":
    unittest.main()
