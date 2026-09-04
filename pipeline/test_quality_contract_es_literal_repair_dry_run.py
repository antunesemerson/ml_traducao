from __future__ import annotations

import unittest

import quality_contract_es_literal_repair_dry_run as dry_run


def row(text: str) -> dict:
    return {
        "run_id": 402,
        "segment_state_run_id": 777,
        "segment_id": 123,
        "relative_path": "contracts/task_contracts_l_spanish.yml",
        "source_key": "test.key",
        "english_text": "English source",
        "candidate_text": text,
        "current_output_text": text,
        "model_safe_probability": 0.02,
        "human_locked": 0,
        "is_closed": 1,
        "needs_output_apply": 0,
    }


class ContractEsLiteralRepairDryRunTest(unittest.TestCase):
    def test_replaces_only_allowlisted_select_cstring_literals(self) -> None:
        original = (
            "[actor.Custom('ES_ElLa')] "
            "[Select_CString(actor.IsFemale, 'la anciana', 'el anciano')]"
        )

        candidate, repairs = dry_run.replace_allowlisted_literals(original)

        self.assertEqual(
            candidate,
            (
                "[actor.Custom('ES_ElLa')] "
                "[Select_CString(actor.IsFemale, 'a anciã', 'o ancião')]"
            ),
        )
        self.assertEqual(len(repairs), 2)
        self.assertIn("[actor.Custom('ES_ElLa')]", candidate)
        self.assertIn("actor.IsFemale", candidate)

    def test_preserves_source_capitalization(self) -> None:
        original = (
            "[Select_CString(peasant.IsFemale, "
            "'La alborotadora', 'El alborotador')]"
        )

        candidate, _ = dry_run.replace_allowlisted_literals(original)

        self.assertIn("'A agitadora'", candidate)
        self.assertIn("'O agitador'", candidate)

    def test_build_record_is_proposal_ready_only_after_clean_validation(self) -> None:
        original = (
            "[actor.Custom('ES_ElLa')] "
            "[Select_CString(actor.IsFemale, 'la anciana', 'el anciano')]"
        )

        record = dry_run.build_record(row(original))

        self.assertEqual(record["lane"], dry_run.ELIGIBLE_LANE)
        self.assertEqual(record["blockers"], [])
        self.assertEqual(record["pre_issue_codes"], [dry_run.ISSUE_CODE])
        self.assertEqual(record["post_issue_codes"], [])
        self.assertTrue(record["token_integrity_ok"])
        self.assertFalse(record["ready_for_apply"])
        self.assertFalse(record["output_changed"])
        self.assertFalse(record["operational_writes"])

    def test_valid_portuguese_tutor_false_positive_stays_blocked(self) -> None:
        original = (
            "[actor.Custom('ES_ElLa')] "
            "[Select_CString(actor.IsFemale, "
            "'vossa tutora e guardiã', 'vosso tutor e guardião')]"
        )

        record = dry_run.build_record(row(original))

        self.assertEqual(record["lane"], dry_run.BLOCKED_LANE)
        self.assertIn("no_allowlisted_literal_replacement", record["blockers"])
        self.assertIn("post_validation_issue", record["blockers"])
        self.assertEqual(record["candidate_text"], original)

    def test_summary_never_claims_operational_writes(self) -> None:
        eligible = dry_run.build_record(
            row(
                "[actor.Custom('ES_ElLa')] "
                "[Select_CString(actor.IsFemale, 'una extranjera', 'un extranjero')]"
            )
        )
        blocked = dry_run.build_record(
            row(
                "[actor.Custom('ES_ElLa')] "
                "[Select_CString(actor.IsFemale, "
                "'vossa tutora e guardiã', 'vosso tutor e guardião')]"
            )
        )
        blocked["segment_id"] = 124

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
