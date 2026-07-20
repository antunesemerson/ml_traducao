from __future__ import annotations

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from apply_safe_output_updates import protected_tokens
from quality_token_punctuation_boundary_shadow import repair_token_punctuation_boundaries


class TokenPunctuationBoundaryTest(unittest.TestCase):
    def assert_repair(self, original: str, expected: str, count: int = 1) -> None:
        candidate, repairs = repair_token_punctuation_boundaries(original)
        self.assertEqual(candidate, expected)
        self.assertEqual(len(repairs), count)
        self.assertEqual(protected_tokens(original), protected_tokens(candidate))

    def test_repairs_bracket_token_before_comma(self) -> None:
        self.assert_repair("os [traits|lE] , e", "os [traits|lE], e")

    def test_repairs_macro_before_period(self) -> None:
        self.assert_repair("Valor $VALUE$ .", "Valor $VALUE$.")

    def test_repairs_style_close_and_icon_boundaries(self) -> None:
        self.assert_repair("#P bônus#! : @gold! ;", "#P bônus#!: @gold!;", 2)

    def test_does_not_change_token_internals(self) -> None:
        original = "[Concept( 'stationed' , 'acantonados' )|E]"
        self.assert_repair(original, original, 0)

    def test_does_not_change_plain_text_spacing(self) -> None:
        original = "texto , outro"
        self.assert_repair(original, original, 0)

    def test_does_not_treat_question_mark_as_safe_punctuation(self) -> None:
        original = "[character.GetName] ? importante"
        self.assert_repair(original, original, 0)


if __name__ == "__main__":
    unittest.main()
