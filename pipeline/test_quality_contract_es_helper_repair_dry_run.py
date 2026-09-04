from __future__ import annotations

import unittest

import quality_contract_es_helper_repair_dry_run as dry_run


def row(
    text: str,
    *,
    segment_id: int,
    source_key: str,
) -> dict:
    return {
        "run_id": 402,
        "segment_state_run_id": 777,
        "segment_id": segment_id,
        "relative_path": "contracts/task_contracts_l_spanish.yml",
        "source_key": source_key,
        "english_text": "English source",
        "candidate_text": text,
        "current_output_text": text,
        "model_safe_probability": 0.02,
        "human_locked": 0,
        "is_closed": 1,
        "needs_output_apply": 0,
    }


class ContractEsHelperRepairDryRunTest(unittest.TestCase):
    def test_exact_rewrite_refuses_stale_fragment(self) -> None:
        record = dry_run.build_record(
            row(
                "Texto que mudou [employer.Custom('ES_ElLa')].",
                segment_id=11678,
                source_key=(
                    "laamp_base_learning_contract_events.4017.desc.success.standard"
                ),
            )
        )

        self.assertEqual(record["lane"], dry_run.BLOCKED_LANE)
        self.assertIn("expected_fragment_mismatch", record["blockers"])
        self.assertEqual(record["candidate_text"], record["original_text"])

    def test_removes_redundant_gender_token_only_for_allowlisted_segment(self) -> None:
        original = (
            "\"Bem-vindo, viajante[ROOT.Char.Custom('ES_OA')]!\", "
            "[local_1.Custom('ES_ElLa')]"
        )
        record = dry_run.build_record(
            row(
                original,
                segment_id=12157,
                source_key="ep3_contract_event.0006.desc_locals_temple",
            )
        )

        self.assertEqual(record["lane"], dry_run.ELIGIBLE_LANE)
        self.assertIn("\"Bem-vindo, viajante!\"", record["candidate_text"])
        self.assertEqual(
            record["token_integrity_status"],
            "allowlisted_token_delta",
        )
        self.assertEqual(record["post_issue_codes"], [])

    def test_repairs_embedded_gender_word_without_changing_token(self) -> None:
        original = (
            "Um dos "
            "[task_contract_employer.Custom('KnightCulturePluralNoTooltip')|l]s, "
            "apelo de seu am[task_contract_employer.Custom('ES_OA')]a: "
            "[task_contract_employer.Custom('ES_ElLa')]"
        )
        record = dry_run.build_record(
            row(
                original,
                segment_id=12427,
                source_key="ep3_contract_event.0060.desc",
            )
        )

        self.assertEqual(record["lane"], dry_run.ELIGIBLE_LANE)
        self.assertIn(
            "amig[task_contract_employer.Custom('ES_OA')]:",
            record["candidate_text"],
        )
        self.assertNotIn("PluralNoTooltip')|l]s", record["candidate_text"])
        self.assertEqual(record["token_integrity_status"], "ok")

    def test_valid_portuguese_tutor_literal_remains_the_single_exception(
        self,
    ) -> None:
        original = (
            "[task_contract_target.Custom('ES_ElLa')] "
            "[Select_CString(task_contract_target.IsFemale, "
            "'vossa tutora e guardiã', 'vosso tutor e guardião')]"
        )
        record = dry_run.build_record(
            row(
                original,
                segment_id=12229,
                source_key="ep3_contract_event.0011.desc_ward_accepted",
            )
        )

        self.assertEqual(record["lane"], dry_run.BLOCKED_LANE)
        self.assertIn("no_allowlisted_repair", record["blockers"])
        self.assertIn("post_validation_issue", record["blockers"])
        self.assertEqual(record["candidate_text"], original)

    def test_contextual_guardian_term_avoids_second_tutor_false_positive(
        self,
    ) -> None:
        original = (
            "[TaskContract.GetEmployer.Custom('ES_ElLa')] "
            "na jornada até a corte de seu tutor."
        )
        record = dry_run.build_record(
            row(
                original,
                segment_id=12008,
                source_key="laamp_transport_ward_desc",
            )
        )

        self.assertEqual(record["lane"], dry_run.ELIGIBLE_LANE)
        self.assertIn("corte de seu guardião", record["candidate_text"])
        self.assertEqual(record["post_issue_codes"], [])

    def test_summary_never_claims_operational_writes(self) -> None:
        eligible = dry_run.build_record(
            row(
                "[TaskContract.GetEmployer.Custom('ES_ElLa')] "
                "livre de um… #EMP personaje problemático#!.",
                segment_id=12693,
                source_key="laamp_rid_councillor_contract_desc",
            )
        )
        blocked = dry_run.build_record(
            row(
                "[task_contract_target.Custom('ES_ElLa')] "
                "[Select_CString(task_contract_target.IsFemale, "
                "'vossa tutora e guardiã', 'vosso tutor e guardião')]",
                segment_id=12229,
                source_key="ep3_contract_event.0011.desc_ward_accepted",
            )
        )

        summary = dry_run.summarize(
            score_run_id=402,
            segment_state_run_id=777,
            records=[eligible, blocked],
        )

        self.assertEqual(summary["proposal_ready_count"], 1)
        self.assertEqual(summary["blocked_count"], 1)
        self.assertEqual(summary["ready_for_apply_count"], 0)
        self.assertEqual(summary["apply_count"], 0)
        self.assertFalse(summary["output_changed"])
        self.assertFalse(summary["operational_writes"])


if __name__ == "__main__":
    unittest.main()
