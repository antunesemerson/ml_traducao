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


SOURCE = "domain_context_landed_title_adjective_name_policy_review_v1"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_TOTAL = 61
SOURCE_DECISION = "needs_domain_landed_title_adjective_name_policy"

ALLOWED_DECISIONS = {
    "domain_landed_adjective_name_terminal_policy",
    "domain_landed_adjective_name_terminal_policy_with_domain_guard",
    "domain_landed_adjective_name_terminal_policy_with_requirement_guard",
    "domain_landed_adjective_name_reuse_effect_list_concept_policy",
    "domain_landed_adjective_name_reuse_not_requirement_effect_culture_policy",
    "domain_landed_adjective_name_reuse_requirement_effect_residual_policy",
    "needs_domain_landed_adjective_localization_policy",
    "needs_domain_landed_title_name_localization_policy",
    "needs_domain_landed_adjective_requirement_tooltip_policy",
    "needs_domain_landed_title_dynasty_house_name_policy",
    "needs_domain_landed_adjective_scope_getter_policy",
    "needs_domain_landed_adjective_rank_de_jure_policy",
    "needs_domain_landed_adjective_location_policy",
    "needs_domain_landed_adjective_government_realm_policy",
    "needs_domain_landed_adjective_culture_name_policy",
    "needs_domain_landed_adjective_event_context_policy",
    "needs_domain_landed_adjective_script_value_policy",
    "needs_domain_landed_adjective_residual_repair",
    "needs_domain_landed_adjective_dynamic_parser_escape",
    "domain_landed_adjective_name_blocked_uncertain",
}

DOMAIN_RE = re.compile(r"title|landed|county|duchy|kingdom|empire|barony|domain|dynasty|dynn_", re.I)
LANDED_TITLE_RE = re.compile(r"\b[ckdebp]_[a-z0-9_]+|titles?_l_|county|duchy|kingdom|empire|barony", re.I)
ADJECTIVE_RE = re.compile(r"adjective|GetAdjective|_adj\b|suffix|c_nf_.*_suffix", re.I)
TITLE_NAME_RE = re.compile(r"GetName|GetNameNoTier|GetBaseName|title_name|_name\b|name_|c_nf_", re.I)
DYNASTY_HOUSE_RE = re.compile(r"dynn_|dynasty|house|GetDynasty|GetHouse|\\$dynn_[^$]+\\$", re.I)
RANK_DE_JURE_RE = re.compile(r"de_jure|de jure|de_facto|de facto|rightful|drift|county|duchy|kingdom|empire|barony|rank|tier|^[ckdebp]_", re.I)
SCOPE_GETTER_RE = re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|\[[^\]]*\.(?:Get|Is|Has)[A-Za-z0-9_]*[^\]]*\]", re.I)
REQUIREMENT_RE = re.compile(r"requirement|tooltip|valid|can_|allow|trigger|effect|EFFECT_LIST|#indent|#weak|\\n|\n", re.I)
LOCATION_RE = re.compile(r"location|capital|holding|province|barony|Phong Chau|Thuong Oai|Naju|Jeonju|Hanyang", re.I)
GOVERNMENT_RE = re.compile(r"government|realm|crown|authority|succession|law|vassal|liege", re.I)
CULTURE_RE = re.compile(r"culture|tradition|heritage|ethos|language|bai|viet|japan|korea", re.I)
EVENT_RE = re.compile(r"event|\.desc|desc\.|option|toast|dialogue|story|scheme|interaction|memory", re.I)
SCRIPT_VALUE_RE = re.compile(r"ScriptValue|GetScriptValue|script_value|\|V[0-9]?|#P\s*[0-9]|\b[0-9]+\s*%", re.I)
DYNAMIC_RE = re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)
RESIDUAL_RE = re.compile(r"NÃƒ|ÃƒÆ’|Ã‚|ï¿½|Ã¯Â¿Â½|\b(the|your|you|their|cannot|sera|serÃ¡|mas|mÃ¡s|facil|fÃ¡cil)\b", re.I)


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_domain_context_landed_title_adjective_name_policy_review"
    spec = reports_dir / f"{stamp}_domain_context_landed_title_adjective_name_policy_spec.json"
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
        "adjective_markers": marker(ADJECTIVE_RE, blob, "TitleSuffixAdjective"),
        "title_name_markers": marker(TITLE_NAME_RE, blob, "TitleName"),
        "dynasty_house_markers": marker(DYNASTY_HOUSE_RE, blob, "DynastyHouseName"),
        "rank_de_jure_markers": marker(RANK_DE_JURE_RE, blob, "RankOrDeJure"),
        "scope_getter_markers": marker(SCOPE_GETTER_RE, blob, "ScopeGetter"),
        "requirement_markers": marker(REQUIREMENT_RE, blob, "RequirementTooltip"),
        "location_markers": marker(LOCATION_RE, blob, "LocationName"),
        "dynamic_markers": marker(DYNAMIC_RE, blob, "DynamicToken"),
        "guard_markers": ["StateClean", "NoOutputApply", "ConfirmedMatchesOutput"],
        "secondary_markers": [
            label
            for pattern, label in [
                (GOVERNMENT_RE, "GovernmentRealm"),
                (CULTURE_RE, "CultureName"),
                (EVENT_RE, "EventContext"),
                (SCRIPT_VALUE_RE, "ScriptValue"),
                (RESIDUAL_RE, "ResidualVisible"),
            ]
            if pattern.search(blob)
        ],
    }


def decide(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    blob = blob_for(row)
    if RESIDUAL_RE.search(blob):
        return (
            "domain_landed_adjective_name_reuse_requirement_effect_residual_policy",
            "residual_repair_after_requirement_effect",
            "",
            "residual_repair_after_requirement_effect",
            "visible residual marker should reuse registered residual policy with adjective/name guard",
        )
    if REQUIREMENT_RE.search(blob):
        return ("needs_domain_landed_adjective_requirement_tooltip_policy", "", "", "domain_landed_adjective_requirement_tooltip_policy", "requirement/effect tooltip marker remains")
    if SCOPE_GETTER_RE.search(blob):
        return ("needs_domain_landed_adjective_scope_getter_policy", "", "", "domain_landed_adjective_scope_getter_policy", "scope/getter marker remains")
    if SCRIPT_VALUE_RE.search(blob):
        return ("needs_domain_landed_adjective_script_value_policy", "", "", "domain_landed_adjective_script_value_policy", "ScriptValue/numeric marker remains")
    if GOVERNMENT_RE.search(blob):
        return ("needs_domain_landed_adjective_government_realm_policy", "", "", "domain_landed_adjective_government_realm_policy", "government/realm marker remains")
    if EVENT_RE.search(blob):
        return ("needs_domain_landed_adjective_event_context_policy", "", "", "domain_landed_adjective_event_context_policy", "event/context marker remains")
    if DYNASTY_HOUSE_RE.search(blob):
        return (
            "needs_domain_landed_title_dynasty_house_name_policy",
            "",
            "",
            "domain_landed_title_dynasty_house_name_policy",
            "landed title label is composed from title suffix plus dynasty/house token",
        )
    if LOCATION_RE.search(blob):
        return ("needs_domain_landed_adjective_location_policy", "", "", "domain_landed_adjective_location_policy", "location/locality marker remains")
    if re.search(r"de_jure|de jure|de_facto|de facto|rightful|drift", blob, re.I):
        return ("needs_domain_landed_adjective_rank_de_jure_policy", "", "", "domain_landed_adjective_rank_de_jure_policy", "rank/de jure marker remains")
    if CULTURE_RE.search(blob):
        return ("needs_domain_landed_adjective_culture_name_policy", "", "", "domain_landed_adjective_culture_name_policy", "culture/name marker remains")
    if TITLE_NAME_RE.search(blob):
        return ("needs_domain_landed_title_name_localization_policy", "", "", "domain_landed_title_name_localization_policy", "title name localization marker remains")
    if ADJECTIVE_RE.search(blob):
        return ("needs_domain_landed_adjective_localization_policy", "", "", "domain_landed_adjective_localization_policy", "title adjective localization marker remains")
    if DYNAMIC_RE.search(blob):
        return ("needs_domain_landed_adjective_dynamic_parser_escape", "", "ck3_dynamic_expression_parser_spec", "ck3_dynamic_expression_parser_spec", "dynamic token should escape after adjective/name checks")
    if LANDED_TITLE_RE.search(blob):
        return (
            "domain_landed_adjective_name_terminal_policy_with_domain_guard",
            "",
            "domain_context_landed_title_adjective_name_policy",
            "domain_context_landed_title_adjective_name_policy",
            "adjective/name pattern appears terminal/read-only with landed-title domain guard",
        )
    return ("domain_landed_adjective_name_blocked_uncertain", "", "", "domain_context_landed_title_adjective_name_policy", "insufficient adjective/name subtype evidence")


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
        "parent_policy": "domain_context_landed_title_policy",
        "primary_route": "domain_context_after_requirement_effect",
        "old_text": str(row.get("old_text") or ""),
        "confirmed_text": str(row.get("confirmed_text") or ""),
        "output_text": str(row.get("output_text") or ""),
        **groups,
        "matched_registered_policy": registered,
        "matched_catalog_spec": catalog,
        "adjective_name_decision": decision,
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
        "adjective_markers",
        "title_name_markers",
        "dynasty_house_markers",
        "rank_de_jure_markers",
        "scope_getter_markers",
        "requirement_markers",
        "location_markers",
        "dynamic_markers",
        "matched_registered_policy",
        "matched_catalog_spec",
        "guard_markers",
        "secondary_markers",
        "adjective_name_decision",
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
        if row["adjective_name_decision"] not in ALLOWED_DECISIONS:
            raise SystemExit(f"invalid adjective_name_decision for {segment_id}: {row['adjective_name_decision']}")
        if row["requires_apply_later"]:
            raise SystemExit(f"requires_apply_later unexpectedly true for {segment_id}")
        if row["requires_lifecycle_later"]:
            raise SystemExit(f"requires_lifecycle_later unexpectedly true for {segment_id}")


def next_prompt_for(decision_counts: Counter[str], reuse_count: int, terminal_count: int) -> str:
    needs_counts = Counter({key: value for key, value in decision_counts.items() if key.startswith("needs_")})
    concentrated_need = next(((key, count) for key, count in needs_counts.most_common() if count >= 18), None)
    if concentrated_need:
        mapping = {
            "needs_domain_landed_title_dynasty_house_name_policy": "chat_exec_domain_context_landed_title_dynasty_house_name_policy_review_prompt.md",
            "needs_domain_landed_adjective_location_policy": "chat_exec_domain_context_landed_adjective_location_policy_review_prompt.md",
            "needs_domain_landed_title_name_localization_policy": "chat_exec_domain_context_landed_title_name_localization_policy_review_prompt.md",
            "needs_domain_landed_adjective_localization_policy": "chat_exec_domain_context_landed_adjective_localization_policy_review_prompt.md",
        }
        return mapping.get(concentrated_need[0], f"chat_exec_domain_context_{concentrated_need[0].replace('needs_domain_', '')}_review_prompt.md")
    if reuse_count >= 25:
        return "chat_exec_domain_context_landed_title_adjective_name_policy_catalog_registration_prompt.md"
    if terminal_count >= 25:
        return "chat_exec_domain_context_landed_title_adjective_name_terminal_spec_registration_prompt.md"
    return "chat_exec_domain_context_after_requirement_effect_religion_holy_site_policy_review_prompt.md"


def write_outputs(
    *,
    args: argparse.Namespace,
    state: dict[str, int],
    registry: dict[str, int],
    samples: list[dict[str, Any]],
) -> tuple[Path, Path, Path]:
    txt_path, jsonl_path, spec_path = output_paths()
    decision_counts = Counter(row["adjective_name_decision"] for row in samples)
    reuse_count = sum(1 for row in samples if row["adjective_name_decision"].startswith("domain_landed_adjective_name_reuse_"))
    terminal_count = sum(1 for row in samples if row["adjective_name_decision"].startswith("domain_landed_adjective_name_terminal_policy"))
    dominant_decision, dominant_count = decision_counts.most_common(1)[0]
    next_prompt = next_prompt_for(decision_counts, reuse_count, terminal_count)
    marker_fields = [
        "domain_markers",
        "landed_title_markers",
        "adjective_markers",
        "title_name_markers",
        "dynasty_house_markers",
        "rank_de_jure_markers",
        "scope_getter_markers",
        "requirement_markers",
        "location_markers",
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
        "package_assessment": "micro_router_needed" if dominant_decision.startswith("needs_") and dominant_count >= 18 else "fragmented_adjective_name_queue",
        "next_prompt": next_prompt,
    }
    spec = {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": "domain_context_landed_title_policy",
        "policy_id": "domain_context_landed_title_adjective_name_policy",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "entry_conditions": [
            "landed_title_decision == needs_domain_landed_title_adjective_name_policy",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
        ],
        "reused_registered_policies": [
            {"agent_key": key, "sampled": sum(1 for row in samples if row["matched_registered_policy"] == key)}
            for key in sorted({row["matched_registered_policy"] for row in samples if row["matched_registered_policy"]})
        ],
        "reused_catalog_specs": [
            {"policy_id": key, "sampled": sum(1 for row in samples if row["matched_catalog_spec"] == key)}
            for key in sorted({row["matched_catalog_spec"] for row in samples if row["matched_catalog_spec"]})
        ],
        "adjective_name_types": [{"type": key, "sampled": value} for key, value in decision_counts.most_common()],
        "resolution_order": [
            "state guard",
            "residual reuse",
            "requirement/effect guard",
            "scope/getter and ScriptValue split",
            "government/event split",
            "dynasty/house/name token split",
            "location, rank/de jure, culture/name split",
            "title name/adjective localization split",
            "dynamic parser escape",
            "terminal adjective/name guard",
        ],
        "next_components": [next_prompt],
        "blocked_conditions": [
            "state guard failed",
            "needs_output_apply != 0",
            "confirmed_matches_output != 1",
            "ambiguous adjective/name evidence",
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
        handle.write("Domain context landed title adjective/name policy review\n\n")
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
        handle.write("- Deve virar componente read-only real: ainda nao; ha sublane estreita dominante de dynasty/house name.\n")
        handle.write("- Nao gera lifecycle/apply em curto prazo.\n")
        handle.write("- Registrar agora: nao; aguardar o review estreito de dynasty/house antes de religion/holy-site.\n")
        handle.write(f"- Proximo prompt recomendado: {next_prompt}.\n")
    return txt_path, jsonl_path, spec_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Review domain-context landed title adjective/name sublane read-only.")
    parser.add_argument("--landed-title-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    rows = landed_review.read_jsonl(args.landed_title_jsonl)
    source_samples = [
        row
        for row in rows
        if row.get("record_type") == "sample_review"
        and row.get("landed_title_decision") == SOURCE_DECISION
    ]
    if len(source_samples) != EXPECTED_TOTAL:
        raise SystemExit(f"source adjective/name total guard failed: {len(source_samples)} expected {EXPECTED_TOTAL}")
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
    decision_counts = Counter(row["adjective_name_decision"] for row in samples)
    print(f"txt_report={txt_path}")
    print(f"jsonl_report={jsonl_path}")
    print(f"spec_json={spec_path}")
    print(f"total_reviewed={len(samples)}")
    print("decision_counts=" + json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True))
    print("reuse_registered_or_cataloged_count=0")
    print("terminal_policy_count=0")
    print("ready_lifecycle_future=0")
    print("apply_candidates_future=0")


if __name__ == "__main__":
    main()
