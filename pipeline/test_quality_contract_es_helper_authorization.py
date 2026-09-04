from __future__ import annotations

import json
import unittest

from apply_segment_state_updates import (
    pairwise_intentional_elision_authorized,
    sha256_text,
)
from quality_contract_es_helper_authorization import (
    PAIRWISE_ELISION_CONFIRMATION_LABEL,
    PAIRWISE_ELISION_CONFIRMATION_SOURCE,
    PAIRWISE_ELISION_EVIDENCE_TYPE,
    evidence_authorizes_contract_es_helper_token_delta,
)
from quality_contract_es_helper_repair_dry_run import build_record


def source_row() -> dict:
    baseline = (
        "\"Bem-vindo, viajante[ROOT.Char.Custom('ES_OA')]!\", "
        "[local_1.Custom('ES_ElLa')]"
    )
    return {
        "run_id": 402,
        "segment_state_run_id": 777,
        "segment_id": 12157,
        "relative_path": "contracts/task_contracts_l_spanish.yml",
        "source_key": "ep3_contract_event.0006.desc_locals_temple",
        "english_text": "English source",
        "candidate_text": baseline,
        "current_output_text": baseline,
        "model_safe_probability": 0.02,
        "human_locked": 0,
        "is_closed": 1,
        "needs_output_apply": 0,
    }


def evidence_row(baseline: str, candidate: str, metadata: dict) -> dict:
    return {
        "pairwise_evidence_id": 1777,
        "confirmation_source": PAIRWISE_ELISION_CONFIRMATION_SOURCE,
        "confirmation_label": PAIRWISE_ELISION_CONFIRMATION_LABEL,
        "pairwise_evidence_type": PAIRWISE_ELISION_EVIDENCE_TYPE,
        "pairwise_baseline_hash": sha256_text(baseline),
        "pairwise_candidate_hash": sha256_text(candidate),
        "pairwise_token_integrity_ok": 1,
        "pairwise_post_validation_clean": 1,
        "pairwise_training_eligible": 1,
        "pairwise_promotion_eligible": 1,
        "pairwise_source_metadata_json": json.dumps(metadata),
    }


class ContractEsHelperAuthorizationTest(unittest.TestCase):
    def test_authorizes_only_exact_regenerated_reviewed_token_delta(self) -> None:
        source = source_row()
        metadata = build_record(source)
        baseline = metadata["original_text"]
        candidate = metadata["candidate_text"]
        row = evidence_row(baseline, candidate, metadata)

        self.assertTrue(
            evidence_authorizes_contract_es_helper_token_delta(
                row,
                baseline,
                candidate,
            )
        )
        self.assertTrue(
            pairwise_intentional_elision_authorized(
                row,
                baseline,
                candidate,
            )
        )

    def test_rejects_unrelated_token_loss_and_stale_evidence(self) -> None:
        source = source_row()
        metadata = build_record(source)
        baseline = metadata["original_text"]
        candidate = metadata["candidate_text"]
        row = evidence_row(baseline, candidate, metadata)

        self.assertFalse(
            evidence_authorizes_contract_es_helper_token_delta(
                row,
                baseline,
                candidate.replace("[local_1.Custom('ES_ElLa')]", ""),
            )
        )
        stale = dict(row)
        stale["pairwise_promotion_eligible"] = 0
        self.assertFalse(
            evidence_authorizes_contract_es_helper_token_delta(
                stale,
                baseline,
                candidate,
            )
        )

    def test_rejects_segment_outside_three_item_allowlist(self) -> None:
        source = source_row()
        metadata = build_record(source)
        metadata["segment_id"] = 999999
        baseline = metadata["original_text"]
        candidate = metadata["candidate_text"]
        row = evidence_row(baseline, candidate, metadata)

        self.assertFalse(
            evidence_authorizes_contract_es_helper_token_delta(
                row,
                baseline,
                candidate,
            )
        )


if __name__ == "__main__":
    unittest.main()
