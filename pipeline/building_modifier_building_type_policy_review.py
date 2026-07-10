from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE_DECISION = "needs_building_modifier_building_type_policy"
PRIMARY_ROUTE = "building_modifier_effect_policy"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_REGISTERED_AGENTS = 219
EXPECTED_REVIEW_TOTAL = 68

REGISTERED_REUSE_POLICIES = {
    "building_modifier_effect_policy",
    "effect_list_concept_policy",
    "effect_list_script_value_policy",
}

BUILDING_MARKERS = [
    ("Building", re.compile(r"building|buildings?|GetBuilding|building_type|building_slot|construct", re.I)),
    ("BuildingType", re.compile(r"building_type|GetBuilding\(|building_[a-z0-9_]+", re.I)),
]

HOLDING_MARKERS = [
    ("Holding", re.compile(r"holding|barony|castle|city|temple|tribal|county", re.I)),
    ("DuchyCounty", re.compile(r"duchy|county|province|domain|barony", re.I)),
]

SPECIAL_BUILDING_MARKERS = [
    ("SpecialBuilding", re.compile(r"special_building|duchy_building|holy_site|grand_temple|cathedral|shrine", re.I)),
    ("HolySite", re.compile(r"holy_site|holy site|temple|church|cathedral|shrine", re.I)),
]

MODIFIER_MARKERS = [
    ("Modifier", re.compile(r"modifier|building_modifier|county_modifier|holding_modifier", re.I)),
    ("RewardEffect", re.compile(r"unlock|desbloqueia|gain|loss|opinion|levy|tax|development", re.I)),
]

DOMAIN_MARKERS = [
    ("CultureReligion", re.compile(r"culture|religion|faith|doctrine|holy_site|church|temple", re.I)),
    ("TitleLaw", re.compile(r"title|law|succession|government|realm|county|duchy|kingdom|empire", re.I)),
    ("Domain", re.compile(r"domain|realm|county|duchy|barony|province|liege|vassal", re.I)),
]

GUARD_MARKERS = [
    ("ScopeGetterGuard", re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|Get[A-Za-z0-9_]+\(", re.I)),
    ("DomainGuard", re.compile(r"culture|faith|religion|realm|title|law|government|county|duchy|church|temple", re.I)),
    ("EventGuard", re.compile(r"event|events|\.desc|desc_|option|story|activity|interaction", re.I)),
    ("BuildingTypeGuard", re.compile(r"building|holding|barony|county|duchy|temple|church|castle|city", re.I)),
]

SECONDARY_MARKERS = [
    ("ScriptValue", re.compile(r"ScriptValue|GetScriptValue|GetValue|\|V[0-9]?|\|=\+?0|[0-9]+\s*%", re.I)),
    ("Concept", re.compile(r"\[[A-Za-z0-9_]+\||Concept\(|game_concept", re.I)),
    ("ArtifactActivity", re.compile(r"artifact|activity|tournament|hunt|feast|travel|legend", re.I)),
    ("TitleLaw", re.compile(r"title|county|duchy|kingdom|empire|law|succession|government", re.I)),
    ("CultureReligion", re.compile(r"culture|religion|faith|doctrine|holy_site|church|temple", re.I)),
    ("ScopeGetter", re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|Get[A-Za-z0-9_]+\(", re.I)),
    ("Event", re.compile(r"event|events|\.desc|desc_|option|story|activity|interaction", re.I)),
    ("Domain", re.compile(r"realm|domain|county|duchy|barony|province|liege|vassal", re.I)),
    ("ResidualVisible", re.compile(r"ÃƒÆ’|Ãƒâ€š|Ã‚Â¿|Ã‚Â¡|Ã¢â‚¬|ï¿½|\bthe\b|\byour\b|\byou\b|\btheir\b|\bcannot\b", re.I)),
    ("DynamicToken", re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)),
]


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_building_modifier_building_type_policy_review"
    spec = reports_dir / f"{stamp}_building_modifier_building_type_policy_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    probe = conn.execute("PRAGMA query_only").fetchone()
    if int(probe[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def source_rows(path: Path) -> list[dict[str, Any]]:
    rows = [
        row
        for row in read_jsonl(path)
        if row.get("record_type") == "sample_review"
        and row.get("building_modifier_decision") == SOURCE_DECISION
    ]
    rows.sort(key=lambda row: (str(row.get("relative_path") or ""), str(row.get("source_key") or ""), int(row["segment_id"])))
    ids = [int(row["segment_id"]) for row in rows]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate source segment_id")
    if len(rows) != EXPECTED_REVIEW_TOTAL:
        raise SystemExit(f"source row guard failed: {len(rows)} expected {EXPECTED_REVIEW_TOTAL}")
    return rows


def fetch_states(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, state_group, is_closed, needs_output_apply, confirmed_matches_output
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def registered_policies(conn: sqlite3.Connection) -> set[str]:
    registry_count = int(conn.execute("SELECT COUNT(*) AS c FROM ml_agent_registry").fetchone()["c"] or 0)
    if registry_count != EXPECTED_REGISTERED_AGENTS:
        raise SystemExit(f"registry guard failed: {registry_count} expected {EXPECTED_REGISTERED_AGENTS}")
    placeholders = ",".join("?" for _ in REGISTERED_REUSE_POLICIES)
    rows = conn.execute(
        f"""
        SELECT agent_key
        FROM ml_agent_registry
        WHERE agent_key IN ({placeholders})
          AND status = 'active'
        """,
        tuple(sorted(REGISTERED_REUSE_POLICIES)),
    ).fetchall()
    return {str(row["agent_key"]) for row in rows}


def detect(patterns: list[tuple[str, re.Pattern[str]]], blob: str) -> list[str]:
    return [name for name, pattern in patterns if pattern.search(blob)]


def classify(
    *,
    state: dict[str, Any] | None,
    registered: set[str],
    building_markers: list[str],
    holding_markers: list[str],
    special_building_markers: list[str],
    modifier_markers: list[str],
    domain_markers: list[str],
    guard_markers: list[str],
    secondary_markers: list[str],
) -> tuple[str, str, str, str, str]:
    building = set(building_markers)
    holding = set(holding_markers)
    special = set(special_building_markers)
    modifier = set(modifier_markers)
    domain = set(domain_markers)
    guards = set(guard_markers)
    secondary = set(secondary_markers)
    if not state or state["state_group"] != "pending" or int(state["is_closed"] or 0) != 0:
        return "building_type_blocked_uncertain", "", "", "state_guard", "segment is not pending in selected state run"
    if int(state["needs_output_apply"] or 0) != 0 or int(state["confirmed_matches_output"] or 0) != 1:
        return "building_type_blocked_uncertain", "", "", "state_guard", "state guard failed: needs_output_apply or confirmed_matches_output"
    if "ResidualVisible" in secondary:
        return "needs_building_type_residual_repair", "", "", "residual_dependency_filtered_repair", "visible residual remains"

    if "ScriptValue" in secondary and "effect_list_script_value_policy" in registered:
        return "building_type_reuse_effect_list_script_value_policy", "effect_list_script_value_policy", "effect_list_script_value_policy", "effect_list_script_value_policy", "can reuse ScriptValue policy with building type guard"
    if "Concept" in secondary and "effect_list_concept_policy" in registered and not (holding or special):
        return "building_type_reuse_effect_list_concept_policy", "effect_list_concept_policy", "effect_list_concept_policy", "effect_list_concept_policy", "can reuse concept policy with building type guard"
    if "building_modifier_effect_policy" in registered and modifier and not (holding or special):
        return "building_type_reuse_building_modifier_effect_policy", "building_modifier_effect_policy", "building_modifier_effect_policy", "building_modifier_effect_policy", "can reuse parent building/modifier splitter"

    if special and "HolySite" in special:
        return "needs_building_type_holy_site_policy", "", "", "holy_site_effect_name_policy", "holy-site special building dependency remains"
    if special:
        return "needs_building_type_special_building_policy", "", "", "special_building_policy", "special/duchy building dependency remains"
    if "DuchyCounty" in holding:
        return "needs_building_type_duchy_or_county_policy", "", "", "duchy_county_building_policy", "duchy/county building dependency remains"
    if holding:
        return "needs_building_type_holding_policy", "", "", "holding_building_policy", "holding/castle/city/temple dependency remains"
    if "ArtifactActivity" in secondary:
        return "needs_building_type_activity_or_artifact_policy", "", "", "artifact_activity_effect_policy", "activity/artifact context remains"
    if "CultureReligion" in secondary or "CultureReligion" in domain:
        return "needs_building_type_culture_religion_policy", "", "", "culture_religion_policy", "culture/religion dependency remains"
    if "TitleLaw" in secondary or "TitleLaw" in domain:
        return "needs_building_type_title_law_policy", "", "", "title_law_policy", "title/law dependency remains"
    if "ScopeGetter" in secondary or "ScopeGetterGuard" in guards:
        return "needs_building_type_scope_getter_policy", "", "", "scope_getter_requirement_policy", "scope/getter dependency remains"
    if "Event" in secondary or "EventGuard" in guards:
        return "needs_building_type_event_context", "", "", "event_context_after_requirement_effect", "event context remains"
    if "Domain" in secondary or "DomainGuard" in guards:
        return "needs_building_type_domain_context", "", "", "domain_context_after_requirement_effect", "domain context remains"
    if "DynamicToken" in secondary:
        return "needs_building_type_dynamic_parser_escape", "", "", "ck3_dynamic_symbolic_parser", "dynamic parser escape remains"
    if domain:
        return "building_type_terminal_policy_with_domain_guard", "", "", "terminal_router_policy", "terminal building type with domain guard"
    if modifier:
        return "building_type_terminal_policy_with_effect_guard", "", "", "terminal_router_policy", "terminal building type with modifier/effect guard"
    if building:
        return "building_type_terminal_policy", "", "", "terminal_router_policy", "plain building type terminal pattern"
    return "building_type_blocked_uncertain", "", "", "human_review_or_evidence_collection", "insufficient building type evidence"


def build_spec(
    *,
    decision_counts: Counter[str],
    reused_policies: Counter[str],
    reused_specs: Counter[str],
    total_reviewed: int,
    next_components: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": "building_modifier_effect_policy",
        "policy_id": "building_modifier_building_type_policy",
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "entry_conditions": [
            "source building_modifier_decision == needs_building_modifier_building_type_policy",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
        ],
        "reused_registered_policies": [{"agent_key": key, "sampled": count} for key, count in reused_policies.most_common()],
        "reused_catalog_specs": [{"policy_id": key, "sampled": count} for key, count in reused_specs.most_common()],
        "building_type_types": [{"decision": key, "sampled": count} for key, count in decision_counts.most_common()],
        "resolution_order": [
            "state guard",
            "residual guard",
            "ScriptValue and concept reuse",
            "special/holy-site building",
            "duchy/county/holding building",
            "culture/title/scope/event/domain fallback",
        ],
        "next_components": next_components,
        "blocked_conditions": [
            "state guard failed",
            "visible residual",
            "missing building type evidence",
        ],
        "promotion_gate": "read_only_component_only_no_apply_no_lifecycle",
        "sampled": total_reviewed,
        "policy_shape": "splitter_candidate",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only building/modifier building-type policy review.")
    parser.add_argument("--building-modifier-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")

    rows = source_rows(args.building_modifier_jsonl)
    segment_ids = [int(row["segment_id"]) for row in rows]
    with connect_readonly() as conn:
        states = fetch_states(conn, args.segment_state_run_id, segment_ids)
        registered = registered_policies(conn)

    results: list[dict[str, Any]] = []
    decision_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    building_counts: Counter[str] = Counter()
    holding_counts: Counter[str] = Counter()
    special_counts: Counter[str] = Counter()
    modifier_counts: Counter[str] = Counter()
    domain_counts: Counter[str] = Counter()
    guard_counts: Counter[str] = Counter()
    secondary_counts: Counter[str] = Counter()
    reused_policy_counts: Counter[str] = Counter()
    reused_spec_counts: Counter[str] = Counter()

    for row in rows:
        segment_id = int(row["segment_id"])
        blob = " ".join(
            str(row.get(key) or "")
            for key in ("relative_path", "source_key", "old_text", "confirmed_text", "output_text")
        )
        blob += " " + " ".join(row.get("families_open") or [])
        building_markers = detect(BUILDING_MARKERS, blob)
        holding_markers = detect(HOLDING_MARKERS, blob)
        special_building_markers = detect(SPECIAL_BUILDING_MARKERS, blob)
        modifier_markers = detect(MODIFIER_MARKERS, blob)
        domain_markers = detect(DOMAIN_MARKERS, blob)
        guard_markers = detect(GUARD_MARKERS, blob)
        secondary_markers = detect(SECONDARY_MARKERS, blob)
        decision, matched_registered_policy, matched_catalog_spec, next_component, rationale = classify(
            state=states.get(segment_id),
            registered=registered,
            building_markers=building_markers,
            holding_markers=holding_markers,
            special_building_markers=special_building_markers,
            modifier_markers=modifier_markers,
            domain_markers=domain_markers,
            guard_markers=guard_markers,
            secondary_markers=secondary_markers,
        )
        family_counts.update(row.get("families_open") or [])
        building_counts.update(building_markers)
        holding_counts.update(holding_markers)
        special_counts.update(special_building_markers)
        modifier_counts.update(modifier_markers)
        domain_counts.update(domain_markers)
        guard_counts.update(guard_markers)
        secondary_counts.update(secondary_markers)
        decision_counts[decision] += 1
        if matched_registered_policy:
            reused_policy_counts[matched_registered_policy] += 1
        if matched_catalog_spec:
            reused_spec_counts[matched_catalog_spec] += 1
        results.append(
            {
                "record_type": "sample_review",
                "segment_id": segment_id,
                "relative_path": row.get("relative_path") or "",
                "source_key": row.get("source_key") or "",
                "families_open": row.get("families_open") or [],
                "source_decision": SOURCE_DECISION,
                "primary_route": PRIMARY_ROUTE,
                "old_text": row.get("old_text") or "",
                "confirmed_text": row.get("confirmed_text") or "",
                "output_text": row.get("output_text") or "",
                "building_markers": building_markers,
                "holding_markers": holding_markers,
                "special_building_markers": special_building_markers,
                "modifier_markers": modifier_markers,
                "domain_markers": domain_markers,
                "matched_registered_policy": matched_registered_policy,
                "matched_catalog_spec": matched_catalog_spec,
                "guard_markers": guard_markers,
                "secondary_markers": secondary_markers,
                "building_type_decision": decision,
                "next_component": next_component,
                "requires_lifecycle_later": False,
                "requires_apply_later": False,
                "corrected_text": "",
                "rationale": rationale,
            }
        )

    reuse_total = sum(reused_policy_counts.values())
    terminal_total = sum(count for decision, count in decision_counts.items() if decision.startswith("building_type_terminal"))
    dominant_decision, dominant_count = decision_counts.most_common(1)[0]
    if dominant_decision.startswith("needs_") and dominant_count >= 20:
        next_prompt = f"chat_exec_{dominant_decision.removeprefix('needs_')}_review_prompt.md"
        recommendation = "open_narrow_subpolicy"
    elif reuse_total >= 30:
        next_prompt = "chat_exec_building_modifier_building_type_policy_catalog_registration_prompt.md"
        recommendation = "register_readonly_reuse_splitter"
    elif terminal_total >= 30:
        next_prompt = "chat_exec_building_modifier_building_type_terminal_spec_registration_prompt.md"
        recommendation = "register_terminal_readonly"
    else:
        next_prompt = "chat_exec_requirement_effect_event_context_policy_review_prompt.md"
        recommendation = "fragmented_close_branch"

    txt_path, jsonl_path, spec_path = output_paths()
    summary = {
        "record_type": "summary",
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "total_reviewed": len(results),
        "pending_no_run_400": len(results),
        "decision_counts": dict(decision_counts),
        "reused_cataloged_policy_count": reuse_total,
        "terminal_policy_count": terminal_total,
        "dominant_subtype": dominant_decision,
        "dominant_count": dominant_count,
        "ready_lifecycle_future": 0,
        "apply_candidates_future": 0,
        "requires_lifecycle_later": False,
        "requires_apply_later": False,
        "recommendation": recommendation,
        "next_prompt": next_prompt,
        "policy_shape": "splitter_candidate" if dominant_decision.startswith("needs_") else "terminal_or_reuse_guard",
    }
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
        for decision, count in decision_counts.most_common():
            handle.write(json.dumps({"record_type": "decision_count", "building_type_decision": decision, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    spec = build_spec(
        decision_counts=decision_counts,
        reused_policies=reused_policy_counts,
        reused_specs=reused_spec_counts,
        total_reviewed=len(results),
        next_components=[next_prompt],
    )
    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Building/modifier building-type policy review\n\n")
        handle.write(f"- total revisado: {len(results)}\n")
        handle.write(f"- reuso policies/specs catalogadas: {reuse_total}\n")
        handle.write(f"- terminal policies futuras: {terminal_total}\n")
        handle.write("- ready lifecycle futuro: 0\n")
        handle.write("- apply candidates futuro: 0\n")
        handle.write(f"- dominante: {dominant_decision} ({dominant_count})\n")
        handle.write(f"- recomendacao: {recommendation}\n")
        handle.write(f"- proximo prompt: {next_prompt}\n\n")
        handle.write("Decisoes\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        for title, counts in [
            ("Top families_open", family_counts),
            ("Top building markers", building_counts),
            ("Top holding/special building markers", holding_counts + special_counts),
            ("Top modifier markers", modifier_counts),
            ("Top domain markers", domain_counts),
            ("Top guard markers", guard_counts),
            ("Top secondary markers", secondary_counts),
        ]:
            handle.write(f"\n{title}\n")
            for key, count in counts.most_common(15):
                handle.write(f"- {key}: {count}\n")
        handle.write("\nConclusoes\n")
        handle.write("- building_modifier_building_type_policy deve virar componente read-only real apenas se a sublane dominante for registrada ou aprofundada.\n")
        handle.write("- lifecycle/apply em curto prazo: nao.\n")
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"total_reviewed: {len(results)}")
    print(f"reused_cataloged_policy_count: {reuse_total}")
    print(f"terminal_policy_count: {terminal_total}")
    print(f"dominant_subtype: {dominant_decision}")
    print(f"dominant_count: {dominant_count}")
    print(f"next_prompt: {next_prompt}")


if __name__ == "__main__":
    main()
