from __future__ import annotations

import sys
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from quality_glossary_display_case_shadow import (
    CONFLICT_LANE,
    FLEXIBLE_LANE,
    REVIEW_LANE,
    SENSITIVE_LANE,
    align_glossary_calls,
    apply_case_repairs,
    build_records,
    canonical_headings,
    glossary_calls,
    has_case_loss_against,
    is_display_case_loss,
    manual_vote,
    policy_from_votes,
)


def row(
    segment_id: int,
    *,
    english: str,
    output: str,
    label: str | None = None,
    reason: str | None = None,
) -> dict[str, object]:
    return {
        "segment_id": segment_id,
        "relative_path": "events/example_l_spanish.yml",
        "source_key": f"example.{segment_id}",
        "english_text": english,
        "output_text": output,
        "human_label": label,
        "review_reason": reason,
        "human_locked": False,
    }


class GlossaryDisplayCaseShadowTests(unittest.TestCase):
    def test_parser_keeps_display_and_internal_key_separate(self) -> None:
        calls = glossary_calls("[Glossary( 'Romaioi', 'ROMAIOS_GLOSS' )]")

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["display"], "Romaioi")
        self.assertEqual(calls[0]["glossary_key"], "ROMAIOS_GLOSS")

    def test_case_loss_accepts_translated_display_with_lower_initial(self) -> None:
        self.assertTrue(
            is_display_case_loss("Autokrator", "autocrátor", None)
        )
        self.assertFalse(
            is_display_case_loss("Honsei", "Honsei", "honsei")
        )
        self.assertTrue(
            has_case_loss_against("Pacta Sunt Servanda", "Pacta sunt servanda")
        )

    def test_human_votes_distinguish_sensitive_and_flexible_keys(self) -> None:
        self.assertEqual(
            manual_vote(
                "structure_error",
                "alteração indevida dentro de glossary, removendo letra maiúscula",
            ),
            "case_sensitive",
        )
        self.assertEqual(
            manual_vote("contextual_exception", "termo comum em contexto"),
            "case_flexible",
        )
        self.assertIsNone(manual_vote("residual_spanish", "outro problema"))
        self.assertEqual(policy_from_votes(1, 1), "policy_conflict")

    def test_build_records_routes_known_and_unknown_keys(self) -> None:
        rows = [
            row(
                1,
                english="[Glossary( 'Romaioi', 'ROMAIOS_GLOSS' )]",
                output="[Glossary( 'romaioi', 'ROMAIOS_GLOSS' )]",
                label="structure_error",
                reason="ajuste incorreto dentro de glossary",
            ),
            row(
                2,
                english="[Glossary( 'Honsei', 'HONSEI_GLOSS' )]",
                output="[Glossary( 'honsei', 'HONSEI_GLOSS' )]",
                label="correct",
                reason="texto correto",
            ),
            row(
                3,
                english="[Glossary( 'Tengu', 'TENGU_GLOSS' )]",
                output="[Glossary( 'tengu', 'TENGU_GLOSS' )]",
            ),
        ]
        definitions = [
            {"source_key": "ROMAIOS_GLOSS", "output_text": "#bold Romaioi#! definição"},
            {"source_key": "HONSEI_GLOSS", "output_text": "#bold honsei#! definição"},
            {"source_key": "TENGU_GLOSS", "output_text": "#bold Tengu#! definição"},
        ]

        records, policies = build_records(
            rows,
            canonical_headings(definitions),
            score_run_id=405,
        )

        lanes = {record["segment_id"]: record["lane"] for record in records}
        self.assertEqual(lanes[1], SENSITIVE_LANE)
        self.assertEqual(lanes[2], FLEXIBLE_LANE)
        self.assertEqual(lanes[3], REVIEW_LANE)
        self.assertEqual(policies["ROMAIOS_GLOSS"]["policy"], "case_sensitive")
        self.assertEqual(policies["HONSEI_GLOSS"]["policy"], "case_flexible")
        self.assertEqual(policies["TENGU_GLOSS"]["policy"], "unknown")

    def test_canonical_heading_falls_back_from_corrupt_output_to_english(self) -> None:
        headings = canonical_headings(
            [
                {
                    "source_key": "DAIJO_DAIJIN_GLOSS",
                    "english_text": "#bold Daijō-daijin#! definition",
                    "output_text": "#bold Daij?-daijin#! definição",
                }
            ]
        )

        self.assertEqual(headings["DAIJO_DAIJIN_GLOSS"], "Daijō-daijin")

    def test_repair_changes_only_display_case(self) -> None:
        text = "dos [Glossary( 'romaioi', 'ROMAIOS_GLOSS' )]"
        occurrence = align_glossary_calls(
            "the [Glossary( 'Romaioi', 'ROMAIOS_GLOSS' )]",
            text,
        )[0]
        occurrence["canonical_heading"] = "Romaioi"

        candidate, replacements = apply_case_repairs(text, [occurrence])

        self.assertEqual(
            candidate,
            "dos [Glossary( 'Romaioi', 'ROMAIOS_GLOSS' )]",
        )
        self.assertEqual(replacements[0]["glossary_key"], "ROMAIOS_GLOSS")
        self.assertIn("'ROMAIOS_GLOSS'", candidate)

    def test_explicit_key_policy_overrides_ambiguous_segment_votes(self) -> None:
        rows = [
            row(
                1,
                english="[Glossary( 'Romaioi', 'ROMAIOS_GLOSS' )]",
                output="[Glossary( 'romaioi', 'ROMAIOS_GLOSS' )]",
            )
        ]

        records, policies = build_records(
            rows,
            {},
            score_run_id=405,
            explicit_policies={
                "ROMAIOS_GLOSS": {
                    "policy": "case_sensitive",
                    "reason": "A chave representa um nome próprio.",
                    "reviewer": "dashboard_human_review",
                    "updated_at": "2026-07-30T12:00:00",
                }
            },
        )

        self.assertEqual(records[0]["lane"], SENSITIVE_LANE)
        self.assertEqual(policies["ROMAIOS_GLOSS"]["policy"], "case_sensitive")
        self.assertTrue(policies["ROMAIOS_GLOSS"]["explicit_policy"])

    def test_repair_can_restore_internal_case_without_changing_letters(self) -> None:
        text = "[Glossary( 'Pacta sunt servanda', 'PACTA_GLOSS' )]"
        occurrence = align_glossary_calls(
            "[Glossary( 'Pacta Sunt Servanda', 'PACTA_GLOSS' )]",
            text,
        )[0]
        occurrence["canonical_heading"] = None

        candidate, _ = apply_case_repairs(text, [occurrence])

        self.assertEqual(
            candidate,
            "[Glossary( 'Pacta Sunt Servanda', 'PACTA_GLOSS' )]",
        )

    def test_conflicting_votes_never_become_review_or_apply_ready(self) -> None:
        rows = [
            row(
                1,
                english="[Glossary( 'Romaioi', 'ROMAIOS_GLOSS' )]",
                output="[Glossary( 'romaioi', 'ROMAIOS_GLOSS' )]",
                label="structure_error",
                reason="capitalização de Glossary",
            ),
            row(
                2,
                english="[Glossary( 'Romaioi', 'ROMAIOS_GLOSS' )]",
                output="[Glossary( 'romaioi', 'ROMAIOS_GLOSS' )]",
                label="contextual_exception",
                reason="minúscula aceita neste contexto",
            ),
        ]

        records, policies = build_records(rows, {}, score_run_id=405)

        self.assertEqual(policies["ROMAIOS_GLOSS"]["policy"], "policy_conflict")
        self.assertTrue(all(record["lane"] == CONFLICT_LANE for record in records))
        self.assertTrue(all(not record["ready_for_apply"] for record in records))


if __name__ == "__main__":
    unittest.main()
