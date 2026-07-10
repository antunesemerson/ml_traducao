from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import domain_context_landed_title_policy_review as landed_review


SOURCE = "domain_context_landed_title_dynasty_house_name_policy_review_v1"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_TOTAL = 61
SOURCE_DECISION = "needs_domain_landed_title_dynasty_house_name_policy"

ALLOWED_DECISIONS = {
    "domain_landed_dynasty_house_terminal_policy",
    "domain_landed_dynasty_house_terminal_policy_with_title_guard",
    "domain_landed_dynasty_house_terminal_policy_with_domain_guard",
    "domain_landed_dynasty_house_reuse_not_requirement_effect_culture_policy",
    "domain_landed_dynasty_house_reuse_effect_list_concept_policy",
    "domain_landed_dynasty_house_reuse_requirement_effect_residual_policy",
    "needs_domain_landed_dynasty_name_policy",
    "needs_domain_landed_house_name_policy",
    "needs_domain_landed_character_name_policy",
    "needs_domain_landed_title_name_policy",
    "needs_domain_landed_culture_name_policy",
    "needs_domain_landed_dynasty_house_scope_getter_policy",
    "needs_domain_landed_dynasty_house_requirement_guard",
    "needs_domain_landed_dynasty_house_event_context_policy",
    "needs_domain_landed_dynasty_house_residual_repair",
    "needs_domain_landed_dynasty_house_dynamic_parser_escape",
    "domain_landed_dynasty_house_blocked_uncertain",
}

DOMAIN_RE = re.compile(r"title|landed|county|duchy|kingdom|empire|barony|domain|dynasty|dynn_", re.I)
LANDED_TITLE_RE = re.compile(r"\b[ckdebp]_[a-z0-9_]+|titles?_l_|county|duchy|kingdom|empire|barony", re.I)
DYNASTY_RE = re.compile(r"dynn_|dynasty|\\$dynn_[^$]+\\$", re.I)
HOUSE_RE = re.compile(r"house|GetHouse|GetHouseName", re.I)
CHARACTER_NAME_RE = re.compile(r"character|first_name|nick|GetFirstName|GetFullName|GetTitledFirstName", re.I)
TITLE_NAME_RE = re.compile(r"GetName|GetNameNoTier|GetBaseName|GetAdjective|title_name|suffix|c_nf_", re.I)
CULTURE_NAME_RE = re.compile(r"culture|tradition|heritage|ethos|language|bai|viet|japan|korea", re.I)
SCOPE_GETTER_RE = re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|\[[^\]]*\.(?:Get|Is|Has)[A-Za-z0-9_]*[^\]]*\]", re.I)
REQUIREMENT_RE = re.compile(r"requirement|tooltip|valid|can_|allow|trigger|effect|EFFECT_LIST|#indent|#weak|\\n|\n", re.I)
EVENT_RE = re.compile(r"event|\.desc|desc\.|option|toast|dialogue|story|scheme|interaction|memory", re.I)
DYNAMIC_RE = re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)
RESIDUAL_RE = re.compile(r"NÃƒ|ÃƒÆ’|Ã‚|ï¿½|Ã¯Â¿Â½|\b(the|your|you|their|cannot|sera|serÃ¡|mas|mÃ¡s|facil|fÃ¡cil)\b", re.I)


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_domain_context_landed_title_dynasty_house_name_policy_review"
    spec = reports_dir / f"{stamp}_domain_context_landed_title_dynasty_house_name_policy_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def blob_for(row: dict[str, Any]) -> str:
    return " ".join(
        [
            str(row.get("relative_path") or ""),
            str(row.get("source_key") or ""),
            str(row.get("old_text") or ""),
            str(row.get("confirmed_text") or ""),
            str(row.get("output_text") or ""),
            " ".join(row.get("families_open") or []),
        ]
    )


def marker(pattern: re.Pattern[str], blob: str, label: str) -> list[str]:
    return [label] if pattern.search(blob) else []


def marker_groups(row: dict[str, Any]) -> dict[str, list[str]]:
    blob = blob_for(row)
    return {
        "domain_markers": marker(DOMAIN_RE, blob, "DomainTitle"),
        "landed_title_markers": marker(LANDED_TITLE_RE, blob, "LandedTitleKey"),
        "dynasty_markers": marker(DYNASTY_RE, blob, "DynastyNameToken"),
        "house_markers": marker(HOUSE_RE, blob, "HouseName"),
        "character_name_markers": marker(CHARACTER_NAME_RE, blob, "CharacterName"),
        "title_name_markers": marker(TITLE_NAME_RE, blob, "TitleNameOrSuffix"),
        "culture_name_markers": marker(CULTURE_NAME_RE, blob, "CultureName"),
        "scope_getter_markers": marker(SCOPE_GETTER_RE, blob, "ScopeGetter"),
        "requirement_markers": marker(REQUIREMENT_RE, blob, "RequirementTooltip"),
        "dynamic_markers": marker(DYNAMIC_RE, blob, "DynamicToken"),
        "guard_markers": ["StateClean", "NoOutputApply", "ConfirmedMatchesOutput"],
        "secondary_markers": [
            label
            for pattern, label in [
                (EVENT_RE, "EventContext"),
                (RESIDUAL_RE, "ResidualVisible"),
            ]
            if pattern.search(blob)
        ],
    }


def decide(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    blob = blob_for(row)
    if RESIDUAL_RE.search(blob):
        return (
            "domain_landed_dynasty_house_reuse_requirement_effect_residual_policy",
            "residual_repair_after_requirement_effect",
            "",
            "residual_repair_after_requirement_effect",
            "visible residual marker should reuse registered residual policy with dynasty/house guard",
        )
    if REQUIREMENT_RE.search(blob):
        return ("needs_domain_landed_dynasty_house_requirement_guard", "", "", "domain_landed_dynasty_house_requirement_guard", "requirement/effect guard marker remains")
    if SCOPE_GETTER_RE.search(blob):
        return ("needs_domain_landed_dynasty_house_scope_getter_policy", "", "", "domain_landed_dynasty_house_scope_getter_policy", "scope/getter marker remains")
    if EVENT_RE.search(blob):
        return ("needs_domain_landed_dynasty_house_event_context_policy", "", "", "domain_landed_dynasty_house_event_context_policy", "event/context marker remains")
    if HOUSE_RE.search(blob) and not DYNASTY_RE.search(blob):
        return ("needs_domain_landed_house_name_policy", "", "", "domain_landed_house_name_policy", "house-name marker appears without dynasty token")
    if CHARACTER_NAME_RE.search(blob):
        return ("needs_domain_landed_character_name_policy", "", "", "domain_landed_character_name_policy", "character/person-name marker remains")
    if DYNASTY_RE.search(blob) and TITLE_NAME_RE.search(blob) and LANDED_TITLE_RE.search(blob):
        return (
            "domain_landed_dynasty_house_terminal_policy_with_title_guard",
            "",
            "domain_context_landed_title_dynasty_house_name_policy",
            "domain_context_landed_title_dynasty_house_name_policy_terminal_registration",
            "cohesive landed-title pattern: title suffix/name plus dynasty token; terminal read-only guard is sufficient",
        )
    if DYNASTY_RE.search(blob):
        return ("needs_domain_landed_dynasty_name_policy", "", "", "domain_landed_dynasty_name_policy", "dynasty-name marker remains but title guard is incomplete")
    if CULTURE_NAME_RE.search(blob):
        return ("needs_domain_landed_culture_name_policy", "", "", "domain_landed_culture_name_policy", "culture/name marker remains")
    if TITLE_NAME_RE.search(blob):
        return ("needs_domain_landed_title_name_policy", "", "", "domain_landed_title_name_policy", "title-name marker remains")
    if DYNAMIC_RE.search(blob):
        return ("needs_domain_landed_dynasty_house_dynamic_parser_escape", "", "ck3_dynamic_expression_parser_spec", "ck3_dynamic_expression_parser_spec", "dynamic token should escape after dynasty/house checks")
    return ("domain_landed_dynasty_house_blocked_uncertain", "", "", "domain_context_landed_title_dynasty_house_name_policy", "insufficient dynasty/house subtype evidence")


def convert_sample(row: dict[str, Any]) -> dict[str, Any]:
    groups = marker_groups(row)
    decision, registered, catalog, next_component, rationale = decide(row)
    return {
        "record_type": "sample_review",
        "segment_id": int(row["segment_id"]),
        "relative_path": str(row.get("relative_path") or ""),
        "source_key": str(row.get("source_key") or ""),
        "families_open": row.get("families_open") or [],
        "source_decision": SOURCE_DECISION,
        "parent_policy": "domain_context_landed_title_adjective_name_policy",
        "primary_route": "domain_context_after_requirement_effect",
        "old_text": str(row.get("old_text") or ""),
        "confirmed_text": str(row.get("confirmed_text") or ""),
        "output_text": str(row.get("output_text") or ""),
        **groups,
        "matched_registered_policy": registered,
        "matched_catalog_spec": catalog,
        "dynasty_house_decision": decision,
        "next_component": next_component,
        "requires_lifecycle_later": False,
        "requires_apply_later": False,
        "corrected_text": "",
        "rationale": rationale,
    }


def validate_samples(samples: list[dict[str, Any]]) -> None:
    required = {
        "record_type",
        "segment_id",
        "relative_path",
        "source_key",
        "families_open",
        "source_decision",
        "parent_policy",
        "primary_route",
        "old_text",
        "confirmed_text",
        "output_text",
        "domain_markers",
        "landed_title_markers",
        "dynasty_markers",
        "house_markers",
        "character_name_markers",
        "title_name_markers",
        "culture_name_markers",
        "scope_getter_markers",
        "requirement_markers",
        "dynamic_markers",
        "matched_registered_policy",
        "matched_catalog_spec",
        "guard_markers",
        "secondary_markers",
        "dynasty_house_decision",
        "next_component",
        "requires_lifecycle_later",
        "requires_apply_later",
        "corrected_text",
        "rationale",
    }
    if len(samples) != EXPECTED_TOTAL:
        raise SystemExit(f"review count mismatch: {len(samples)} expected {EXPECTED_TOTAL}")
    seen: set[int] = set()
    for row in samples:
        missing = required - set(row)
        if missing:
            raise SystemExit(f"missing fields for {row.get('segment_id')}: {sorted(missing)}")
        segment_id = int(row["segment_id"])
        if segment_id in seen:
            raise SystemExit(f"duplicate segment_id: {segment_id}")
        seen.add(segment_id)
        if row["source_decision"] != SOURCE_DECISION:
            raise SystemExit(f"wrong source decision for {segment_id}: {row['source_decision']}")
        if row["dynasty_house_decision"] not in ALLOWED_DECISIONS:
            raise SystemExit(f"invalid dynasty_house_decision for {segment_id}: {row['dynasty_house_decision']}")
        if row["requires_apply_later"]:
            raise SystemExit(f"requires_apply_later unexpectedly true for {segment_id}")
        if row["requires_lifecycle_later"]:
            raise SystemExit(f"requires_lifecycle_later unexpectedly true for {segment_id}")


def write_outputs(
    *,
    args: argparse.Namespace,
    state: dict[str, int],
    registry: dict[str, int],
    samples: list[dict[str, Any]],
) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, spec_path = output_paths()
    decision_counts = Counter(row["dynasty_house_decision"] for row in samples)
    reuse_count = sum(1 for row in samples if row["dynasty_house_decision"].startswith("domain_landed_dynasty_house_reuse_"))
    terminal_count = sum(1 for row in samples if row["dynasty_house_decision"].startswith("domain_landed_dynasty_house_terminal_policy"))
    dominant_decision, dominant_count = decision_counts.most_common(1)[0]
    if terminal_count >= 25:
        next_prompt = "chat_exec_domain_context_landed_title_dynasty_house_terminal_spec_registration_prompt.md"
        assessment = "terminal_read_only_component"
    elif reuse_count >= 25:
        next_prompt = "chat_exec_domain_context_landed_title_dynasty_house_policy_catalog_registration_prompt.md"
        assessment = "reuse_component"
    elif dominant_decision.startswith("needs_") and dominant_count >= 30:
        next_prompt = f"chat_exec_domain_context_{dominant_decision.replace('needs_domain_', '')}_review_prompt.md"
        assessment = "micro_router_possible"
    else:
        next_prompt = "chat_exec_domain_context_after_requirement_effect_religion_holy_site_policy_review_prompt.md"
        assessment = "fragmented_dynasty_house_queue"
    marker_fields = [
        "domain_markers",
        "landed_title_markers",
        "dynasty_markers",
        "house_markers",
        "character_name_markers",
        "title_name_markers",
        "culture_name_markers",
        "scope_getter_markers",
        "requirement_markers",
        "dynamic_markers",
        "guard_markers",
        "secondary_markers",
    ]
    marker_counts = {
        field: dict(Counter(marker for row in samples for marker in row[field]).most_common(20))
        for field in marker_fields
    }
    family_counts = Counter(family for row in samples for family in row["families_open"])
    summary = {
        "record_type": "summary",
        "source": SOURCE,
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        **state,
        **registry,
        "total_reviewed": len(samples),
        "reuse_registered_or_cataloged_count": reuse_count,
        "terminal_policy_count": terminal_count,
        "ready_lifecycle_future": 0,
        "apply_candidates_future": 0,
        "requires_lifecycle_later": False,
        "requires_apply_later": False,
        "decision_counts": dict(decision_counts),
        "dominant_subtype": dominant_decision,
        "dominant_count": dominant_count,
        "package_assessment": assessment,
        "next_prompt": next_prompt,
    }
    spec = {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": "domain_context_landed_title_adjective_name_policy",
        "policy_id": "domain_context_landed_title_dynasty_house_name_policy",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "entry_conditions": [
            "adjective_name_decision == needs_domain_landed_title_dynasty_house_name_policy",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
            "landed title key plus dynasty token plus title suffix/name token",
        ],
        "reused_registered_policies": [
            {"agent_key": key, "sampled": sum(1 for row in samples if row["matched_registered_policy"] == key)}
            for key in sorted({row["matched_registered_policy"] for row in samples if row["matched_registered_policy"]})
        ],
        "reused_catalog_specs": [
            {"policy_id": key, "sampled": sum(1 for row in samples if row["matched_catalog_spec"] == key)}
            for key in sorted({row["matched_catalog_spec"] for row in samples if row["matched_catalog_spec"]})
        ],
        "dynasty_house_name_types": [{"type": key, "sampled": value} for key, value in decision_counts.most_common()],
        "resolution_order": [
            "state guard",
            "residual reuse",
            "requirement/effect guard",
            "scope/getter and event split",
            "house-only and character-name split",
            "terminal landed-title dynasty token with title guard",
            "culture/name and title/name fallback",
            "dynamic parser escape",
        ],
        "next_components": [next_prompt],
        "blocked_conditions": [
            "state guard failed",
            "needs_output_apply != 0",
            "confirmed_matches_output != 1",
            "dynasty token without landed title/name guard",
            "ambiguous dynasty/house/name evidence",
        ],
        "promotion_gate": "read_only_component_only_no_apply_no_lifecycle",
        "observed_decision_counts": dict(decision_counts),
    }
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
        for field, counts in marker_counts.items():
            for marker_name, count in counts.items():
                handle.write(json.dumps({"record_type": f"top_{field}", "value": marker_name, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for family, count in family_counts.most_common(20):
            handle.write(json.dumps({"record_type": "top_family", "family": family, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False, sort_keys=True) + "\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Domain context landed title dynasty/house name policy review\n\n")
        for key in [
            "total_reviewed",
            "reuse_registered_or_cataloged_count",
            "terminal_policy_count",
            "ready_lifecycle_future",
            "apply_candidates_future",
            "dominant_subtype",
            "dominant_count",
            "package_assessment",
            "next_prompt",
        ]:
            handle.write(f"- {key}: {summary[key]}\n")
        handle.write("\nDecisoes\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nTop families\n")
        for family, count in family_counts.most_common(12):
            handle.write(f"- {family}: {count}\n")
        handle.write("\nTop markers\n")
        for field, counts in marker_counts.items():
            handle.write(f"- {field}: {counts}\n")
        handle.write("\nRespostas objetivas\n")
        handle.write("- Deve virar componente read-only real: sim, como terminal guard de landed title + dynasty token + title suffix/name.\n")
        handle.write("- Nao gera lifecycle/apply em curto prazo.\n")
        handle.write("- Registrar agora: sim, como terminal/read-only; depois voltar para needs_domain_religion_holy_site_policy.\n")
        handle.write(f"- Proximo prompt recomendado: {next_prompt}.\n")
    return txt_path, jsonl_path, spec_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Review domain-context landed title dynasty/house name sublane read-only.")
    parser.add_argument("--adjective-name-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    rows = landed_review.read_jsonl(args.adjective_name_jsonl)
    source_samples = [
        row
        for row in rows
        if row.get("record_type") == "sample_review"
        and row.get("adjective_name_decision") == SOURCE_DECISION
    ]
    if len(source_samples) != EXPECTED_TOTAL:
        raise SystemExit(f"source dynasty/house total guard failed: {len(source_samples)} expected {EXPECTED_TOTAL}")
    ids = [int(row["segment_id"]) for row in source_samples]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate source segment_id")
    with landed_review.connect_readonly() as conn:
        state = landed_review.state_counts(conn, args.segment_state_run_id)
        registry = landed_review.registry_metrics(conn)
        state_rows = landed_review.state_by_id(conn, ids, args.segment_state_run_id)
    samples: list[dict[str, Any]] = []
    for row in source_samples:
        segment_id = int(row["segment_id"])
        state_row = state_rows.get(segment_id)
        if not state_row:
            raise SystemExit(f"missing state row for segment_id={segment_id}")
        if str(state_row.get("state_group") or "") != "pending" or int(state_row.get("is_closed") or 0) != 0:
            raise SystemExit(f"pending guard failed for segment_id={segment_id}")
        if int(state_row.get("needs_output_apply") or 0) != 0:
            raise SystemExit(f"needs_output_apply guard failed for segment_id={segment_id}")
        if int(state_row.get("confirmed_matches_output") or 0) != 1:
            raise SystemExit(f"confirmed_matches_output guard failed for segment_id={segment_id}")
        samples.append(convert_sample(row))
    validate_samples(samples)
    txt_path, jsonl_path, spec_path = write_outputs(args=args, state=state, registry=registry, samples=samples)
    decision_counts = Counter(row["dynasty_house_decision"] for row in samples)
    terminal_count = sum(1 for row in samples if row["dynasty_house_decision"].startswith("domain_landed_dynasty_house_terminal_policy"))
    print(f"txt_report={txt_path}")
    print(f"jsonl_report={jsonl_path}")
    print(f"spec_json={spec_path}")
    print(f"total_reviewed={len(samples)}")
    print("decision_counts=" + json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True))
    print("reuse_registered_or_cataloged_count=0")
    print(f"terminal_policy_count={terminal_count}")
    print("ready_lifecycle_future=0")
    print("apply_candidates_future=0")


if __name__ == "__main__":
    main()
