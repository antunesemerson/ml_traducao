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


PRIMARY_ROUTE = "script_value_effect_policy"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_UNIVERSE = 382
EXPECTED_REGISTERED_AGENTS = 223

REUSE_POLICIES = {
    "script_value_requirement_policy",
    "effect_list_script_value_policy",
    "artifact_activity_script_value_policy",
    "artifact_activity_effect_policy",
    "building_modifier_effect_policy",
    "effect_list_concept_policy",
    "accolade_trait_requirement_policy",
}

SPEC_PATHS = {
    "script_value_requirement_policy": "reports/20260621_220608_287532_script_value_requirement_policy_spec.json",
    "artifact_activity_script_value_policy": "reports/20260622_123357_154242_artifact_activity_script_value_policy_spec.json",
    "effect_list_script_value_policy": "reports/20260622_141149_802441_effect_list_script_value_policy_spec.json",
    "artifact_activity_effect_policy": "reports/20260622_155336_452988_artifact_activity_effect_policy_spec.json",
    "building_modifier_effect_policy": "reports/20260622_161624_167472_building_modifier_effect_policy_spec.json",
    "effect_list_concept_policy": "reports/20260622_144059_266106_effect_list_concept_policy_spec.json",
    "accolade_trait_requirement_policy": "reports/20260622_175931_206005_accolade_trait_requirement_policy_spec.json",
}

SCRIPT_VALUE_MARKERS = [
    ("ScriptValue", re.compile(r"ScriptValue|GetScriptValue", re.I)),
    ("ScopeScriptValue", re.compile(r"SCOPE\.ScriptValue|MakeScope\.ScriptValue|EmptyScope\.ScriptValue", re.I)),
    ("GetValue", re.compile(r"GetValue|Subtract_CFixedPoint|Add_CFixedPoint|Multiply_CFixedPoint", re.I)),
    ("ValueFormat", re.compile(r"\|[PNV+0/%^.=:-]+|#(?:P|N|V|Z|bold)", re.I)),
]

NUMERIC_MARKERS = [
    ("Percent", re.compile(r"[0-9]+\s*%|\|[PNV+0/%^.-]*%", re.I)),
    ("SignedValue", re.compile(r"#(?:P|N)\s*[+-]?|\|[+-]?[0-9]", re.I)),
    ("NumericComparator", re.compile(r">=|<=|greater|less|at_least|at_most|threshold|minimum|maximum", re.I)),
    ("ModifierNumber", re.compile(r"modifier|value|bonus|penalty|multiplier|factor", re.I)),
]

EFFECT_MARKERS = [
    ("Effect", re.compile(r"effect|_effect_name\b|gain|lose|loss|add_|remove_", re.I)),
    ("Modifier", re.compile(r"modifier|opinion|prestige|piety|gold|stress|tax|levy", re.I)),
    ("Tooltip", re.compile(r"tooltip|_tt\b|\.tt\b|#T|#help", re.I)),
]

EFFECT_LIST_MARKERS = [
    ("Multiline", re.compile(r"\n|\\n|EFFECT_LIST_BULLET|BULLET_WITH_TAB|#indent", re.I)),
    ("EffectListBullet", re.compile(r"EFFECT_LIST_BULLET|BULLET_WITH_TAB|\$TAB|\$BULLET", re.I)),
]

ARTIFACT_ACTIVITY_MARKERS = [
    ("Artifact", re.compile(r"artifact|court_artifact|relic|inventory|antiquarian", re.I)),
    ("Activity", re.compile(r"activity|tournament|travel|feast|hunt|pilgrimage|journey|legend", re.I)),
]

BUILDING_MODIFIER_MARKERS = [
    ("Building", re.compile(r"building|holding|county|duchy_building|special_building", re.I)),
    ("BuildingModifier", re.compile(r"building_modifier|county_modifier|holding_modifier|development|tax|levy|garrison", re.I)),
]

GUARD_MARKERS = [
    ("NumericGuard", re.compile(r"ScriptValue|GetScriptValue|GetValue|\|[PNV+0/%^.-]+|#(?:P|N|V|Z|bold)|[0-9]+\s*%", re.I)),
    ("EffectGuard", re.compile(r"effect|modifier|gain|loss|add_|remove_|tooltip", re.I)),
    ("DomainGuard", re.compile(r"culture|faith|religion|realm|title|law|government|building|court|domain", re.I)),
    ("EventGuard", re.compile(r"event|events/|\.desc|desc_|option|story|scheme|interaction", re.I)),
    ("ScopeGetterGuard", re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|Get[A-Za-z0-9_]+\(", re.I)),
]

SECONDARY_MARKERS = [
    ("ArtifactActivity", re.compile(r"artifact|activity|tournament|travel|legend|relic|inventory|hunt|feast", re.I)),
    ("BuildingModifier", re.compile(r"building|holding|modifier|county_modifier|duchy_building|development|tax|levy", re.I)),
    ("Concept", re.compile(r"\[[A-Za-z0-9_]+\||Concept\(|game_concept", re.I)),
    ("TitleLaw", re.compile(r"title|law|government|realm|succession|county|duchy|kingdom|empire|rank|holding", re.I)),
    ("TraitAccolade", re.compile(r"GetTrait|trait|accolade|knight|prowess|aptitude", re.I)),
    ("ScopeGetter", re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|Get[A-Za-z0-9_]+\(", re.I)),
    ("GenderLocalPlayer", re.compile(r"Select_CString|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)|local_player|GetPlayer|\bseu\b|\bsua\b", re.I)),
    ("Domain", re.compile(r"culture|religion|faith|doctrine|tradition|domain|realm|court", re.I)),
    ("Event", re.compile(r"event|events/|\.desc|desc_|option|toast|dialogue|story|scheme|interaction|memory", re.I)),
    ("ResidualVisible", re.compile(r"Ã|Â|�|â€™|â€œ|â€|\b(?:the|your|you|their|cannot|consiguio|sera|mas|facil)\b", re.I)),
    ("DynamicToken", re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)),
]


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_script_value_effect_policy_review"
    spec = reports_dir / f"{stamp}_script_value_effect_policy_spec.json"
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
    script_value_markers: list[str],
    numeric_markers: list[str],
    effect_markers: list[str],
    effect_list_markers: list[str],
    artifact_activity_markers: list[str],
    building_modifier_markers: list[str],
    guard_markers: list[str],
    secondary_markers: list[str],
) -> tuple[str, str, str, str, str]:
    script = set(script_value_markers)
    numeric = set(numeric_markers)
    effects = set(effect_markers)
    effect_list = set(effect_list_markers)
    artifact_activity = set(artifact_activity_markers)
    building_modifier = set(building_modifier_markers)
    guards = set(guard_markers)
    secondary = set(secondary_markers)
    if state["state_group"] != "pending" or int(state["is_closed"]) != 0:
        return "script_value_effect_blocked_uncertain", "", "", "state_guard", "segment is not pending in selected state run"
    if int(state["needs_output_apply"]) != 0 or int(state["confirmed_matches_output"]) != 1:
        return "script_value_effect_blocked_uncertain", "", "", "state_guard", "state guard failed: needs_output_apply or confirmed_matches_output"
    if "ResidualVisible" in secondary:
        return "needs_script_value_effect_residual_repair", "", "", "script_value_effect_residual_repair", "visible residual remains"

    reuse_candidates = [
        ("artifact_activity_script_value_policy", "script_value_effect_reuse_artifact_activity_script_value_policy", bool(artifact_activity and script), "artifact/activity ScriptValue spec matches this effect"),
        ("artifact_activity_effect_policy", "script_value_effect_reuse_artifact_activity_effect_policy", bool("ArtifactActivity" in secondary or artifact_activity), "artifact/activity effect splitter matches this effect"),
        ("building_modifier_effect_policy", "script_value_effect_reuse_building_modifier_effect_policy", bool("BuildingModifier" in secondary or building_modifier), "building/modifier effect splitter matches this effect"),
        ("effect_list_script_value_policy", "script_value_effect_reuse_effect_list_script_value_policy", bool(effect_list and script), "effect-list ScriptValue policy matches this effect"),
        ("effect_list_concept_policy", "script_value_effect_reuse_effect_list_concept_policy", bool("Concept" in secondary), "effect-list concept policy matches this effect"),
        ("accolade_trait_requirement_policy", "script_value_effect_reuse_accolade_trait_requirement_policy", bool("TraitAccolade" in secondary), "accolade/trait requirement splitter matches this effect"),
        ("script_value_requirement_policy", "script_value_effect_reuse_script_value_requirement_policy", bool(script), "ScriptValue requirement spec matches this effect"),
    ]
    for policy, decision, condition, rationale in reuse_candidates:
        if condition:
            reused = reuse_decision(policy, decision, registered, specs, rationale)
            if reused is not None:
                return reused

    if numeric and any(marker == "Percent" for marker in numeric):
        return "needs_script_value_effect_percent_modifier_policy", "", "", "script_value_effect_percent_modifier_policy", "percent modifier ScriptValue dependency remains"
    if numeric:
        return "needs_script_value_effect_numeric_modifier_policy", "", "", "script_value_effect_numeric_modifier_policy", "numeric modifier ScriptValue dependency remains"
    if artifact_activity:
        return "needs_script_value_effect_artifact_activity_policy", "", "", "artifact_activity_script_value_policy", "artifact/activity dependency remains"
    if building_modifier:
        return "needs_script_value_effect_building_modifier_policy", "", "", "building_modifier_effect_policy", "building/modifier dependency remains"
    if effect_list:
        return "needs_script_value_effect_effect_list_policy", "", "", "effect_list_script_value_policy", "effect-list dependency remains"
    if "Concept" in secondary:
        return "needs_script_value_effect_concept_policy", "", "", "effect_list_concept_policy", "concept dependency remains"
    if "TitleLaw" in secondary:
        return "needs_script_value_effect_title_law_policy", "", "", "script_value_title_law_policy", "title/law dependency remains"
    if "TraitAccolade" in secondary:
        return "needs_script_value_effect_accolade_trait_policy", "", "", "accolade_trait_requirement_policy", "accolade/trait dependency remains"
    if "ScopeGetter" in secondary or "ScopeGetterGuard" in guards:
        return "needs_script_value_effect_scope_getter_policy", "", "", "script_value_scope_getter_policy", "scope/getter dependency remains"
    if "GenderLocalPlayer" in secondary:
        return "needs_script_value_effect_gender_local_player_policy", "", "", "script_value_gender_local_player_policy", "gender/local-player dependency remains"
    if "Domain" in secondary or "DomainGuard" in guards:
        return "needs_script_value_effect_domain_context", "", "", "domain_context_after_requirement_effect", "domain context remains"
    if "Event" in secondary or "EventGuard" in guards:
        return "needs_script_value_effect_event_context", "", "", "event_context_after_requirement_effect", "event context remains"
    if "DynamicToken" in secondary:
        return "needs_script_value_effect_dynamic_parser_escape", "", "", "ck3_dynamic_symbolic_parser", "dynamic parser escape remains"
    if script and numeric:
        return "script_value_effect_terminal_policy_with_numeric_guard", "", "", "script_value_effect_terminal_policy", "terminal ScriptValue effect with numeric guard"
    if script and effects:
        return "script_value_effect_terminal_policy_with_effect_guard", "", "", "script_value_effect_terminal_policy", "terminal ScriptValue effect with effect guard"
    if script and "DomainGuard" in guards:
        return "script_value_effect_terminal_policy_with_domain_guard", "", "", "script_value_effect_terminal_policy", "terminal ScriptValue effect with domain guard"
    if script:
        return "script_value_effect_terminal_policy", "", "", "script_value_effect_terminal_policy", "plain terminal ScriptValue effect"
    return "script_value_effect_blocked_uncertain", "", "", "human_review_or_evidence_collection", "insufficient ScriptValue evidence"


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
        "policy_id": "script_value_effect_policy",
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "entry_conditions": [
            "route == script_value_effect_policy",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
        ],
        "reused_registered_policies": [{"agent_key": key, "sampled": count} for key, count in reused_policies.most_common()],
        "reused_catalog_specs": [{"policy_id": key, "sampled": count} for key, count in reused_specs.most_common()],
        "script_value_effect_types": [{"type": key, "sampled": count} for key, count in type_counts.most_common()],
        "resolution_order": [
            "state guard",
            "visible residual guard",
            "artifact/activity and building/modifier reuse",
            "effect-list ScriptValue/concept reuse",
            "accolade/trait reuse",
            "generic ScriptValue requirement reuse",
            "numeric/percent terminal or subpolicy",
            "domain/event/dynamic fallback",
        ],
        "next_components": next_components,
        "blocked_conditions": [
            "state guard failed",
            "visible residual",
            "missing ScriptValue marker",
            "ambiguous dynamic structure",
        ],
        "promotion_gate": "read_only_component_only_no_apply_no_lifecycle",
        "observed_decision_counts": dict(decision_counts),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only ScriptValue effect policy review.")
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
    script_counts: Counter[str] = Counter()
    numeric_counts: Counter[str] = Counter()
    effect_counts: Counter[str] = Counter()
    effect_list_counts: Counter[str] = Counter()
    artifact_activity_counts: Counter[str] = Counter()
    building_modifier_counts: Counter[str] = Counter()
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
        script_value_markers = detect(SCRIPT_VALUE_MARKERS, blob)
        numeric_markers = detect(NUMERIC_MARKERS, blob)
        effect_markers = detect(EFFECT_MARKERS, blob)
        effect_list_markers = detect(EFFECT_LIST_MARKERS, blob)
        artifact_activity_markers = detect(ARTIFACT_ACTIVITY_MARKERS, blob)
        building_modifier_markers = detect(BUILDING_MODIFIER_MARKERS, blob)
        guard_markers = detect(GUARD_MARKERS, blob)
        secondary_markers = detect(SECONDARY_MARKERS, blob)
        decision, matched_registered_policy, matched_catalog_spec, next_component, rationale = classify(
            state=record["state"],
            registered=registered,
            specs=specs,
            script_value_markers=script_value_markers,
            numeric_markers=numeric_markers,
            effect_markers=effect_markers,
            effect_list_markers=effect_list_markers,
            artifact_activity_markers=artifact_activity_markers,
            building_modifier_markers=building_modifier_markers,
            guard_markers=guard_markers,
            secondary_markers=secondary_markers,
        )
        family_counts.update(record["families_open"])
        script_counts.update(script_value_markers)
        numeric_counts.update(numeric_markers)
        effect_counts.update(effect_markers)
        effect_list_counts.update(effect_list_markers)
        artifact_activity_counts.update(artifact_activity_markers)
        building_modifier_counts.update(building_modifier_markers)
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
                "script_value_markers": script_value_markers,
                "numeric_markers": numeric_markers,
                "effect_markers": effect_markers,
                "effect_list_markers": effect_list_markers,
                "artifact_activity_markers": artifact_activity_markers,
                "building_modifier_markers": building_modifier_markers,
                "matched_registered_policy": matched_registered_policy,
                "matched_catalog_spec": matched_catalog_spec,
                "guard_markers": guard_markers,
                "secondary_markers": secondary_markers,
                "script_value_effect_decision": decision,
                "next_component": next_component,
                "requires_lifecycle_later": False,
                "requires_apply_later": False,
                "corrected_text": "",
                "rationale": rationale,
            }
        )

    reuse_total = sum(1 for row in results if row["matched_registered_policy"] or row["matched_catalog_spec"])
    terminal_total = sum(count for decision, count in decision_counts.items() if decision.startswith("script_value_effect_terminal"))
    dominant_decision, dominant_count = decision_counts.most_common(1)[0] if decision_counts else ("", 0)
    if dominant_decision.startswith("needs_") and dominant_count >= 40:
        next_prompt = f"chat_exec_{dominant_decision.removeprefix('needs_')}_review_prompt.md"
        recommendation = "open_narrow_subpolicy"
    elif reuse_total >= 70:
        next_prompt = "chat_exec_script_value_effect_policy_catalog_registration_prompt.md"
        recommendation = "register_readonly_reuse_splitter"
    elif terminal_total >= 70:
        next_prompt = "chat_exec_script_value_effect_terminal_spec_registration_prompt.md"
        recommendation = "register_terminal_readonly"
    else:
        next_prompt = "chat_exec_holy_site_effect_name_policy_review_prompt.md"
        recommendation = "fragmented_move_to_next_large_route"

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
        "policy_shape": "reuse_splitter_route" if reuse_total >= 70 else ("terminal_policy" if terminal_total >= 70 else "splitter_candidate"),
    }
    spec = build_spec(
        decision_counts=decision_counts,
        reused_policies=reused_policy_counts,
        reused_specs=reused_spec_counts,
        type_counts=script_counts + numeric_counts + effect_counts + effect_list_counts + artifact_activity_counts + building_modifier_counts + secondary_counts,
        next_components=[next_prompt],
    )

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
        for decision, count in decision_counts.most_common():
            handle.write(json.dumps({"record_type": "decision_count", "script_value_effect_decision": decision, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for section_name, counts in [
            ("top_family", family_counts),
            ("top_script_value_marker", script_counts),
            ("top_numeric_marker", numeric_counts),
            ("top_effect_marker", effect_counts),
            ("top_effect_list_marker", effect_list_counts),
            ("top_artifact_activity_marker", artifact_activity_counts),
            ("top_building_modifier_marker", building_modifier_counts),
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
        handle.write("ScriptValue effect policy review\n\n")
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
            ("Top ScriptValue markers", script_counts),
            ("Top numeric markers", numeric_counts),
            ("Top effect/effect-list markers", effect_counts + effect_list_counts),
            ("Top artifact/activity markers", artifact_activity_counts),
            ("Top building/modifier markers", building_modifier_counts),
            ("Top guard markers", guard_counts),
            ("Top secondary markers", secondary_counts),
        ]:
            handle.write(f"\n{title}\n")
            for key, count in counts.most_common(15):
                handle.write(f"- {key}: {count}\n")
        handle.write("\nRespostas\n")
        handle.write("- script_value_effect_policy deve virar componente read-only real se reuso >= 70 ou sublane dominante persistir.\n")
        handle.write("- lifecycle/apply em curto prazo: nao.\n")
        handle.write("- reaproveitamento medido contra ScriptValue requirement, effect-list ScriptValue, artifact/activity, building/modifier, concept e accolade/trait.\n")
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
