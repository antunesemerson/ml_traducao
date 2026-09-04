from __future__ import annotations

import hashlib
import json
from typing import Any


PAIRWISE_ELISION_EVIDENCE_TYPE = "deterministic_redundant_select_cstring_repair"
PAIRWISE_ELISION_SOURCE = "quality_redundant_select_cstring_shadow_v1"
PAIRWISE_ELISION_CONFIRMATION_SOURCE = "pairwise_monotonic_repair"
PAIRWISE_ELISION_CONFIRMATION_LABEL = (
    "pairwise_deterministic_redundant_select_cstring_repair_v1"
)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def evidence_authorizes_redundant_select_elision(
    row: dict[str, Any],
    baseline_text: str,
    candidate_text: str,
    *,
    require_active_promotion: bool = True,
) -> bool:
    """Authorize only the exact evidence-backed removal of redundant wrappers."""

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
    if metadata.get("source") != PAIRWISE_ELISION_SOURCE:
        return False
    if metadata.get("lane") != "pairwise_evidence_eligible":
        return False
    if metadata.get("token_integrity_mode") != "intentional_exact_select_elision":
        return False
    if metadata.get("blockers"):
        return False

    # Imported lazily because the shadow scorer imports the scoring stack.
    from local_quality_validator import collapse_redundant_select_cstring
    from quality_redundant_select_cstring_shadow import (
        intentional_elision_token_integrity,
    )

    regenerated, repairs = collapse_redundant_select_cstring(baseline_text)
    if not repairs or regenerated != candidate_text:
        return False
    return intentional_elision_token_integrity(
        baseline_text,
        candidate_text,
        repairs,
    )
