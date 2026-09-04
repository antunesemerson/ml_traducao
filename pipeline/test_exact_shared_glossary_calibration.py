from __future__ import annotations

import sqlite3
import unittest

from ml_score_segments import (
    calibrated_model_probability,
    final_decision,
    insert_items,
)
from quality_exact_shared_glossary_calibration import (
    calibrate_safe_probability,
    matches_operational_scope,
)


GLOSSARY = "[Glossary( 'hellenothreskos', 'HELLENOTHRESKOS_GLOSS' )]"


def glossary_row(**overrides) -> dict[str, object]:
    row: dict[str, object] = {
        "relative_path": "custom_localization/example_l_spanish.yml",
        "candidate_text": GLOSSARY,
        "output_text": GLOSSARY,
        "english_text": GLOSSARY,
        "spanish_text": GLOSSARY,
    }
    row.update(overrides)
    return row


class ExactSharedGlossaryCalibrationTest(unittest.TestCase):
    def test_exact_shared_glossary_receives_evidence_backed_probability(self) -> None:
        result = calibrate_safe_probability(glossary_row(), 0.0742)

        self.assertTrue(result["applied"])
        self.assertEqual(result["policy_id"], "exact_shared_glossary_v1")
        self.assertEqual(result["source_shadow_run_id"], 171)
        self.assertAlmostEqual(result["calibrated_safe_probability"], 0.96875)

    def test_scope_requires_custom_localization_and_current_output(self) -> None:
        self.assertFalse(
            matches_operational_scope(
                glossary_row(relative_path="events/example_l_spanish.yml")
            )
        )
        self.assertFalse(
            matches_operational_scope(glossary_row(output_text="[Glossary('other','OTHER')]"))
        )

    def test_near_misses_remain_uncalibrated(self) -> None:
        different_term = glossary_row(
            candidate_text="[Glossary( 'de los hellenothreskos', 'HELLENOTHRESKOS_GLOSS' )]",
            output_text="[Glossary( 'de los hellenothreskos', 'HELLENOTHRESKOS_GLOSS' )]",
        )
        different_key = glossary_row(
            candidate_text="[Glossary( 'hellenothreskos', 'OTHER_GLOSS' )]",
            output_text="[Glossary( 'hellenothreskos', 'OTHER_GLOSS' )]",
        )

        self.assertFalse(calibrate_safe_probability(different_term, 0.08)["applied"])
        self.assertFalse(calibrate_safe_probability(different_key, 0.08)["applied"])

    def test_higher_raw_probability_is_never_reduced(self) -> None:
        result = calibrate_safe_probability(glossary_row(), 0.99)

        self.assertFalse(result["applied"])
        self.assertAlmostEqual(result["calibrated_safe_probability"], 0.99)

    def test_scorer_preserves_raw_probability_and_provenance(self) -> None:
        row = glossary_row(
            segment_id=1,
            source_key="hellenothreskos",
            source_line_number=1,
            old_text=GLOSSARY,
            candidate_text_source="output",
            confirmation_level="auto_confirmed",
            locked=0,
        )
        safe_probability, probabilities = calibrated_model_probability(
            row,
            0.0742,
            {
                "auto_safe": 0.0742,
                "needs_autofix": 0.70,
                "needs_human": 0.2258,
            },
        )
        result = final_decision(
            row,
            model_action="auto_safe",
            safe_probability=safe_probability,
            model_confidence=safe_probability,
            probability_by_class=probabilities,
            safe_threshold=0.86,
        )

        self.assertAlmostEqual(sum(probabilities.values()), 1.0)
        self.assertEqual(result["final_action"], "auto_safe")
        self.assertAlmostEqual(result["raw_model_safe_probability"], 0.0742)
        self.assertAlmostEqual(result["model_safe_probability"], 0.96875)
        self.assertEqual(result["score_calibration"], "exact_shared_glossary_v1")
        self.assertEqual(result["score_calibration_source_run_id"], 171)
        self.assertTrue(
            any(reason.startswith("score_calibration:") for reason in result["reasons"])
        )

    def test_score_insert_persists_calibration_columns(self) -> None:
        con = sqlite3.connect(":memory:")
        con.execute(
            """
            CREATE TABLE ml_score_items (
              run_id INTEGER,
              segment_id INTEGER,
              relative_path TEXT,
              source_key TEXT,
              source_line_number INTEGER,
              candidate_text TEXT,
              model_action TEXT,
              final_action TEXT,
              risk_class TEXT,
              model_safe_probability REAL,
              raw_model_safe_probability REAL,
              score_calibration TEXT,
              score_calibration_source_run_id INTEGER,
              model_confidence REAL,
              token_status TEXT,
              issue_count INTEGER,
              high_issue_count INTEGER,
              medium_issue_count INTEGER,
              word_count INTEGER,
              deterministic_blocked INTEGER,
              confirmation_level TEXT,
              locked INTEGER,
              reasons_json TEXT,
              issues_json TEXT,
              created_at TEXT,
              UNIQUE(run_id, segment_id)
            )
            """
        )
        item = {
            "segment_id": 1,
            "relative_path": "custom_localization/example_l_spanish.yml",
            "source_key": "hellenothreskos",
            "source_line_number": 1,
            "candidate_text": GLOSSARY,
            "model_action": "auto_safe",
            "final_action": "auto_safe",
            "risk_class": "low",
            "model_safe_probability": 0.96875,
            "raw_model_safe_probability": 0.0742,
            "score_calibration": "exact_shared_glossary_v1",
            "score_calibration_source_run_id": 171,
            "model_confidence": 0.96875,
            "token_status": "ok",
            "issue_count": 0,
            "high_issue_count": 0,
            "medium_issue_count": 0,
            "word_count": 0,
            "deterministic_blocked": 0,
            "confirmation_level": "auto_confirmed",
            "locked": 0,
            "reasons": [],
            "issues": [],
        }

        insert_items(con, 99, [item], "2026-07-25T19:20:00")
        stored = con.execute(
            """
            SELECT model_safe_probability, raw_model_safe_probability,
                   score_calibration, score_calibration_source_run_id
            FROM ml_score_items
            """
        ).fetchone()

        self.assertEqual(
            stored,
            (0.96875, 0.0742, "exact_shared_glossary_v1", 171),
        )
        con.close()


if __name__ == "__main__":
    unittest.main()
