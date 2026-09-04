from __future__ import annotations

import hashlib
import json
from typing import Any


PAIRWISE_ELISION_EVIDENCE_TYPE = "reviewed_contract_es_helper_repair"
PAIRWISE_ELISION_SOURCE = "quality_contract_es_helper_repair_dry_run_v1"
PAIRWISE_ELISION_CONFIRMATION_SOURCE = "pairwise_monotonic_repair"
PAIRWISE_ELISION_CONFIRMATION_LABEL = (
    "pairwise_reviewed_contract_es_helper_repair_v1"
)
ALLOWLISTED_TOKEN_DELTA_SEGMENTS = {12157, 12232, 12241}


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def evidence_authorizes_contract_es_helper_token_delta(
    row: dict[str, Any],
    baseline_text: str,
    candidate_text: str,
    *,
    require_active_promotion: bool = True,
) -> bool:
    """Authorize only the three reviewed, exactly regenerated ES helper removals."""

    if row.get("pairwise_evidence_id") is None:
        return False
    if row.get("pairwise_evidence_type") != PAIRWISE_ELISION_EVIDENCE_TYPE:
        return False
    required_fields = [
        "pairwise_token_integrity_ok",
        "pairwise_post_validation_clean",
    ]
    if require_active_promotion:
        required_fields.extend(
            [
                "pairwise_training_eligible",
                "pairwise_promotion_eligible",
            ]
        )
    if not all(int(row.get(field) or 0) == 1 for field in required_fields):
        return False
    if row.get("pairwise_baseline_hash") != sha256_text(baseline_text):
        return False
    if row.get("pairwise_candidate_hash") != sha256_text(candidate_text):
        return False
    try:
        metadata = json.loads(str(row.get("pairwise_source_metadata_json") or "{}"))
    except json.JSONDecodeError:
        return False
    segment_id = int(metadata.get("segment_id") or 0)
    if segment_id not in ALLOWLISTED_TOKEN_DELTA_SEGMENTS:
        return False
    if metadata.get("source") != PAIRWISE_ELISION_SOURCE:
        return False
    if metadata.get("lane") != "proposal_ready":
        return False
    if metadata.get("blockers"):
        return False
    if metadata.get("token_integrity_status") != "allowlisted_token_delta":
        return False
    if not bool(metadata.get("token_integrity_ok")):
        return False
    if metadata.get("original_text") != baseline_text:
        return False
    if metadata.get("candidate_text") != candidate_text:
        return False
    repair_actions = {
        str(repair.get("action") or "")
        for repair in metadata.get("repairs") or []
    }
    if not repair_actions or not repair_actions.issubset(
        {
            "remove_redundant_gender_token_after_neutral_word",
            "remove_redundant_gender_token_and_space",
        }
    ):
        return False

    # Imported lazily because the deterministic dry-run loads the scoring stack.
    from quality_contract_es_helper_repair_dry_run import build_record

    regenerated = build_record(
        {
            "run_id": int(metadata.get("score_run_id") or 0),
            "segment_state_run_id": int(
                metadata.get("segment_state_run_id") or 0
            ),
            "segment_id": segment_id,
            "relative_path": metadata.get("relative_path"),
            "source_key": metadata.get("source_key"),
            "english_text": metadata.get("english_text"),
            "candidate_text": baseline_text,
            "current_output_text": baseline_text,
            "model_safe_probability": float(
                metadata.get("raw_current_score") or 0.0
            ),
            "human_locked": 0,
            "is_closed": 1,
            "needs_output_apply": 0,
        }
    )
    return (
        regenerated.get("lane") == "proposal_ready"
        and not regenerated.get("blockers")
        and regenerated.get("token_integrity_status")
        == "allowlisted_token_delta"
        and regenerated.get("candidate_text") == candidate_text
        and regenerated.get("original_hash") == sha256_text(baseline_text)
        and regenerated.get("candidate_hash") == sha256_text(candidate_text)
    )
