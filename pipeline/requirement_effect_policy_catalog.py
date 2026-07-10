from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


READONLY_PROMOTION_GATE = "read_only_component_only_no_apply_no_lifecycle"

SPEC_RELATIVE_PATHS = [
    "reports/20260621_182252_832397_scope_getter_requirement_policy_spec.json",
    "reports/20260621_182856_093345_get_trait_scope_requirement_policy_spec.json",
    "reports/20260621_183516_533002_get_trait_accolade_requirement_policy_spec.json",
    "reports/20260621_184149_690696_accolade_knight_attribute_policy_spec.json",
    "reports/20260621_184729_570490_knight_attribute_unlock_requirement_policy_spec.json",
    "reports/20260621_190000_627489_unlock_acclaimed_knight_entity_policy_spec.json",
    "reports/20260621_190448_336581_acclaimed_knight_entity_requirement_policy_spec.json",
    "reports/20260621_190820_254167_acclaimed_knight_entity_unlock_final_policy_spec.json",
    "reports/20260621_191352_367785_gender_local_player_requirement_policy_spec.json",
    "reports/20260621_194238_213136_select_cstring_requirement_policy_spec.json",
    "reports/20260621_201000_372249_select_cstring_player_target_direct_policy_spec.json",
    "reports/20260621_202657_815802_select_cstring_possessive_policy_spec.json",
    "reports/20260621_205240_696871_select_cstring_es_helper_policy_spec.json",
    "reports/20260621_212845_060519_local_player_requirement_policy_spec.json",
    "reports/20260621_214949_651334_es_oa_requirement_policy_spec.json",
    "reports/20260621_220608_287532_script_value_requirement_policy_spec.json",
    "reports/20260621_221617_085028_concept_requirement_policy_spec.json",
    "reports/20260621_233548_100358_name_nickname_requirement_guard_spec.json",
    "reports/20260622_004245_181542_effect_list_multiline_policy_spec.json",
    "reports/20260622_014609_044703_effect_list_artifact_activity_policy_spec.json",
    "reports/20260622_130721_601727_effect_list_gender_local_player_policy_spec.json",
    "reports/20260622_133901_719476_effect_list_trait_accolade_policy_spec.json",
    "reports/20260622_141149_802441_effect_list_script_value_policy_spec.json",
    "reports/20260622_144059_266106_effect_list_concept_policy_spec.json",
    "reports/20260622_020638_524121_artifact_item_effect_policy_spec.json",
    "reports/20260622_023205_601243_artifact_item_scope_getter_policy_spec.json",
    "reports/20260622_113242_032221_artifact_activity_gender_local_player_policy_spec.json",
    "reports/20260622_123357_154242_artifact_activity_script_value_policy_spec.json",
]

TERMINAL_POLICY_IDS = {
    "effect_list_gender_local_player_policy",
    "effect_list_trait_accolade_policy",
    "effect_list_script_value_policy",
    "effect_list_concept_policy",
    "artifact_activity_gender_local_player_policy",
    "artifact_activity_script_value_policy",
}

SPLITTER_POLICY_IDS = {
    "effect_list_multiline_policy",
    "effect_list_artifact_activity_policy",
    "artifact_item_effect_policy",
    "artifact_item_scope_getter_policy",
}


@dataclass(frozen=True)
class PolicyRecord:
    path: str
    policy_id: str
    parent_policy: str
    created_for: str
    terminal_policy: bool
    catalog_role: str
    fallback_stage: str
    validation_issue: str
    raw: dict[str, Any]


def _is_terminal(spec: dict[str, Any]) -> bool:
    policy_id = str(spec.get("policy_id") or "")
    if policy_id in SPLITTER_POLICY_IDS:
        return False
    if policy_id in TERMINAL_POLICY_IDS:
        return True
    gate = str(spec.get("promotion_gate") or "")
    if READONLY_PROMOTION_GATE in gate:
        return True
    if spec.get("terminal_policy") is True:
        return True
    return str(spec.get("created_for") or "") == "terminal_read_only_policy_spec"


def _catalog_role(spec: dict[str, Any], terminal_policy: bool) -> str:
    policy_id = str(spec.get("policy_id") or "")
    if policy_id in TERMINAL_POLICY_IDS:
        return "terminal_reuse" if spec.get("policy_shape") == "terminal_reuse_route" else "terminal_guard"
    if policy_id in SPLITTER_POLICY_IDS:
        return "splitter"
    if terminal_policy:
        return "terminal_guard"
    return "splitter"


def _validate(spec: dict[str, Any], expected_run_id: int) -> str:
    issues: list[str] = []
    if spec.get("schema_version") != 1:
        issues.append("schema_version")
    if not spec.get("policy_id"):
        issues.append("policy_id")
    if not spec.get("parent_policy"):
        issues.append("parent_policy")
    if not spec.get("created_for"):
        issues.append("created_for")
    if spec.get("segment_state_run_id") != expected_run_id:
        issues.append("segment_state_run_id")
    return ",".join(issues)


def load_policy_catalog(project_root: Path, expected_run_id: int) -> dict[str, Any]:
    records: list[PolicyRecord] = []
    inventory: list[dict[str, Any]] = []
    by_id: dict[str, PolicyRecord] = {}
    children_by_parent: dict[str, list[PolicyRecord]] = {}

    for relative_path in SPEC_RELATIVE_PATHS:
        path = project_root / relative_path
        if not path.exists():
            inventory.append(
                {
                    "record_type": "policy_inventory",
                    "path": relative_path,
                    "policy_id": "",
                    "parent_policy": "",
                    "created_for": "",
                    "terminal_policy": False,
                    "loaded": False,
                    "missing": True,
                    "validation_issue": "missing_spec",
                }
            )
            continue
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            inventory.append(
                {
                    "record_type": "policy_inventory",
                    "path": relative_path,
                    "policy_id": "",
                    "parent_policy": "",
                    "created_for": "",
                    "terminal_policy": False,
                    "loaded": False,
                    "missing": False,
                    "validation_issue": f"json_error:{exc}",
                }
            )
            continue

        validation_issue = _validate(spec, expected_run_id)
        record = PolicyRecord(
            path=relative_path,
            policy_id=str(spec.get("policy_id") or ""),
            parent_policy=str(spec.get("parent_policy") or ""),
            created_for=str(spec.get("created_for") or ""),
            terminal_policy=_is_terminal(spec),
            catalog_role="",
            fallback_stage=str(spec.get("fallback_stage") or ""),
            validation_issue=validation_issue,
            raw=spec,
        )
        record = PolicyRecord(
            path=record.path,
            policy_id=record.policy_id,
            parent_policy=record.parent_policy,
            created_for=record.created_for,
            terminal_policy=record.terminal_policy,
            catalog_role=_catalog_role(spec, record.terminal_policy),
            fallback_stage=record.fallback_stage,
            validation_issue=record.validation_issue,
            raw=record.raw,
        )
        records.append(record)
        if record.policy_id:
            by_id[record.policy_id] = record
        children_by_parent.setdefault(record.parent_policy, []).append(record)
        inventory.append(
            {
                "record_type": "policy_inventory",
                "path": relative_path,
                "policy_id": record.policy_id,
                "parent_policy": record.parent_policy,
                "created_for": record.created_for,
                "terminal_policy": record.terminal_policy,
                "catalog_role": record.catalog_role,
                "loaded": validation_issue == "",
                "missing": False,
                "validation_issue": validation_issue,
            }
        )

    return {
        "records": records,
        "inventory": inventory,
        "by_id": by_id,
        "children_by_parent": children_by_parent,
    }


def policy_chain(policy_id: str, by_id: dict[str, PolicyRecord]) -> list[str]:
    chain: list[str] = []
    current = by_id.get(policy_id)
    seen: set[str] = set()
    while current and current.policy_id not in seen:
        chain.append(current.policy_id)
        seen.add(current.policy_id)
        current = by_id.get(current.parent_policy)
    return chain


def match_route(route: str, catalog: dict[str, Any]) -> dict[str, Any]:
    by_id: dict[str, PolicyRecord] = catalog["by_id"]
    children_by_parent: dict[str, list[PolicyRecord]] = catalog["children_by_parent"]
    if route in by_id:
        record = by_id[route]
        return {
            "matched_policy_id": record.policy_id,
            "matched_parent_policy": record.parent_policy,
            "policy_chain": policy_chain(record.policy_id, by_id),
            "terminal_policy": record.terminal_policy,
            "fallback_stage": record.fallback_stage,
            "notes": "route matched policy_id",
        }
    children = children_by_parent.get(route, [])
    if children:
        terminal_children = [child for child in children if child.terminal_policy]
        return {
            "matched_policy_id": route,
            "matched_parent_policy": "",
            "policy_chain": [route] + [child.policy_id for child in children],
            "terminal_policy": len(terminal_children) == len(children),
            "fallback_stage": "parser_backed_dynamic_expression",
            "notes": f"route has {len(children)} child policies ({len(terminal_children)} terminal)",
        }
    return {
        "matched_policy_id": "",
        "matched_parent_policy": "",
        "policy_chain": [],
        "terminal_policy": False,
        "fallback_stage": "",
        "notes": "no policy catalog match",
    }
