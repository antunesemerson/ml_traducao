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
import requirement_effect_router_readonly as router


PRIMARY_ROUTE = "building_modifier_effect_policy"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_REGISTERED_AGENTS = 218
EXPECTED_UNIVERSE = 866

REGISTERED_REUSE_POLICIES = {
    "script_value_requirement_policy",
    "effect_list_script_value_policy",
    "effect_list_concept_policy",
    "artifact_activity_effect_policy",
}

BUILDING_MARKERS = [
    ("Building", re.compile(r"building|buildings?|duchy_building|special_building", re.I)),
    ("Holding", re.compile(r"holding|castle|city|temple|barony|county", re.I)),
    ("BuildingType", re.compile(r"building_type|GetBuilding|building_slot|construct|constructed", re.I)),
    ("HolySite", re.compile(r"holy_site|holy site|temple|church|cathedral|shrine", re.I)),
]

MODIFIER_MARKERS = [
    ("Modifier", re.compile(r"modifier|building_modifier|county_modifier|holding_modifier", re.I)),
    ("Development", re.compile(r"development|tax|levy|garrison|control|supply|advantage", re.I)),
    ("EffectName", re.compile(r"_effect_name\b|effect_name", re.I)),
]

EFFECT_MARKERS = [
    ("Reward", re.compile(r"reward|gain|loss|opinion|prestige|piety|gold|income", re.I)),
    ("Tooltip", re.compile(r"tooltip|_tt\b|\.tt\b|#T|#help", re.I)),
    ("EffectList", re.compile(r"\n|\\n|EFFECT_LIST_BULLET|BULLET_WITH_TAB", re.I)),
    ("ModifierEffect", re.compile(r"modifier|special_type_bar_segment|effect", re.I)),
]

SCRIPT_VALUE_MARKERS = [
    ("ScriptValue", re.compile(r"ScriptValue|GetScriptValue|GetValue|\|V[0-9]?|\|=\+?0|[0-9]+\s*%", re.I)),
]

DOMAIN_MARKERS = [
    ("CultureReligion", re.compile(r"culture|religion|faith|doctrine|holy_site|church|temple", re.I)),
    ("RealmGovernment", re.compile(r"realm|government|domain|liege|vassal|county|duchy|kingdom", re.I)),
    ("TitleLaw", re.compile(r"title|law|succession|county|duchy|kingdom|empire", re.I)),
]

GUARD_MARKERS = [
    ("ScopeGetterGuard", re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|Get[A-Za-z0-9_]+\(", re.I)),
    ("EventGuard", re.compile(r"event|events|\.desc|desc_|option|flavor|story|scheme|interaction", re.I)),
    ("DomainGuard", re.compile(r"culture|faith|religion|realm|title|law|government|county|duchy|church", re.I)),
    ("BuildingModifierGuard", re.compile(r"building|holding|modifier|county|duchy|temple|church", re.I)),
]

SECONDARY_MARKERS = [
    ("EffectList", re.compile(r"\n|\\n|EFFECT_LIST_BULLET|BULLET_WITH_TAB", re.I)),
    ("Concept", re.compile(r"\[[A-Za-z0-9_]+\||Concept\(|game_concept", re.I)),
    ("TitleLaw", re.compile(r"title|county|duchy|kingdom|empire|law|succession|government", re.I)),
    ("HolySite", re.compile(r"holy_site|holy site|temple|church|cathedral|shrine", re.I)),
    ("ArtifactActivity", re.compile(r"artifact|activity|tournament|hunt|feast|travel|legend", re.I)),
    ("ScopeGetter", re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|Get[A-Za-z0-9_]+\(", re.I)),
    ("ScriptValue", re.compile(r"ScriptValue|GetScriptValue|GetValue|\|V[0-9]?|\|=\+?0|[0-9]+\s*%", re.I)),
    ("CultureReligion", re.compile(r"culture|religion|faith|doctrine|holy_site|church|temple", re.I)),
    ("Event", re.compile(r"event|events|\.desc|desc_|option|flavor|story|scheme|interaction", re.I)),
    ("Domain", re.compile(r"realm|domain|government|county|duchy|kingdom|liege|vassal|court", re.I)),
    ("ResidualVisible", re.compile(r"ÃƒÆ’|Ãƒâ€š|Ã‚Â¿|Ã‚Â¡|Ã¢â‚¬|ï¿½|\bthe\b|\byour\b|\byou\b|\btheir\b|\bcannot\b", re.I)),
    ("DynamicToken", re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)),
]


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_building_modifier_effect_policy_review"
    spec = reports_dir / f"{stamp}_building_modifier_effect_policy_spec.json"
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


def detect(patterns: list[tuple[str, re.Pattern[str]]], blob: str) -> list[str]:
    return [name for name, pattern in patterns if pattern.search(blob)]


def fetch_texts(conn: sqlite3.Connection, segment_ids: list[int]) -> dict[int, dict[str, str]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
          ss.id AS segment_id,
          ss.old_text,
          ss.spanish_text,
          os.portuguese_text AS output_text,
          (
            SELECT sc.confirmed_text
            FROM segment_confirmations sc
            WHERE sc.segment_id = ss.id
            ORDER BY sc.updated_at DESC, sc.id DESC
            LIMIT 1
          ) AS confirmed_text
        FROM source_segments ss
        LEFT JOIN output_segments os ON os.segment_id = ss.id
        WHERE ss.id IN ({placeholders})
        """,
        tuple(segment_ids),
    ).fetchall()
    return {
        int(row["segment_id"]): {
            "old_text": row["old_text"] or row["spanish_text"] or "",
            "confirmed_text": row["confirmed_text"] or "",
            "output_text": row["output_text"] or "",
        }
        for row in rows
    }


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


def route_records(conn: sqlite3.Connection, segment_state_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    router.fetch_runs(conn, segment_state_run_id, ledger_run_id)
    grouped = router.fetch_pending_rows(conn, segment_state_run_id, ledger_run_id)
    records: list[dict[str, Any]] = []
    for segment_id, rows in grouped.items():
        first = rows[0]
        blob = router.blob_for(rows)
        markers = router.detect_markers(blob)
        route, _reason = router.route_for(blob, markers)
        if route != PRIMARY_ROUTE:
            continue
        records.append(
            {
                "segment_id": segment_id,
                "relative_path": str(first.get("relative_path") or ""),
                "source_key": str(first.get("source_key") or ""),
                "families_open": list(router.families_for(rows)),
                "router_markers": markers,
                "state": {
                    "state_group": first.get("state_group"),
                    "is_closed": int(first.get("is_closed") or 0),
                    "needs_output_apply": int(first.get("needs_output_apply") or 0),
                    "confirmed_matches_output": int(first.get("confirmed_matches_output") or 0),
                },
            }
        )
    records.sort(key=lambda row: (row["relative_path"], row["source_key"], int(row["segment_id"])))
    return records


def classify(
    *,
    state: dict[str, Any],
    registered: set[str],
    building_markers: list[str],
    modifier_markers: list[str],
    effect_markers: list[str],
    script_value_markers: list[str],
    domain_markers: list[str],
    guard_markers: list[str],
    secondary_markers: list[str],
) -> tuple[str, str, str, str, str]:
    building = set(building_markers)
    modifier = set(modifier_markers)
    effect = set(effect_markers)
    script = set(script_value_markers)
    domain = set(domain_markers)
    guards = set(guard_markers)
    secondary = set(secondary_markers)
    if state["state_group"] != "pending" or int(state["is_closed"]) != 0:
        return "building_modifier_blocked_uncertain", "", "", "state_guard", "segment is not pending in selected state run"
    if int(state["needs_output_apply"]) != 0 or int(state["confirmed_matches_output"]) != 1:
        return "building_modifier_blocked_uncertain", "", "", "state_guard", "state guard failed: needs_output_apply or confirmed_matches_output"
    if "ResidualVisible" in secondary:
        return "needs_building_modifier_residual_repair", "", "", "residual_dependency_filtered_repair", "visible residual remains"

    if (script or "ScriptValue" in secondary) and "effect_list_script_value_policy" in registered:
        return "building_modifier_reuse_effect_list_script_value_policy", "effect_list_script_value_policy", "effect_list_script_value_policy", "effect_list_script_value_policy", "can reuse registered effect-list ScriptValue policy with building/modifier guard"
    if (script or "ScriptValue" in secondary) and "script_value_requirement_policy" in registered:
        return "building_modifier_reuse_script_value_requirement_policy", "script_value_requirement_policy", "script_value_requirement_policy", "script_value_requirement_policy", "can reuse registered ScriptValue requirement policy"
    if "ArtifactActivity" in secondary and "artifact_activity_effect_policy" in registered:
        return "building_modifier_reuse_artifact_activity_effect_policy", "artifact_activity_effect_policy", "artifact_activity_effect_policy", "artifact_activity_effect_policy", "can reuse registered artifact/activity effect splitter"
    if "Concept" in secondary and "effect_list_concept_policy" in registered:
        return "building_modifier_reuse_effect_list_concept_policy", "effect_list_concept_policy", "effect_list_concept_policy", "effect_list_concept_policy", "can reuse registered effect-list concept policy with building/modifier guard"

    if "EffectList" in secondary:
        return "needs_building_modifier_effect_list_policy", "", "", "effect_list_multiline_policy", "effect-list/multiline dependency remains"
    if "HolySite" in secondary or "HolySite" in building:
        return "needs_building_modifier_holy_site_policy", "", "", "holy_site_effect_name_policy", "holy-site/religion building dependency remains"
    if "TitleLaw" in secondary or "TitleLaw" in domain:
        return "needs_building_modifier_title_law_policy", "", "", "title_law_policy", "title/law/government dependency remains"
    if "ScopeGetter" in secondary or "ScopeGetterGuard" in guards:
        return "needs_building_modifier_scope_getter_policy", "", "", "scope_getter_requirement_policy", "scope/getter dependency dominates"
    if "CultureReligion" in secondary or "CultureReligion" in domain:
        return "needs_building_modifier_culture_religion_policy", "", "", "culture_religion_policy", "culture/religion dependency remains"
    if "Domain" in secondary or "DomainGuard" in guards:
        return "needs_building_modifier_domain_context", "", "", "domain_context_after_requirement_effect", "domain context remains"
    if "Event" in secondary or "EventGuard" in guards:
        return "needs_building_modifier_event_context", "", "", "event_context_after_requirement_effect", "event context remains"
    if building:
        return "needs_building_modifier_building_type_policy", "", "", "building_type_policy", "building/holding type dependency remains"
    if modifier or effect:
        return "building_modifier_terminal_policy", "", "", "terminal_router_policy", "plain building/modifier effect terminal pattern"
    if "DynamicToken" in secondary:
        return "needs_building_modifier_dynamic_parser_escape", "", "", "ck3_dynamic_symbolic_parser", "dynamic parser escape remains"
    return "building_modifier_blocked_uncertain", "", "", "human_review_or_evidence_collection", "insufficient building/modifier evidence"


def build_spec(
    *,
    decision_counts: Counter[str],
    reused_policies: Counter[str],
    reused_specs: Counter[str],
    total_reviewed: int,
    universe: int,
    next_components: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": "requirement_effect_router_readonly",
        "policy_id": "building_modifier_effect_policy",
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "entry_conditions": [
            "route == building_modifier_effect_policy",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
        ],
        "reused_registered_policies": [{"agent_key": key, "sampled": count} for key, count in reused_policies.most_common()],
        "reused_catalog_specs": [{"policy_id": key, "sampled": count} for key, count in reused_specs.most_common()],
        "building_modifier_types": [{"decision": key, "sampled": count} for key, count in decision_counts.most_common()],
        "resolution_order": [
            "state guard",
            "residual guard",
            "ScriptValue reuse",
            "artifact/activity reuse",
            "effect-list concept reuse",
            "building/domain/title/culture sublanes",
            "event/domain/dynamic fallback",
        ],
        "next_components": next_components,
        "blocked_conditions": [
            "state guard failed",
            "visible residual",
            "missing building/modifier evidence",
        ],
        "promotion_gate": "read_only_component_only_no_apply_no_lifecycle",
        "sampled": total_reviewed,
        "universe_estimated": universe,
        "policy_shape": "splitter_candidate",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only building/modifier effect policy review.")
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    parser.add_argument("--limit", type=int, default=240)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    if args.ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("ledger_run_id guard failed")

    with connect_readonly() as conn:
        records = route_records(conn, args.segment_state_run_id, args.ledger_run_id)
        registered = registered_policies(conn)
        selected = records[: min(args.limit, 240)]
        texts = fetch_texts(conn, [int(row["segment_id"]) for row in selected])

    universe = len(records)
    if universe != EXPECTED_UNIVERSE:
        raise SystemExit(f"universe guard failed: {universe} expected {EXPECTED_UNIVERSE}")

    results: list[dict[str, Any]] = []
    decision_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    building_counts: Counter[str] = Counter()
    modifier_counts: Counter[str] = Counter()
    effect_counts: Counter[str] = Counter()
    script_counts: Counter[str] = Counter()
    domain_counts: Counter[str] = Counter()
    guard_counts: Counter[str] = Counter()
    secondary_counts: Counter[str] = Counter()
    reused_policy_counts: Counter[str] = Counter()
    reused_spec_counts: Counter[str] = Counter()

    for record in selected:
        segment_id = int(record["segment_id"])
        text = texts.get(segment_id, {"old_text": "", "confirmed_text": "", "output_text": ""})
        blob = " ".join(
            [
                record["relative_path"],
                record["source_key"],
                text["old_text"],
                text["confirmed_text"],
                text["output_text"],
                " ".join(record["families_open"]),
            ]
        )
        building_markers = detect(BUILDING_MARKERS, blob)
        modifier_markers = detect(MODIFIER_MARKERS, blob)
        effect_markers = detect(EFFECT_MARKERS, blob)
        script_value_markers = detect(SCRIPT_VALUE_MARKERS, blob)
        domain_markers = detect(DOMAIN_MARKERS, blob)
        guard_markers = detect(GUARD_MARKERS, blob)
        secondary_markers = detect(SECONDARY_MARKERS, blob)
        decision, matched_registered_policy, matched_catalog_spec, next_component, rationale = classify(
            state=record["state"],
            registered=registered,
            building_markers=building_markers,
            modifier_markers=modifier_markers,
            effect_markers=effect_markers,
            script_value_markers=script_value_markers,
            domain_markers=domain_markers,
            guard_markers=guard_markers,
            secondary_markers=secondary_markers,
        )
        family_counts.update(record["families_open"])
        building_counts.update(building_markers)
        modifier_counts.update(modifier_markers)
        effect_counts.update(effect_markers)
        script_counts.update(script_value_markers)
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
                "relative_path": record["relative_path"],
                "source_key": record["source_key"],
                "families_open": record["families_open"],
                "primary_route": PRIMARY_ROUTE,
                "old_text": text["old_text"],
                "confirmed_text": text["confirmed_text"],
                "output_text": text["output_text"],
                "building_markers": building_markers,
                "modifier_markers": modifier_markers,
                "effect_markers": effect_markers,
                "script_value_markers": script_value_markers,
                "domain_markers": domain_markers,
                "matched_registered_policy": matched_registered_policy,
                "matched_catalog_spec": matched_catalog_spec,
                "guard_markers": guard_markers,
                "secondary_markers": secondary_markers,
                "building_modifier_decision": decision,
                "next_component": next_component,
                "requires_lifecycle_later": False,
                "requires_apply_later": False,
                "corrected_text": "",
                "rationale": rationale,
            }
        )

    reuse_total = sum(reused_policy_counts.values())
    terminal_total = sum(count for decision, count in decision_counts.items() if decision.startswith("building_modifier_terminal"))
    dominant_decision, dominant_count = decision_counts.most_common(1)[0] if decision_counts else ("", 0)
    if dominant_decision.startswith("needs_") and dominant_count >= 50:
        next_prompt = f"chat_exec_{dominant_decision.removeprefix('needs_')}_review_prompt.md"
        recommendation = "open_narrow_subpolicy"
    elif reuse_total >= 70:
        next_prompt = "chat_exec_building_modifier_effect_policy_catalog_registration_prompt.md"
        recommendation = "register_readonly_reuse_splitter"
    elif terminal_total >= 70:
        next_prompt = "chat_exec_building_modifier_effect_terminal_spec_registration_prompt.md"
        recommendation = "register_terminal_readonly"
    else:
        next_prompt = "chat_exec_requirement_effect_event_context_policy_review_prompt.md"
        recommendation = "fragmented_move_to_next_large_route"

    txt_path, jsonl_path, spec_path = output_paths()
    summary = {
        "record_type": "summary",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": args.ledger_run_id,
        "universe_estimated": universe,
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
        "policy_shape": "reuse_splitter_route" if reuse_total >= 70 else "splitter_candidate",
    }
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
        for decision, count in decision_counts.most_common():
            handle.write(json.dumps({"record_type": "decision_count", "building_modifier_decision": decision, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    spec = build_spec(
        decision_counts=decision_counts,
        reused_policies=reused_policy_counts,
        reused_specs=reused_spec_counts,
        total_reviewed=len(results),
        universe=universe,
        next_components=[next_prompt],
    )
    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Building/modifier effect policy review\n\n")
        handle.write(f"- universo estimado: {universe}\n")
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
            ("Top modifier markers", modifier_counts),
            ("Top effect markers", effect_counts),
            ("Top ScriptValue markers", script_counts),
            ("Top domain markers", domain_counts),
            ("Top guard markers", guard_counts),
            ("Top secondary markers", secondary_counts),
        ]:
            handle.write(f"\n{title}\n")
            for key, count in counts.most_common(15):
                handle.write(f"- {key}: {count}\n")
        handle.write("\nConclusoes\n")
        handle.write("- building_modifier_effect_policy deve virar componente read-only real se o reuso catalogado continuar alto.\n")
        handle.write("- lifecycle/apply em curto prazo: nao.\n")
        handle.write("- a policy reaproveita parcialmente effect-list/script/concept/artifact, mas tambem revela sublanes de dominio/building.\n")
    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"universe_estimated: {universe}")
    print(f"total_reviewed: {len(results)}")
    print(f"reused_cataloged_policy_count: {reuse_total}")
    print(f"terminal_policy_count: {terminal_total}")
    print(f"dominant_subtype: {dominant_decision}")
    print(f"dominant_count: {dominant_count}")
    print(f"next_prompt: {next_prompt}")


if __name__ == "__main__":
    main()
