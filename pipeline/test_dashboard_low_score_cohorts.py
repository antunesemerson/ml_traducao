from __future__ import annotations

import unittest

from dashboard.backend import _low_score_cohort


class LowScoreCohortTests(unittest.TestCase):
    def test_explicit_issue_has_highest_priority(self) -> None:
        record = {
            "issue_count": 1,
            "token_status": "mismatch",
            "score_action": "auto_safe",
        }
        self.assertEqual(_low_score_cohort(record), "explicit_text_issue")

    def test_structural_block_precedes_auto_safe(self) -> None:
        record = {
            "issue_count": 0,
            "token_status": "mismatch",
            "score_action": "auto_safe",
        }
        self.assertEqual(_low_score_cohort(record), "structural_block_without_issue")

    def test_safe_and_preserved_cohorts_are_informational(self) -> None:
        self.assertEqual(
            _low_score_cohort({"issue_count": 0, "score_action": "auto_safe"}),
            "deterministic_safe_but_low_score",
        )
        self.assertEqual(
            _low_score_cohort(
                {
                    "issue_count": 0,
                    "score_action": "needs_human",
                    "score_candidate_text": "texto preservado",
                    "old_text": "texto preservado",
                }
            ),
            "unchanged_or_preserved_text",
        )

    def test_unknown_low_confidence_remains_visible(self) -> None:
        record = {
            "issue_count": 0,
            "score_action": "needs_human",
            "score_candidate_text": "candidato",
            "old_text": "old",
            "spanish_text": "es",
            "english_text": "en",
        }
        self.assertEqual(
            _low_score_cohort(record),
            "low_confidence_without_specific_evidence",
        )


if __name__ == "__main__":
    unittest.main()
