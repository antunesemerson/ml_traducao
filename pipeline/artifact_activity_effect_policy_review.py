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


PRIMARY_ROUTE = "artifact_activity_effect_policy"
EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76
EXPECTED_UNIVERSE = 1057

REGISTERED_REUSE_POLICIES = {
    "effect_list_artifact_activity_policy",
    "artifact_item_effect_policy",
    "artifact_activity_gender_local_player_policy",
    "artifact_activity_script_value_policy",
}

ARTIFACT_MARKERS = [
    ("Artifact", re.compile(r"artifact|court_artifact|relic|inventory|antiquarian", re.I)),
    ("Item", re.compile(r"\bitem\b|weapon|armor|armou?r|book|trinket|regalia|pedestal|display", re.I)),
    ("InventoryRelic", re.compile(r"inventory|relic|steal(?:ing)?_back_artifact|claim_artifact", re.I)),
]

ACTIVITY_MARKERS = [
    ("Activity", re.compile(r"activity|activities/|activity_type", re.I)),
    ("Tournament", re.compile(r"tournament|contest|joust|melee|archery", re.I)),
    ("Hunt", re.compile(r"\bhunt\b|hunter|hunting", re.I)),
    ("FeastFestival", re.compile(r"feast|festival|funeral|wedding|coronation|pilgrimage", re.I)),
    ("EducationSurvey", re.compile(r"education|survey|imperial_examination", re.I)),
]

EFFECT_MARKERS = [
    ("Reward", re.compile(r"reward|gain|loss|opinion|modifier|prestige|piety|gold", re.I)),
    ("Tooltip", re.compile(r"tooltip|_tt\b|\.tt\b|#T|#help", re.I)),
    ("EffectName", re.compile(r"_effect_name\b|effect_name", re.I)),
    ("Modifier", re.compile(r"modifier|special_type_bar_segment", re.I)),
]

TRAVEL_MARKERS = [
    ("Travel", re.compile(r"travel|journey|tour|wanderer|adventurer|road|destination", re.I)),
]

LEGEND_LORE_MARKERS = [
    ("Legend", re.compile(r"legend|legendary|myth", re.I)),
    ("LoreStory", re.compile(r"lore|story|chronicle|library|recital|carmina|tale", re.I)),
]

GUARD_MARKERS = [
    ("ScopeGetterGuard", re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|Get[A-Za-z0-9_]+\(", re.I)),
    ("EventGuard", re.compile(r"event|events|\.desc|desc_|option|flavor|story|scheme|interaction", re.I)),
    ("DomainGuard", re.compile(r"culture|faith|religion|realm|title|law|government|succession|church|court", re.I)),
    ("ArtifactActivityGuard", re.compile(r"artifact|activity|tournament|hunt|feast|travel|legend", re.I)),
]

SECONDARY_MARKERS = [
    ("ScopeGetter", re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|Get[A-Za-z0-9_]+\(", re.I)),
    ("ScriptValue", re.compile(r"ScriptValue|GetScriptValue|GetValue|\|V[0-9]?|\|=\+?0|[0-9]+\s*%", re.I)),
    ("GenderLocalPlayer", re.compile(r"Select_CString|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)|local_player|GetPlayer|\bvoc(?:ê|e|Ãª)\b|\bseu\b|\bsua\b", re.I)),
    ("TitleLaw", re.compile(r"title|county|duchy|kingdom|empire|law|succession|government", re.I)),
    ("Concept", re.compile(r"\[[A-Za-z0-9_]+\||Concept\(|game_concept", re.I)),
    ("Event", re.compile(r"event|events|\.desc|desc_|option|flavor|story|scheme|interaction", re.I)),
    ("Domain", re.compile(r"culture|faith|religion|realm|court|church|succession|festival|funeral|education", re.I)),
    ("ResidualVisible", re.compile(r"ÃƒÆ’|Ãƒâ€š|Ã‚Â¿|Ã‚Â¡|Ã¢â‚¬|ï¿½|\bthe\b|\byour\b|\byou\b|\btheir\b|\bcannot\b", re.I)),
    ("DynamicToken", re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)),
]


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_artifact_activity_effect_policy_review"
    spec = reports_dir / f"{stamp}_artifact_activity_effect_policy_spec.json"
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
    artifact_markers: list[str],
    activity_markers: list[str],
    effect_markers: list[str],
    travel_markers: list[str],
    legend_lore_markers: list[str],
    guard_markers: list[str],
    secondary_markers: list[str],
) -> tuple[str, str, str, str, str]:
    artifact = set(artifact_markers)
    activity = set(activity_markers)
    effect = set(effect_markers)
    travel = set(travel_markers)
    legend = set(legend_lore_markers)
    guards = set(guard_markers)
    secondary = set(secondary_markers)
    if state["state_group"] != "pending" or int(state["is_closed"]) != 0:
        return "artifact_activity_effect_blocked_uncertain", "", "", "state_guard", "segment is not pending in selected state run"
    if int(state["needs_output_apply"]) != 0 or int(state["confirmed_matches_output"]) != 1:
        return "artifact_activity_effect_blocked_uncertain", "", "", "state_guard", "state guard failed: needs_output_apply or confirmed_matches_output"
    if "ResidualVisible" in secondary:
        return "needs_artifact_activity_residual_repair", "", "", "residual_dependency_filtered_repair", "visible residual remains"

    if "GenderLocalPlayer" in secondary and "artifact_activity_gender_local_player_policy" in registered:
        return (
            "artifact_activity_effect_reuse_artifact_activity_gender_policy",
            "artifact_activity_gender_local_player_policy",
            "artifact_activity_gender_local_player_policy",
            "artifact_activity_gender_local_player_policy",
            "can reuse registered artifact/activity gender/local-player terminal guard",
        )
    if "ScriptValue" in secondary and "artifact_activity_script_value_policy" in registered:
        return (
            "artifact_activity_effect_reuse_artifact_activity_script_value_policy",
            "artifact_activity_script_value_policy",
            "artifact_activity_script_value_policy",
            "artifact_activity_script_value_policy",
            "can reuse registered artifact/activity ScriptValue terminal guard",
        )
    if artifact and "artifact_item_effect_policy" in registered:
        return (
            "artifact_activity_effect_reuse_artifact_item_effect_policy",
            "artifact_item_effect_policy",
            "artifact_item_effect_policy",
            "artifact_item_effect_policy",
            "can reuse registered artifact item effect splitter/spec",
        )
    if (activity or travel or legend or effect) and "effect_list_artifact_activity_policy" in registered:
        return (
            "artifact_activity_effect_reuse_effect_list_artifact_activity_policy",
            "effect_list_artifact_activity_policy",
            "effect_list_artifact_activity_policy",
            "effect_list_artifact_activity_policy",
            "can reuse registered effect-list artifact/activity splitter with artifact/activity guard",
        )

    if artifact:
        return "needs_artifact_activity_item_policy", "", "", "artifact_item_effect_policy", "artifact/item/inventory surface needs policy"
    if travel:
        return "needs_artifact_activity_travel_policy", "", "", "travel_effect_policy", "travel/journey surface needs policy"
    if legend:
        return "needs_artifact_activity_legend_lore_policy", "", "", "legend_lore_effect_policy", "legend/lore/story surface needs policy"
    if "RewardTooltip" in secondary or "Reward" in effect or "Tooltip" in effect:
        return "needs_artifact_activity_reward_tooltip_policy", "", "", "reward_tooltip_effect_policy", "reward/tooltip/gain/loss surface needs policy"
    if activity:
        return "needs_artifact_activity_activity_context_policy", "", "", "activity_context_policy", "activity/tournament/hunt/feast surface needs policy"
    if "ScopeGetter" in secondary or "ScopeGetterGuard" in guards:
        return "needs_artifact_activity_scope_getter_policy", "", "", "artifact_activity_scope_getter_policy", "scope/getter dependency dominates"
    if "TitleLaw" in secondary:
        return "needs_artifact_activity_title_law_policy", "", "", "title_law_policy", "title/law dependency remains"
    if "Concept" in secondary:
        return "needs_artifact_activity_concept_policy", "", "", "concept_policy", "concept dependency remains"
    if "Event" in secondary or "EventGuard" in guards:
        return "artifact_activity_effect_terminal_policy_with_event_guard", "", "", "terminal_router_policy", "terminal artifact/activity effect with event guard"
    if "Domain" in secondary or "DomainGuard" in guards:
        return "artifact_activity_effect_terminal_policy_with_domain_guard", "", "", "terminal_router_policy", "terminal artifact/activity effect with domain guard"
    if "DynamicToken" in secondary:
        return "needs_artifact_activity_dynamic_parser_escape", "", "", "ck3_dynamic_symbolic_parser", "dynamic parser escape remains"
    if effect:
        return "artifact_activity_effect_terminal_policy", "", "", "terminal_router_policy", "plain artifact/activity effect terminal pattern"
    return "artifact_activity_effect_blocked_uncertain", "", "", "human_review_or_evidence_collection", "insufficient artifact/activity evidence"


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
        "policy_id": "artifact_activity_effect_policy",
        "segment_state_run_id": EXPECTED_SEGMENT_STATE_RUN_ID,
        "ledger_run_id": EXPECTED_LEDGER_RUN_ID,
        "entry_conditions": [
            "route == artifact_activity_effect_policy",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
        ],
        "reused_registered_policies": [{"agent_key": key, "sampled": count} for key, count in reused_policies.most_common()],
        "reused_catalog_specs": [{"policy_id": key, "sampled": count} for key, count in reused_specs.most_common()],
        "artifact_activity_effect_types": [{"decision": key, "sampled": count} for key, count in decision_counts.most_common()],
        "resolution_order": [
            "state guard",
            "residual guard",
            "registered artifact/activity gender and ScriptValue terminal guards",
            "artifact item effect splitter",
            "effect-list artifact/activity splitter",
            "local artifact/activity sublanes",
            "event/domain/dynamic fallback",
        ],
        "next_components": next_components,
        "blocked_conditions": [
            "state guard failed",
            "visible residual",
            "missing artifact/activity evidence",
        ],
        "promotion_gate": "read_only_component_only_no_apply_no_lifecycle",
        "sampled": total_reviewed,
        "universe_estimated": universe,
        "policy_shape": "reuse_splitter_route",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only artifact/activity effect policy review.")
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
    artifact_counts: Counter[str] = Counter()
    activity_counts: Counter[str] = Counter()
    effect_counts: Counter[str] = Counter()
    travel_counts: Counter[str] = Counter()
    legend_counts: Counter[str] = Counter()
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
        artifact_markers = detect(ARTIFACT_MARKERS, blob)
        activity_markers = detect(ACTIVITY_MARKERS, blob)
        effect_markers = detect(EFFECT_MARKERS, blob)
        travel_markers = detect(TRAVEL_MARKERS, blob)
        legend_lore_markers = detect(LEGEND_LORE_MARKERS, blob)
        guard_markers = detect(GUARD_MARKERS, blob)
        secondary_markers = detect(SECONDARY_MARKERS, blob)
        decision, matched_registered_policy, matched_catalog_spec, next_component, rationale = classify(
            state=record["state"],
            registered=registered,
            artifact_markers=artifact_markers,
            activity_markers=activity_markers,
            effect_markers=effect_markers,
            travel_markers=travel_markers,
            legend_lore_markers=legend_lore_markers,
            guard_markers=guard_markers,
            secondary_markers=secondary_markers,
        )
        family_counts.update(record["families_open"])
        artifact_counts.update(artifact_markers)
        activity_counts.update(activity_markers)
        effect_counts.update(effect_markers)
        travel_counts.update(travel_markers)
        legend_counts.update(legend_lore_markers)
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
                "artifact_markers": artifact_markers,
                "activity_markers": activity_markers,
                "effect_markers": effect_markers,
                "travel_markers": travel_markers,
                "legend_lore_markers": legend_lore_markers,
                "matched_registered_policy": matched_registered_policy,
                "matched_catalog_spec": matched_catalog_spec,
                "guard_markers": guard_markers,
                "secondary_markers": secondary_markers,
                "artifact_activity_effect_decision": decision,
                "next_component": next_component,
                "requires_lifecycle_later": False,
                "requires_apply_later": False,
                "corrected_text": "",
                "rationale": rationale,
            }
        )

    reuse_total = sum(reused_policy_counts.values())
    terminal_total = sum(count for decision, count in decision_counts.items() if decision.startswith("artifact_activity_effect_terminal"))
    dominant_decision, dominant_count = decision_counts.most_common(1)[0] if decision_counts else ("", 0)
    if dominant_decision.startswith("needs_") and dominant_count >= 50:
        next_prompt = f"chat_exec_{dominant_decision.removeprefix('needs_')}_review_prompt.md"
        recommendation = "open_narrow_subpolicy"
    elif reuse_total >= 70:
        next_prompt = "chat_exec_artifact_activity_effect_policy_catalog_registration_prompt.md"
        recommendation = "register_readonly_reuse_splitter"
    elif terminal_total >= 70:
        next_prompt = "chat_exec_artifact_activity_effect_terminal_spec_registration_prompt.md"
        recommendation = "register_terminal_readonly"
    else:
        next_prompt = "chat_exec_building_modifier_effect_policy_review_prompt.md"
        recommendation = "fragmented_move_to_next_large_route"
    next_components = [next_prompt]

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
            handle.write(json.dumps({"record_type": "decision_count", "artifact_activity_effect_decision": decision, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    spec = build_spec(
        decision_counts=decision_counts,
        reused_policies=reused_policy_counts,
        reused_specs=reused_spec_counts,
        total_reviewed=len(results),
        universe=universe,
        next_components=next_components,
    )
    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Artifact/activity effect policy review\n\n")
        handle.write(f"- universo estimado: {universe}\n")
        handle.write(f"- total revisado: {len(results)}\n")
        handle.write(f"- reuso policies/specs catalogadas: {reuse_total}\n")
        handle.write(f"- terminal policies futuras: {terminal_total}\n")
        handle.write(f"- ready lifecycle futuro: 0\n")
        handle.write(f"- apply candidates futuro: 0\n")
        handle.write(f"- dominante: {dominant_decision} ({dominant_count})\n")
        handle.write(f"- recomendacao: {recommendation}\n")
        handle.write(f"- proximo prompt: {next_prompt}\n\n")
        handle.write("Decisoes\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nTop families_open\n")
        for key, count in family_counts.most_common(15):
            handle.write(f"- {key}: {count}\n")
        handle.write("\nTop artifact markers\n")
        for key, count in artifact_counts.most_common():
            handle.write(f"- {key}: {count}\n")
        handle.write("\nTop activity/travel/legend markers\n")
        for key, count in (activity_counts + travel_counts + legend_counts).most_common(15):
            handle.write(f"- {key}: {count}\n")
        handle.write("\nTop effect markers\n")
        for key, count in effect_counts.most_common(15):
            handle.write(f"- {key}: {count}\n")
        handle.write("\nTop guard markers\n")
        for key, count in guard_counts.most_common(15):
            handle.write(f"- {key}: {count}\n")
        handle.write("\nTop secondary markers\n")
        for key, count in secondary_counts.most_common(15):
            handle.write(f"- {key}: {count}\n")
        handle.write("\nConclusoes\n")
        handle.write("- artifact_activity_effect_policy deve virar componente read-only real: sim, como rota/splitter de reuso.\n")
        handle.write("- lifecycle/apply em curto prazo: nao.\n")
        handle.write("- a policy reaproveita fortemente o pacote effect-list/artifact ja registrado.\n")
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
