from __future__ import annotations

import copy
import unittest

import quality_contract_es_helper_pairwise_evidence as evidence
import quality_contract_es_helper_repair_dry_run as dry_run


def exact_row() -> dict:
    text = (
        "[TaskContract.GetEmployer.Custom('ES_ElLa')] "
        "na jornada até a corte de seu tutor."
    )
    return {
        "run_id": 402,
        "segment_state_run_id": 777,
        "segment_id": 12008,
        "relative_path": "contracts/task_contracts_l_spanish.yml",
        "source_key": "laamp_transport_ward_desc",
        "english_text": "English source",
        "candidate_text": text,
        "current_output_text": text,
        "model_safe_probability": 0.02,
        "human_locked": 0,
        "is_closed": 1,
        "needs_output_apply": 0,
    }


class ContractEsHelperPairwiseEvidenceTest(unittest.TestCase):
    def test_regenerates_exact_reviewed_candidate(self) -> None:
        row = exact_row()
        shadow = dry_run.build_record(row)

        regenerated = evidence.verify_shadow_candidate(row, shadow)

        self.assertEqual(regenerated["lane"], dry_run.ELIGIBLE_LANE)
        self.assertIn("corte de seu guardião", regenerated["candidate_text"])
        self.assertEqual(regenerated["post_issue_codes"], [])
        self.assertTrue(regenerated["token_integrity_ok"])

    def test_rejects_stale_shadow_hash(self) -> None:
        row = exact_row()
        shadow = copy.deepcopy(dry_run.build_record(row))
        shadow["original_hash"] = "stale"

        with self.assertRaisesRegex(RuntimeError, "baseline is stale"):
            evidence.verify_shadow_candidate(row, shadow)

    def test_rejects_output_drift_before_evidence(self) -> None:
        row = exact_row()
        shadow = dry_run.build_record(row)
        row["current_output_text"] = "Output changed after the shadow."

        with self.assertRaisesRegex(RuntimeError, "Current output changed"):
            evidence.verify_shadow_candidate(row, shadow)

    def test_summary_declares_no_operational_writes_or_report_dependency(self) -> None:
        summary = evidence.summarize(
            source_path="db:ml_quality_shadow_runs/206",
            shadow_run_id=206,
            evidence=[{"segment_id": 12008}],
            apply=True,
            run_id=88,
            inserted=1,
            reused=0,
        )

        self.assertEqual(summary["evidence_count"], 1)
        self.assertEqual(summary["confirmation_write_count"], 0)
        self.assertEqual(summary["segment_state_write_count"], 0)
        self.assertEqual(summary["output_write_count"], 0)
        self.assertFalse(summary["reports_required"])
        self.assertEqual(summary["artifacts"], {})


if __name__ == "__main__":
    unittest.main()
