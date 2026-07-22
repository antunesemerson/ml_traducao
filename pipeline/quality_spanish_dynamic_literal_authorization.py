from __future__ import annotations

import hashlib
import json
from typing import Any


PAIRWISE_ELISION_EVIDENCE_TYPE = "deterministic_spanish_dynamic_literal_repair"
PAIRWISE_ELISION_SOURCE = "quality_spanish_dynamic_literal_shadow_v3"
PAIRWISE_ELISION_CONFIRMATION_SOURCE = "pairwise_monotonic_repair"
PAIRWISE_ELISION_CONFIRMATION_LABEL = "pairwise_deterministic_spanish_dynamic_literal_repair_v1"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def evidence_authorizes_intentional_elision(
    row: dict[str, Any],
    baseline_text: str,
    candidate_text: str,
    *,
    require_active_promotion: bool = True,
) -> bool:
    """Validate the exact evidence-backed removal; never act as a generic override."""

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
    if not all(
        int(row.get(field) or 0) == 1
        for field in required_fields
    ):
        return False
    if row.get("pairwise_baseline_hash") != sha256_text(baseline_text):
        return False
    if row.get("pairwise_candidate_hash") != sha256_text(candidate_text):
        return False
    try:
        metadata = json.loads(str(row.get("pairwise_source_metadata_json") or "{}"))
    except json.JSONDecodeError:
        return False
    if metadata.get("source") != PAIRWISE_ELISION_SOURCE:
        return False
    if metadata.get("source_token_status") != "intentional_elision":
        return False

    # Imported lazily because the shadow scorer itself imports ml_score_segments.
    from quality_spanish_dynamic_literal_shadow import (
        protected_tokens_after_intentional_elision,
        repair_dynamic_literals,
    )

    regenerated, repairs = repair_dynamic_literals(baseline_text)
    if regenerated != candidate_text:
        return False
    return protected_tokens_after_intentional_elision(
        baseline_text,
        candidate_text,
        repairs,
    )
