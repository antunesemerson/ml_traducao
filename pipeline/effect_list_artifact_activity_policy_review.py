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


SOURCE_DECISION = "needs_effect_list_artifact_activity_policy"
PRIMARY_ROUTE = "effect_list_multiline_policy"
LEDGER_RUN_ID = 76

ALLOWED_DECISIONS = {
    "artifact_activity_terminal_policy",
    "artifact_activity_terminal_policy_with_event_guard",
    "artifact_activity_terminal_policy_with_domain_guard",
    "needs_artifact_item_effect_policy",
    "needs_activity_effect_policy",
    "needs_travel_effect_policy",
    "needs_legend_lore_effect_policy",
    "needs_reward_tooltip_effect_policy",
    "needs_artifact_activity_scope_getter_policy",
    "needs_artifact_activity_script_value_policy",
    "needs_artifact_activity_gender_local_player_policy",
    "needs_artifact_activity_event_context",
    "needs_artifact_activity_domain_context",
    "needs_artifact_activity_residual_repair",
    "needs_artifact_activity_dynamic_parser_escape",
    "artifact_activity_blocked_uncertain",
}

ARTIFACT_MARKERS = [
    ("Artifact", re.compile(r"artifact|relic|inventory|court_artifact|stealing_back_artifact", re.I)),
    ("Item", re.compile(r"\bitem\b|weapon|armor|book|trinket|regalia", re.I)),
]

ACTIVITY_MARKERS = [
    ("Activity", re.compile(r"activity|activities/", re.I)),
    ("Tournament", re.compile(r"tournament|contest|ep2/tournament", re.I)),
    ("Hunt", re.compile(r"hunt|hunter", re.I)),
    ("FeastFuneralFestival", re.compile(r"feast|funeral|festival|gruesome_festival", re.I)),
    ("CoronationEducationSurvey", re.compile(r"coronation|education|survey|imperial_examination", re.I)),
]

EFFECT_LIST_MARKERS = [
    ("Tooltip", re.compile(r"tooltip|_tt\b|\.tt\b", re.I)),
    ("Desc", re.compile(r"\.desc|desc_|_desc|description", re.I)),
    ("OptionReward", re.compile(r"reward|option|gain|loss|opinion|modifier|special_type_bar_segment", re.I)),
    ("MultilineSourceRoute", re.compile(r"effect_list_multiline_policy", re.I)),
]

GUARD_MARKERS = [
    ("ScopeGetterGuard", re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|Get[A-Za-z0-9_]+\(", re.I)),
    ("EventGuard", re.compile(r"event|events|\.desc|desc_|option|flavor|spectator|story", re.I)),
    ("DomainGuard", re.compile(r"coronation|church|legend|succession|mapmaking|library|festival|funeral|education|tournament|hunt", re.I)),
]

SECONDARY_MARKERS = [
    ("Travel", re.compile(r"travel|journey|tour|wanderer|adventurer", re.I)),
    ("LegendLore", re.compile(r"legend|lore|story|chronicle|library|carmina|recital", re.I)),
    ("RewardTooltip", re.compile(r"reward|tooltip|_tt\b|\.tt\b|gain|loss|opinion|modifier|special_type_bar_segment", re.I)),
    ("ScopeGetter", re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|Get[A-Za-z0-9_]+\(", re.I)),
    ("ScriptValue", re.compile(r"ScriptValue|GetScriptValue|\|V[0-9]?|\|=\+?0|[0-9]+%", re.I)),
    ("GenderLocalPlayer", re.compile(r"Select_CString|gender_token|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)|local_player|GetPlayer|\bvoc(?:ê|e)\b|\bseu\b|\bsua\b", re.I)),
    ("Event", re.compile(r"event|events|\.desc|desc_|option|flavor|spectator", re.I)),
    ("Domain", re.compile(r"coronation|church|legend|succession|mapmaking|library|festival|funeral|education|tournament|hunt|survey", re.I)),
    ("ResidualVisible", re.compile(r"Ãƒ|Ã‚|Â¿|Â¡|â€™|â€œ|â€�|�|\bthe\b|\byour\b|\byou\b|\btheir\b|\bcannot\b", re.I)),
    ("DynamicToken", re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$|dynamic_ck3_expression", re.I)),
]


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
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
        and row.get("effect_list_multiline_decision") == SOURCE_DECISION
    ]
    rows.sort(key=lambda row: (str(row.get("relative_path") or ""), str(row.get("source_key") or ""), int(row["segment_id"])))
    seen = set()
    for row in rows:
        segment_id = int(row["segment_id"])
        if segment_id in seen:
            raise SystemExit(f"duplicate source segment_id: {segment_id}")
        seen.add(segment_id)
    return rows


def fetch_states(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
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


def detect(patterns: list[tuple[str, re.Pattern[str]]], blob: str) -> list[str]:
    return [name for name, pattern in patterns if pattern.search(blob)]


def classify(
    state: dict[str, Any] | None,
    artifact_markers: list[str],
    activity_markers: list[str],
    effect_markers: list[str],
    guard_markers: list[str],
    secondary_markers: list[str],
) -> tuple[str, str, str]:
    artifact = set(artifact_markers)
    activity = set(activity_markers)
    effect = set(effect_markers)
    guards = set(guard_markers)
    secondary = set(secondary_markers)
    if not state or state["state_group"] != "pending" or int(state["is_closed"] or 0) != 0:
        return "artifact_activity_blocked_uncertain", "state_guard", "segment is not pending in selected state run"
    if int(state["needs_output_apply"] or 0) != 0 or int(state["confirmed_matches_output"] or 0) != 1:
        return "artifact_activity_blocked_uncertain", "state_guard", "state guard failed: needs_output_apply or confirmed_matches_output"
    if "ResidualVisible" in secondary:
        return "needs_artifact_activity_residual_repair", "residual_dependency_filtered_repair", "visible residual remains"
    if "ScriptValue" in secondary:
        return "needs_artifact_activity_script_value_policy", "artifact_activity_script_value_policy", "ScriptValue/numeric dependency dominates"
    if "GenderLocalPlayer" in secondary:
        return "needs_artifact_activity_gender_local_player_policy", "artifact_activity_gender_local_player_policy", "gender/local-player dependency dominates"
    if artifact:
        return "needs_artifact_item_effect_policy", "artifact_item_effect_policy", "artifact/item/inventory surface dominates"
    if "Travel" in secondary:
        return "needs_travel_effect_policy", "travel_effect_policy", "travel/journey/tour surface dominates"
    if "LegendLore" in secondary:
        return "needs_legend_lore_effect_policy", "legend_lore_effect_policy", "legend/lore/story surface dominates"
    if "RewardTooltip" in secondary or "OptionReward" in effect:
        return "needs_reward_tooltip_effect_policy", "reward_tooltip_effect_policy", "reward/tooltip/gain/loss surface dominates"
    if activity:
        return "needs_activity_effect_policy", "activity_effect_policy", "activity/tournament/hunt/festival surface dominates"
    if "ScopeGetter" in secondary or "ScopeGetterGuard" in guards:
        return "needs_artifact_activity_scope_getter_policy", "artifact_activity_scope_getter_policy", "scope/getter dependency dominates"
    if "Event" in secondary or "EventGuard" in guards:
        return "artifact_activity_terminal_policy_with_event_guard", "terminal_router_policy", "artifact/activity effect-list with event guard"
    if "Domain" in secondary or "DomainGuard" in guards:
        return "artifact_activity_terminal_policy_with_domain_guard", "terminal_router_policy", "artifact/activity effect-list with domain guard"
    if "DynamicToken" in secondary:
        return "needs_artifact_activity_dynamic_parser_escape", "ck3_dynamic_symbolic_parser", "dynamic parser escape remains"
    if effect:
        return "artifact_activity_terminal_policy", "terminal_router_policy", "plain artifact/activity effect-list terminal surface"
    return "artifact_activity_blocked_uncertain", "human_review_or_evidence_collection", "insufficient artifact/activity evidence"


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_effect_list_artifact_activity_policy_review"
    spec = reports_dir / f"{stamp}_effect_list_artifact_activity_policy_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only effect-list artifact/activity policy review.")
    parser.add_argument("--effect-list-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()

    rows = source_rows(args.effect_list_jsonl)
    conn = connect_readonly()
    states = fetch_states(conn, args.segment_state_run_id, [int(row["segment_id"]) for row in rows])

    results = []
    decision_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    artifact_counts: Counter[str] = Counter()
    activity_counts: Counter[str] = Counter()
    effect_counts: Counter[str] = Counter()
    guard_counts: Counter[str] = Counter()
    secondary_counts: Counter[str] = Counter()
    pending_count = 0

    for row in rows:
        segment_id = int(row["segment_id"])
        state = states.get(segment_id)
        if state and state["state_group"] == "pending" and int(state["is_closed"] or 0) == 0:
            pending_count += 1
        blob = " ".join(
            str(row.get(key) or "")
            for key in ("relative_path", "source_key", "old_text", "confirmed_text", "output_text")
        )
        blob += " " + " ".join(row.get("families_open") or [])
        blob += " " + " ".join(row.get("secondary_markers") or [])
        artifact_markers = detect(ARTIFACT_MARKERS, blob)
        activity_markers = detect(ACTIVITY_MARKERS, blob)
        effect_markers = detect(EFFECT_LIST_MARKERS, blob)
        guard_markers = detect(GUARD_MARKERS, blob)
        secondary_markers = sorted(set(row.get("secondary_markers") or []) | set(detect(SECONDARY_MARKERS, blob)))
        decision, next_component, rationale = classify(
            state,
            artifact_markers,
            activity_markers,
            effect_markers,
            guard_markers,
            secondary_markers,
        )
        if decision not in ALLOWED_DECISIONS:
            raise SystemExit(f"unknown decision {decision} for segment_id {segment_id}")
        decision_counts[decision] += 1
        family_counts.update(row.get("families_open") or ["NoOpenFamily"])
        artifact_counts.update(artifact_markers or ["NoArtifactMarker"])
        activity_counts.update(activity_markers or ["NoActivityMarker"])
        effect_counts.update(effect_markers or ["NoEffectListMarker"])
        guard_counts.update(guard_markers or ["NoGuardMarker"])
        secondary_counts.update(secondary_markers or ["NoSecondaryMarker"])
        results.append(
            {
                "record_type": "sample_review",
                "segment_id": segment_id,
                "relative_path": str(row.get("relative_path") or ""),
                "source_key": str(row.get("source_key") or ""),
                "families_open": list(row.get("families_open") or []),
                "source_decision": SOURCE_DECISION,
                "primary_route": PRIMARY_ROUTE,
                "old_text": str(row.get("old_text") or ""),
                "confirmed_text": str(row.get("confirmed_text") or ""),
                "output_text": str(row.get("output_text") or ""),
                "artifact_markers": artifact_markers,
                "activity_markers": activity_markers,
                "effect_list_markers": effect_markers,
                "guard_markers": guard_markers,
                "secondary_markers": secondary_markers,
                "artifact_activity_decision": decision,
                "next_component": next_component,
                "requires_lifecycle_later": False,
                "requires_apply_later": False,
                "corrected_text": "",
                "rationale": rationale,
            }
        )

    terminal_count = sum(count for decision, count in decision_counts.items() if decision.startswith("artifact_activity_terminal_policy"))
    dominant, dominant_count = decision_counts.most_common(1)[0] if decision_counts else ("none", 0)
    if dominant.startswith("needs_") and dominant_count >= 20:
        next_prompt = f"chat_exec_{dominant.removeprefix('needs_')}_review_prompt.md"
        stop_rule = f"continue_narrow_prompt: {dominant} reached {dominant_count} >= 20"
    elif terminal_count > len(results) / 2:
        next_prompt = "chat_exec_effect_list_artifact_activity_terminal_spec_prompt.md"
        stop_rule = "terminal_majority_prepare_readonly_spec_registration"
    else:
        next_prompt = "chat_exec_effect_list_gender_local_player_policy_review_prompt.md"
        stop_rule = "fragmented_return_to_gender_local_player"

    spec = {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": "effect_list_multiline_policy",
        "policy_id": "effect_list_artifact_activity_policy",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": LEDGER_RUN_ID,
        "entry_conditions": [
            "effect_list_multiline_decision == needs_effect_list_artifact_activity_policy",
            "primary_route == effect_list_multiline_policy",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
        ],
        "artifact_activity_types": [{"decision": decision, "sampled": count} for decision, count in decision_counts.most_common()],
        "resolution_order": [
            "state guard",
            "residual guard",
            "ScriptValue and gender/local-player",
            "artifact/item",
            "travel",
            "legend/lore",
            "reward tooltip",
            "activity",
            "scope/getter",
            "event/domain terminal guards",
        ],
        "next_components": [
            "artifact_item_effect_policy",
            "activity_effect_policy",
            "travel_effect_policy",
            "legend_lore_effect_policy",
            "reward_tooltip_effect_policy",
            "effect_list_gender_local_player_policy",
        ],
        "blocked_conditions": ["state guard failed", "visible residual", "ambiguous artifact/activity effect surface"],
        "promotion_gate": "read_only_component_only_no_apply_no_lifecycle",
        "sampled": len(results),
        "terminal_policy_majority": terminal_count > len(results) / 2,
        "stop_rule": stop_rule,
    }

    txt_path, jsonl_path, spec_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({
            "record_type": "summary",
            "source_decision": SOURCE_DECISION,
            "primary_route": PRIMARY_ROUTE,
            "total_reviewed": len(results),
            "pending_count": pending_count,
            "decision_counts": dict(decision_counts),
            "family_counts": dict(family_counts),
            "artifact_marker_counts": dict(artifact_counts),
            "activity_marker_counts": dict(activity_counts),
            "effect_list_marker_counts": dict(effect_counts),
            "guard_marker_counts": dict(guard_counts),
            "secondary_marker_counts": dict(secondary_counts),
            "ready_lifecycle_future": 0,
            "apply_candidates_future": 0,
            "dominant_subtype": dominant,
            "dominant_count": dominant_count,
            "terminal_policy_majority": terminal_count > len(results) / 2,
            "stop_rule": stop_rule,
            "next_prompt": next_prompt,
            "requires_lifecycle_later": False,
            "requires_apply_later": False,
        }, ensure_ascii=False, sort_keys=True) + "\n")
        for decision, count in decision_counts.most_common():
            handle.write(json.dumps({"record_type": "decision_count", "artifact_activity_decision": decision, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for family, count in family_counts.most_common():
            handle.write(json.dumps({"record_type": "family_count", "family": family, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in artifact_counts.most_common():
            handle.write(json.dumps({"record_type": "artifact_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in activity_counts.most_common():
            handle.write(json.dumps({"record_type": "activity_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in effect_counts.most_common():
            handle.write(json.dumps({"record_type": "effect_list_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in guard_counts.most_common():
            handle.write(json.dumps({"record_type": "guard_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in secondary_counts.most_common():
            handle.write(json.dumps({"record_type": "secondary_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for priority, (prompt, rationale) in enumerate([
            (next_prompt, "selected by artifact/activity stop rule"),
            ("chat_exec_effect_list_gender_local_player_policy_review_prompt.md", "return to next effect-list branch if artifact/activity fragments"),
            ("chat_exec_global_post_architecture_diagnostic_prompt.md", "global check after registered router expansion"),
        ], 1):
            handle.write(json.dumps({"record_type": "strategy", "priority": priority, "next_prompt": prompt, "rationale": rationale}, ensure_ascii=False, sort_keys=True) + "\n")
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")

    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Effect-list artifact/activity policy review\n\n")
        handle.write(f"- total revisado: {len(results)}\n")
        handle.write("- ready_lifecycle_future: 0\n")
        handle.write("- apply_candidates_future: 0\n")
        handle.write(f"- subtipo dominante: {dominant}\n")
        handle.write(f"- dominant_count: {dominant_count}\n")
        handle.write(f"- regra_de_parada: {stop_rule}\n")
        handle.write(f"- proximo_prompt: {next_prompt}\n\n")
        handle.write("artifact_activity_decision_counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nTop families_open:\n")
        for family, count in family_counts.most_common(15):
            handle.write(f"- {family}: {count}\n")
        handle.write("\nTop artifact markers:\n")
        for marker, count in artifact_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop activity markers:\n")
        for marker, count in activity_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop effect-list markers:\n")
        for marker, count in effect_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop guard markers:\n")
        for marker, count in guard_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop secondary markers:\n")
        for marker, count in secondary_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nAnalise\n")
        handle.write("- effect_list_artifact_activity_policy deve virar componente read-only real.\n")
        handle.write("- Esta revisao nao gera lifecycle/apply.\n")
        handle.write("- Registro futuro como policy de effect_list_multiline_policy depende da proxima subpolicy dominante.\n")

    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"total_reviewed: {len(results)}")
    print("ready_lifecycle_future: 0")
    print("apply_candidates_future: 0")
    print(f"stop_rule: {stop_rule}")
    print("decision_counts:")
    for decision, count in decision_counts.most_common():
        print(f"  {decision}: {count}")


if __name__ == "__main__":
    main()
