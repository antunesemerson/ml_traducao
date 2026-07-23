from __future__ import annotations

import unittest

from local_quality_validator import (
    SORTED_SPANISH_RESIDUE_PHRASES,
    SORTED_SPANISH_RESIDUE_WORDS,
    count_spanish_residue,
    count_spanish_residue_regex,
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


if __name__ == "__main__":
    unittest.main()
