from __future__ import annotations

import unittest

from local_quality_validator import (
    SORTED_SPANISH_RESIDUE_PHRASES,
    SORTED_SPANISH_RESIDUE_WORDS,
    collapse_redundant_select_cstring,
    count_accent_sensitive_spanish_residue,
    count_spanish_residue,
    count_spanish_residue_regex,
    redundant_select_cstring_literals,
    validate_text,
)


class SpanishResidueMatcherTests(unittest.TestCase):
    def assert_matches_legacy(self, text: str | None) -> None:
        self.assertEqual(count_spanish_residue_regex(text), count_spanish_residue(text), text)

    def test_every_configured_term_matches_the_legacy_detector(self) -> None:
        for term in (*SORTED_SPANISH_RESIDUE_PHRASES, *SORTED_SPANISH_RESIDUE_WORDS):
            with self.subTest(term=term):
                self.assert_matches_legacy(f"@@ {term} @@")

    def test_boundary_variants_match_the_legacy_detector(self) -> None:
        samples = (
            *SORTED_SPANISH_RESIDUE_PHRASES[::17],
            *SORTED_SPANISH_RESIDUE_WORDS[::31],
        )
        for term in samples:
            for text in (f"x{term}", f"{term}x", f"-{term}", f"{term}-"):
                with self.subTest(term=term, text=text):
                    self.assert_matches_legacy(text)

    def test_normalization_protected_tokens_and_output_order_match_legacy(self) -> None:
        phrases_with_spaces = [term for term in SORTED_SPANISH_RESIDUE_PHRASES if " " in term]
        phrase = phrases_with_spaces[0]
        normalized_whitespace = " \t\n ".join(phrase.split())
        samples = (
            None,
            "",
            f"@@ {normalized_whitespace} @@",
            "[Select_CString(local.IsFemale, 'ella', 'el')] conteúdo preservado",
            " | ".join((*SORTED_SPANISH_RESIDUE_PHRASES, *SORTED_SPANISH_RESIDUE_WORDS)),
        )
        for text in samples:
            with self.subTest(text=text):
                self.assert_matches_legacy(text)

    def test_accent_sensitive_spanish_does_not_block_portuguese_accents(self) -> None:
        spanish = (
            "nessa #EMP escoria#!, no #EMP estudio#!, "
            "e com #EMP esto#!; por favor #EMP hacédmelo#!"
        )
        portuguese = "nessa escória, no estúdio, isto está correto"

        count, matches = count_accent_sensitive_spanish_residue(spanish)
        portuguese_count, portuguese_matches = (
            count_accent_sensitive_spanish_residue(portuguese)
        )

        self.assertEqual(count, 4)
        self.assertEqual(matches, ["escoria", "estudio", "esto", "hacedmelo"])
        self.assertEqual((portuguese_count, portuguese_matches), (0, []))
        self.assertIn(
            "accent_sensitive_spanish_residue",
            {issue["code"] for issue in validate_text(spanish)["issues"]},
        )

    def test_portuguese_tu_is_not_a_standalone_spanish_residue(self) -> None:
        portuguese = (
            "Tu não amas [marriage_vassal.GetFirstNameNoTooltip], "
            "só queres que eu #EMP sofra!#!"
        )

        self.assertEqual(count_spanish_residue_regex(portuguese), (0, []))
        self.assertEqual(count_spanish_residue(portuguese), (0, []))
        self.assertNotIn(
            "spanish_residue",
            {issue["code"] for issue in validate_text(portuguese)["issues"]},
        )

    def test_localization_plural_suffix_is_not_a_missing_space(self) -> None:
        valid_plural_suffix = (
            "Os [defender.GetFaith.GetAdjectiveNoTooltip]s já se reuniram."
        )
        glued_human_word = (
            "Os [defender.GetFaith.GetAdjectiveNoTooltip]seguem reunidos."
        )

        valid_codes = {issue["code"] for issue in validate_text(valid_plural_suffix)["issues"]}
        glued_codes = {issue["code"] for issue in validate_text(glued_human_word)["issues"]}

        self.assertNotIn("missing_space_after_token", valid_codes)
        self.assertIn("missing_space_after_token", glued_codes)

    def test_redundant_select_cstring_only_flags_equal_literal_branches(self) -> None:
        repeated = (
            "[Select_CString(And(actor.IsFemale, actor.IsLocalPlayer), "
            "'continuará', 'continuará')]"
        )
        meaningful = "[Select_CString(actor.IsFemale, 'a dama', 'o cavalheiro')]"

        self.assertEqual(
            redundant_select_cstring_literals(repeated),
            ["continuará"],
        )
        self.assertEqual(redundant_select_cstring_literals(meaningful), [])
        self.assertIn(
            "redundant_select_cstring_options",
            {issue["code"] for issue in validate_text(repeated)["issues"]},
        )

    def test_redundant_select_cstring_collapse_is_exact_and_conservative(self) -> None:
        repeated = (
            "[Select_CString(And(actor.IsFemale, actor.IsLocalPlayer), "
            "'continuará', 'continuará')]"
        )
        normalized_only = (
            "[Select_CString(actor.IsFemale, 'Você', 'você')]"
        )
        filtered = (
            "[Select_CString(actor.IsFemale, 'nome', 'nome')|U]"
        )

        candidate, repairs = collapse_redundant_select_cstring(repeated)

        self.assertEqual(candidate, "continuará")
        self.assertEqual(len(repairs), 1)
        self.assertEqual(
            collapse_redundant_select_cstring(normalized_only),
            (normalized_only, []),
        )
        self.assertEqual(
            collapse_redundant_select_cstring(filtered),
            (filtered, []),
        )


if __name__ == "__main__":
    unittest.main()
