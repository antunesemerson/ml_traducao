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


PRIMARY_ROUTE = "holy_site_effect_name_policy"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_UNIVERSE = 286
EXPECTED_REGISTERED_AGENTS = 224

REUSE_POLICIES = {
    "building_modifier_effect_policy",
    "building_modifier_building_type_policy",
    "effect_list_concept_policy",
    "script_value_effect_policy",
    "concept_requirement_policy",
    "event_context_after_requirement_effect",
}

SPEC_PATHS = {
    "building_modifier_effect_policy": "reports/20260622_161624_167472_building_modifier_effect_policy_spec.json",
    "building_modifier_building_type_policy": "reports/20260622_164542_376256_building_modifier_building_type_policy_spec.json",
    "effect_list_concept_policy": "reports/20260622_144059_266106_effect_list_concept_policy_spec.json",
    "effect_list_script_value_policy": "reports/20260622_141149_802441_effect_list_script_value_policy_spec.json",
    "event_context_after_requirement_effect": "reports/20260622_171258_175723_requirement_effect_event_context_policy_spec.json",
    "script_value_effect_policy": "reports/20260622_181934_836489_script_value_effect_policy_spec.json",
    "concept_requirement_policy": "reports/20260621_221617_085028_concept_requirement_policy_spec.json",
}

HOLY_SITE_MARKERS = [
    ("HolySite", re.compile(r"holy_site|holy site|holy_site_name|holy_site_effect", re.I)),
    ("ShrineTemple", re.compile(r"shrine|temple|cathedral|mosque|church|sanctuary|sacred", re.I)),
    ("Pilgrimage", re.compile(r"pilgrim|pilgrimage|hajj|journey_to", re.I)),
]

RELIGION_MARKERS = [
    ("Religion", re.compile(r"religion|religious|faith|doctrine|tenet|piety", re.I)),
    ("Clergy", re.compile(r"clergy|priest|bishop|imam|chaplain|church", re.I)),
    ("HolyOrder", re.compile(r"holy_order|holy order|crusade|jihad", re.I)),
]

FAITH_MARKERS = [
    ("Faith", re.compile(r"\bfaith\b|GetFaith|FAITH\.|same_faith|faithful", re.I)),
    ("DoctrineTenet", re.compile(r"doctrine|tenet|heresy|hostility|fervor", re.I)),
]

NAME_LOCATION_MARKERS = [
    ("Name", re.compile(r"\bname\b|GetName|GetBaseName|GetTitleName|localized_name|effect_name", re.I)),
    ("Location", re.compile(r"location|province|county|barony|capital|site|place|region", re.I)),
    ("TitlePlace", re.compile(r"title|county|duchy|kingdom|empire|barony|holding", re.I)),
]

BUILDING_MARKERS = [
    ("Building", re.compile(r"building|holding|special_building|duchy_building|construct", re.I)),
    ("ModifierBuilding", re.compile(r"modifier|building_modifier|county_modifier|development|tax|levy|garrison", re.I)),
]

EFFECT_MARKERS = [
    ("Effect", re.compile(r"effect|_effect_name\b|gain|lose|loss|add_|remove_", re.I)),
    ("Tooltip", re.compile(r"tooltip|_tt\b|\.tt\b|#T|#help", re.I)),
    ("EffectList", re.compile(r"\n|\\n|EFFECT_LIST_BULLET|BULLET_WITH_TAB|#indent", re.I)),
]

GUARD_MARKERS = [
    ("DomainGuard", re.compile(r"culture|faith|religion|realm|title|law|government|succession|church|court|domain", re.I)),
    ("NameGuard", re.compile(r"name|GetName|location|province|county|barony|site|place", re.I)),
    ("BuildingGuard", re.compile(r"building|holding|modifier|temple|church|cathedral|shrine", re.I)),
    ("EventGuard", re.compile(r"event|events/|\.desc|desc_|option|story|scheme|interaction", re.I)),
]

SECONDARY_MARKERS = [
    ("BuildingModifier", re.compile(r"building|holding|modifier|county_modifier|duchy_building|development|tax|levy", re.I)),
    ("TitleLaw", re.compile(r"title|law|government|realm|succession|county|duchy|kingdom|empire|rank|holding", re.I)),
    ("Concept", re.compile(r"\[[A-Za-z0-9_]+\||Concept\(|game_concept", re.I)),
    ("ScriptValue", re.compile(r"ScriptValue|GetScriptValue|GetValue|\|V[0-9]?|\|=\+?0|[0-9]+\s*%", re.I)),
    ("EffectList", re.compile(r"\n|\\n|EFFECT_LIST_BULLET|BULLET_WITH_TAB|#indent", re.I)),
    ("CultureFaith", re.compile(r"culture|faith|religion|doctrine|tenet|heritage|tradition", re.I)),
    ("Event", re.compile(r"event|events/|\.desc|desc_|option|toast|dialogue|story|scheme|interaction|memory", re.I)),
    ("Domain", re.compile(r"domain|realm|court|council|government|province|county", re.I)),
    ("ResidualVisible", re.compile(r"Ã|Â|�|â€™|â€œ|â€|\b(?:the|your|you|their|cannot|consiguio|sera|mas|facil)\b", re.I)),
    ("DynamicToken", re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)),
]


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_holy_site_effect_name_policy_review"
    spec = reports_dir / f"{stamp}_holy_site_effect_name_policy_spec.json"
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
    placeholders = ",".join("?" for _ in REUSE_POLICIES)
    rows = conn.execute(
        f"""
        SELECT agent_key
        FROM ml_agent_registry
        WHERE agent_key IN ({placeholders})
          AND status = 'active'
        """,
        tuple(sorted(REUSE_POLICIES)),
    ).fetchall()
    return {str(row["agent_key"]) for row in rows}


def catalog_specs() -> set[str]:
    present: set[str] = set()
    for key, rel_path in SPEC_PATHS.items():
        path = Path.cwd() / rel_path
        if not path.exists():
            continue
        json.loads(path.read_text(encoding="utf-8"))
        present.add(key)
    return present


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


def reuse_decision(policy: str, decision: str, registered: set[str], specs: set[str], rationale: str) -> tuple[str, str, str, str, str] | None:
    if policy in registered or policy in specs:
        return decision, policy if policy in registered else "", policy if policy in specs else "", policy, rationale
    return None


def classify(
    *,
    state: dict[str, Any],
    registered: set[str],
    specs: set[str],
    holy_site_markers: list[str],
    religion_markers: list[str],
    faith_markers: list[str],
    name_location_markers: list[str],
    building_markers: list[str],
    effect_markers: list[str],
    guard_markers: list[str],
    secondary_markers: list[str],
) -> tuple[str, str, str, str, str]:
    holy = set(holy_site_markers)
    religion = set(religion_markers)
    faith = set(faith_markers)
    names = set(name_location_markers)
    buildings = set(building_markers)
    effects = set(effect_markers)
    guards = set(guard_markers)
    secondary = set(secondary_markers)
    if state["state_group"] != "pending" or int(state["is_closed"]) != 0:
        return "holy_site_blocked_uncertain", "", "", "state_guard", "segment is not pending in selected state run"
    if int(state["needs_output_apply"]) != 0 or int(state["confirmed_matches_output"]) != 1:
        return "holy_site_blocked_uncertain", "", "", "state_guard", "state guard failed: needs_output_apply or confirmed_matches_output"
    if "ResidualVisible" in secondary:
        return "needs_holy_site_residual_repair", "", "", "holy_site_residual_repair", "visible residual remains"

    for policy, decision, condition, rationale in [
        ("building_modifier_effect_policy", "holy_site_reuse_building_modifier_effect_policy", bool("BuildingModifier" in secondary and buildings), "building/modifier effect splitter matches holy-site building surface"),
        ("building_modifier_building_type_policy", "holy_site_reuse_building_type_policy", bool(buildings and not effects), "building type policy matches holy-site building/name surface"),
        ("script_value_effect_policy", "holy_site_reuse_script_value_effect_policy", bool("ScriptValue" in secondary), "ScriptValue effect splitter matches holy-site numeric surface"),
        ("effect_list_concept_policy", "holy_site_reuse_effect_list_concept_policy", bool("Concept" in secondary or "EffectList" in secondary), "effect-list concept policy matches concept/effect-list holy-site surface"),
        ("concept_requirement_policy", "holy_site_reuse_concept_requirement_policy", bool("Concept" in secondary), "concept requirement spec matches holy-site concept surface"),
        ("event_context_after_requirement_effect", "holy_site_reuse_event_context_policy", bool("Event" in secondary or "EventGuard" in guards), "event-context splitter matches holy-site event surface"),
    ]:
        if condition:
            reused = reuse_decision(policy, decision, registered, specs, rationale)
            if reused is not None:
                return reused

    if holy and (religion or faith) and not secondary - {"CultureFaith", "Domain"}:
        return "holy_site_terminal_policy_with_domain_guard", "", "", "holy_site_terminal_policy", "terminal holy-site/religion effect with domain guard"
    if holy and names and not buildings:
        return "holy_site_terminal_policy_with_name_guard", "", "", "holy_site_terminal_policy", "terminal holy-site name/location effect"
    if holy and buildings:
        return "holy_site_terminal_policy_with_building_guard", "", "", "holy_site_terminal_policy", "terminal holy-site building effect"
    if holy or religion or faith:
        return "holy_site_terminal_policy", "", "", "holy_site_terminal_policy", "plain terminal holy-site/religion effect"

    if religion or "DoctrineTenet" in faith:
        return "needs_holy_site_religion_doctrine_policy", "", "", "holy_site_religion_doctrine_policy", "religion/doctrine dependency remains"
    if names:
        return "needs_holy_site_name_or_location_policy", "", "", "holy_site_name_location_policy", "name/location dependency remains"
    if buildings or "BuildingModifier" in secondary:
        return "needs_holy_site_building_modifier_policy", "", "", "building_modifier_effect_policy", "building/modifier dependency remains"
    if "TitleLaw" in secondary:
        return "needs_holy_site_title_law_policy", "", "", "holy_site_title_law_policy", "title/law dependency remains"
    if "CultureFaith" in secondary:
        return "needs_holy_site_culture_or_faith_policy", "", "", "holy_site_culture_faith_policy", "culture/faith dependency remains"
    if "Concept" in secondary:
        return "needs_holy_site_concept_policy", "", "", "concept_requirement_policy", "concept dependency remains"
    if "ScriptValue" in secondary:
        return "needs_holy_site_script_value_policy", "", "", "script_value_effect_policy", "ScriptValue dependency remains"
    if "EffectList" in secondary:
        return "needs_holy_site_effect_list_policy", "", "", "effect_list_concept_policy", "effect-list dependency remains"
    if "Event" in secondary or "EventGuard" in guards:
        return "needs_holy_site_event_context", "", "", "event_context_after_requirement_effect", "event context remains"
    if "Domain" in secondary or "DomainGuard" in guards:
        return "needs_holy_site_domain_context", "", "", "domain_context_after_requirement_effect", "domain context remains"
    if "DynamicToken" in secondary:
        return "needs_holy_site_dynamic_parser_escape", "", "", "ck3_dynamic_symbolic_parser", "dynamic parser escape remains"
    return "holy_site_blocked_uncertain", "", "", "human_review_or_evidence_collection", "insufficient holy-site evidence"


def count_records(values: Counter[str], field: str) -> list[dict[str, Any]]:
    return [{"record_type": field, "value": key, "segments": count} for key, count in values.most_common()]


def build_spec(
    *,
    decision_counts: Counter[str],
    reused_policies: Counter[str],
    reused_specs: Counter[str],
    type_counts: Counter[str],
    next_components: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": "requirement_effect_router_readonly",
        "policy_id": "holy_site_effect_name_policy",
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "entry_conditions": [
            "route == holy_site_effect_name_policy",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
        ],
        "reused_registered_policies": [{"agent_key": key, "sampled": count} for key, count in reused_policies.most_common()],
        "reused_catalog_specs": [{"policy_id": key, "sampled": count} for key, count in reused_specs.most_common()],
        "holy_site_effect_types": [{"type": key, "sampled": count} for key, count in type_counts.most_common()],
        "resolution_order": [
            "state guard",
            "visible residual guard",
            "building/modifier and building type reuse",
            "ScriptValue/concept/event reuse",
            "terminal holy-site/religion/name guards",
            "religion/name/building sublanes",
            "domain/event/dynamic fallback",
        ],
        "next_components": next_components,
        "blocked_conditions": [
            "state guard failed",
            "visible residual",
            "missing holy-site/religion/name evidence",
            "ambiguous dynamic structure",
        ],
        "promotion_gate": "read_only_component_only_no_apply_no_lifecycle",
        "observed_decision_counts": dict(decision_counts),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only holy-site effect name policy review.")
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", required=True, type=int)
    parser.add_argument("--limit", type=int, default=240)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    if args.ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("ledger_run_id guard failed")

    specs = catalog_specs()
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
    holy_counts: Counter[str] = Counter()
    religion_counts: Counter[str] = Counter()
    faith_counts: Counter[str] = Counter()
    name_location_counts: Counter[str] = Counter()
    building_counts: Counter[str] = Counter()
    effect_counts: Counter[str] = Counter()
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
        holy_site_markers = detect(HOLY_SITE_MARKERS, blob)
        religion_markers = detect(RELIGION_MARKERS, blob)
        faith_markers = detect(FAITH_MARKERS, blob)
        name_location_markers = detect(NAME_LOCATION_MARKERS, blob)
        building_markers = detect(BUILDING_MARKERS, blob)
        effect_markers = detect(EFFECT_MARKERS, blob)
        guard_markers = detect(GUARD_MARKERS, blob)
        secondary_markers = detect(SECONDARY_MARKERS, blob)
        decision, matched_registered_policy, matched_catalog_spec, next_component, rationale = classify(
            state=record["state"],
            registered=registered,
            specs=specs,
            holy_site_markers=holy_site_markers,
            religion_markers=religion_markers,
            faith_markers=faith_markers,
            name_location_markers=name_location_markers,
            building_markers=building_markers,
            effect_markers=effect_markers,
            guard_markers=guard_markers,
            secondary_markers=secondary_markers,
        )
        family_counts.update(record["families_open"])
        holy_counts.update(holy_site_markers)
        religion_counts.update(religion_markers)
        faith_counts.update(faith_markers)
        name_location_counts.update(name_location_markers)
        building_counts.update(building_markers)
        effect_counts.update(effect_markers)
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
                "holy_site_markers": holy_site_markers,
                "religion_markers": religion_markers,
                "faith_markers": faith_markers,
                "name_location_markers": name_location_markers,
                "building_markers": building_markers,
                "effect_markers": effect_markers,
                "matched_registered_policy": matched_registered_policy,
                "matched_catalog_spec": matched_catalog_spec,
                "guard_markers": guard_markers,
                "secondary_markers": secondary_markers,
                "holy_site_decision": decision,
                "next_component": next_component,
                "requires_lifecycle_later": False,
                "requires_apply_later": False,
                "corrected_text": "",
                "rationale": rationale,
            }
        )

    reuse_total = sum(1 for row in results if row["matched_registered_policy"] or row["matched_catalog_spec"])
    terminal_total = sum(count for decision, count in decision_counts.items() if decision.startswith("holy_site_terminal"))
    dominant_decision, dominant_count = decision_counts.most_common(1)[0] if decision_counts else ("", 0)
    if dominant_decision.startswith("needs_") and dominant_count >= 30:
        next_prompt = f"chat_exec_{dominant_decision.removeprefix('needs_')}_review_prompt.md"
        recommendation = "open_narrow_subpolicy"
    elif reuse_total >= 50:
        next_prompt = "chat_exec_holy_site_effect_name_policy_catalog_registration_prompt.md"
        recommendation = "register_readonly_reuse_splitter"
    elif terminal_total >= 50:
        next_prompt = "chat_exec_holy_site_effect_name_terminal_spec_registration_prompt.md"
        recommendation = "register_terminal_readonly"
    else:
        next_prompt = "chat_exec_global_post_holy_site_policy_diagnostic_prompt.md"
        recommendation = "fragmented_global_diagnostic"

    txt_path, jsonl_path, spec_path = output_paths()
    summary = {
        "record_type": "summary",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": args.ledger_run_id,
        "universe_estimated": universe,
        "total_reviewed": len(results),
        "decision_counts": dict(decision_counts),
        "reused_cataloged_policy_count": reuse_total,
        "reused_registered_policy_count": sum(reused_policy_counts.values()),
        "reused_catalog_spec_count": sum(reused_spec_counts.values()),
        "terminal_policy_count": terminal_total,
        "dominant_subtype": dominant_decision,
        "dominant_count": dominant_count,
        "ready_lifecycle_future": 0,
        "apply_candidates_future": 0,
        "requires_lifecycle_later": False,
        "requires_apply_later": False,
        "recommendation": recommendation,
        "next_prompt": next_prompt,
        "policy_shape": "reuse_splitter_route" if reuse_total >= 50 else ("terminal_policy" if terminal_total >= 50 else "splitter_candidate"),
    }
    spec = build_spec(
        decision_counts=decision_counts,
        reused_policies=reused_policy_counts,
        reused_specs=reused_spec_counts,
        type_counts=holy_counts + religion_counts + faith_counts + name_location_counts + building_counts + effect_counts + secondary_counts,
        next_components=[next_prompt],
    )

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
        for decision, count in decision_counts.most_common():
            handle.write(json.dumps({"record_type": "decision_count", "holy_site_decision": decision, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for section_name, counts in [
            ("top_family", family_counts),
            ("top_holy_site_marker", holy_counts),
            ("top_religion_marker", religion_counts),
            ("top_faith_marker", faith_counts),
            ("top_name_location_marker", name_location_counts),
            ("top_building_marker", building_counts),
            ("top_effect_marker", effect_counts),
            ("top_guard_marker", guard_counts),
            ("top_secondary_marker", secondary_counts),
        ]:
            for row in count_records(counts, section_name):
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")

    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Holy-site effect name policy review\n\n")
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
            ("Top holy site markers", holy_counts),
            ("Top religion/faith markers", religion_counts + faith_counts),
            ("Top name/location markers", name_location_counts),
            ("Top building markers", building_counts),
            ("Top effect markers", effect_counts),
            ("Top guard markers", guard_counts),
            ("Top secondary markers", secondary_counts),
        ]:
            handle.write(f"\n{title}\n")
            for key, count in counts.most_common(15):
                handle.write(f"- {key}: {count}\n")
        handle.write("\nRespostas\n")
        handle.write("- holy_site_effect_name_policy deve virar componente read-only real se reuso >= 50 ou terminalidade >= 50.\n")
        handle.write("- lifecycle/apply em curto prazo: nao.\n")
        handle.write("- reaproveitamento medido contra building/modifier, concept, ScriptValue, event-context e concept requirement.\n")
        handle.write(f"- proximo prompt recomendado: {next_prompt}\n")
        handle.write("- sem escrita em banco, source ou output.\n")

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
