from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import local_quality_validator


POLICY_PATH = (
    Path(__file__).resolve().with_name("calibration_policies")
    / "exact_shared_glossary.json"
)


@lru_cache(maxsize=1)
def load_policy() -> dict[str, Any]:
    payload = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    if int(payload.get("schema_version") or 0) != 1:
        raise ValueError("Unsupported exact shared Glossary calibration schema.")
    if payload.get("pattern") != "exact_shared_glossary_token":
        raise ValueError("Unexpected exact shared Glossary calibration pattern.")
    probability = float(payload.get("calibrated_safe_probability") or 0.0)
    if not 0.0 < probability < 1.0:
        raise ValueError("Glossary calibration probability must be between 0 and 1.")
    return payload


def matches_operational_scope(row: dict[str, Any]) -> bool:
    policy = load_policy()
    if not bool(policy.get("enabled")):
        return False
    if not str(row.get("relative_path") or "").startswith(
        str(policy.get("path_prefix") or "")
    ):
        return False
    candidate_text = row.get("candidate_text")
    output_text = row.get("output_text")
    if candidate_text is None or output_text is None:
        return False
    if str(candidate_text) != str(output_text):
        return False
    return local_quality_validator.is_exact_shared_glossary_token(row)


def calibrate_safe_probability(
    row: dict[str, Any],
    raw_safe_probability: float,
) -> dict[str, Any]:
    raw_probability = min(max(float(raw_safe_probability), 0.0), 1.0)
    policy = load_policy()
    calibrated_probability = float(policy["calibrated_safe_probability"])
    if not matches_operational_scope(row) or raw_probability >= calibrated_probability:
        return {
            "applied": False,
            "raw_safe_probability": raw_probability,
            "calibrated_safe_probability": raw_probability,
        }
    return {
        "applied": True,
        "policy_id": str(policy["policy_id"]),
        "pattern": str(policy["pattern"]),
        "source_score_run_id": int(policy["source_score_run_id"]),
        "source_snapshot_run_id": int(policy["source_snapshot_run_id"]),
        "source_shadow_run_id": int(policy["source_shadow_run_id"]),
        "source_shadow_rule_version": str(policy["source_shadow_rule_version"]),
        "raw_safe_probability": raw_probability,
        "calibrated_safe_probability": calibrated_probability,
    }
