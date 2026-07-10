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


SOURCE_DECISION = "needs_artifact_activity_script_value_policy"
PRIMARY_ROUTE = "effect_list_multiline_policy"
PARENT_POLICY = "effect_list_artifact_activity_policy"
LEDGER_RUN_ID = 76

ALLOWED_DECISIONS = {
    "artifact_script_value_terminal_policy",
    "artifact_script_value_terminal_policy_with_numeric_guard",
    "artifact_script_value_terminal_policy_with_domain_guard",
    "artifact_script_value_terminal_policy_with_event_guard",
    "needs_artifact_script_value_reward_tooltip_policy",
    "needs_artifact_script_value_scope_getter_policy",
    "needs_artifact_script_value_residual_repair",
    "needs_artifact_script_value_dynamic_parser_escape",
    "artifact_script_value_blocked_uncertain",
}

ARTIFACT_MARKERS = [
    ("Artifact", re.compile(r"artifact|accolade|knight|tournament|survey|activity|tour", re.I)),
    ("Activity", re.compile(r"activity|tournament|survey|tour|contest|GetActivityType", re.I)),
]

SCRIPT_VALUE_MARKERS = [
    ("ScriptValue", re.compile(r"ScriptValue", re.I)),
    ("NumericExpression", re.compile(r"Subtract_CFixedPoint|GetValue|#(?:P|p|n|bold)|[0-9]+\s*%|\|[+0V-]+", re.I)),
    ("MakeScope", re.compile(r"MakeScope", re.I)),
    ("EmptyScope", re.compile(r"EmptyScope", re.I)),
]

GUARD_MARKERS = [
    ("NumericGuard", re.compile(r"ScriptValue|Subtract_CFixedPoint|GetValue|#(?:P|p|n|bold)|[0-9]+\s*%|\|[+0V-]+", re.I)),
    ("DomainGuard", re.compile(r"activity|tournament|survey|tour|county|development|opinion|prestige|trait|knight|vassal", re.I)),
    ("EventGuard", re.compile(r"event|events|\.desc|desc_|tooltip|tt_|_tt|segment", re.I)),
    ("ScopeGetterGuard", re.compile(r"ROOT\.|GetPlayer|Activity\.|MakeScope|EmptyScope|GetTrait|GetActivityType|GetVassalStance", re.I)),
]

SECONDARY_MARKERS = [
    ("RewardTooltip", re.compile(r"EFFECT_LIST_BULLET|BULLET_WITH_TAB|tooltip|_tt\b|\.tt\b|gain|ganha|ganham|reward|opinion|prestige|gold|cost|custos", re.I)),
    ("ScopeGetter", re.compile(r"ROOT\.|GetPlayer|Activity\.|MakeScope|EmptyScope|GetTrait|GetActivityType|GetVassalStance", re.I)),
    ("Numeric", re.compile(r"ScriptValue|Subtract_CFixedPoint|GetValue|#(?:P|p|n|bold)|[0-9]+\s*%|\|[+0V-]+", re.I)),
    ("Event", re.compile(r"event|events|\.desc|desc_", re.I)),
    ("Domain", re.compile(r"activity|tournament|survey|tour|county|development|opinion|prestige|trait|knight|vassal", re.I)),
    ("ResidualVisible", re.compile(r"ÃƒÆ’|Ãƒâ€š|Ã‚Â¿|Ã‚Â¡|Ã¢â‚¬â„¢|Ã¢â‚¬Å“|Ã¢â‚¬ï¿½|ï¿½|\bthe\b|\byour\b|\byou\b|\btheir\b|\bcannot\b", re.I)),
    ("DynamicToken", re.compile(r"ScriptValue|Subtract_CFixedPoint|GetTrait|GetActivityType|ROOT\.|GetPlayer|MakeScope|EmptyScope|\[[^\]]+\]|\$[^$]+\$|dynamic_ck3_expression|dynamictoken", re.I)),
]


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
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
        and row.get("artifact_activity_decision") == SOURCE_DECISION
    ]
    rows.sort(key=lambda row: (str(row.get("relative_path") or ""), str(row.get("source_key") or ""), int(row["segment_id"])))
    seen: set[int] = set()
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


def detect(patterns: list[tuple[str, re.Pattern[str]]], blob: str) -> list[str]:
    return [name for name, pattern in patterns if pattern.search(blob)]


def classify(
    state: dict[str, Any] | None,
    script_markers: list[str],
    guard_markers: list[str],
    secondary_markers: list[str],
) -> tuple[str, str, str]:
    script = set(script_markers)
    guards = set(guard_markers)
    secondary = set(secondary_markers)
    if not state or state["state_group"] != "pending" or int(state["is_closed"] or 0) != 0:
        return "artifact_script_value_blocked_uncertain", "state_guard", "segment is not pending in selected state run"
    if int(state["needs_output_apply"] or 0) != 0 or int(state["confirmed_matches_output"] or 0) != 1:
        return "artifact_script_value_blocked_uncertain", "state_guard", "state guard failed: needs_output_apply or confirmed_matches_output"
    if "ResidualVisible" in secondary:
        return "needs_artifact_script_value_residual_repair", "residual_dependency_filtered_repair", "visible residual remains"
    if "ScriptValue" in script or "NumericExpression" in script or "Numeric" in secondary:
        if "Event" in secondary or "EventGuard" in guards:
            return "artifact_script_value_terminal_policy_with_event_guard", "artifact_activity_script_value_policy", "ScriptValue/numeric artifact activity surface with event guard"
        if "Domain" in secondary or "DomainGuard" in guards:
            return "artifact_script_value_terminal_policy_with_domain_guard", "artifact_activity_script_value_policy", "ScriptValue/numeric artifact activity surface with domain guard"
        return "artifact_script_value_terminal_policy_with_numeric_guard", "artifact_activity_script_value_policy", "ScriptValue/numeric artifact activity surface"
    if "RewardTooltip" in secondary:
        return "needs_artifact_script_value_reward_tooltip_policy", "artifact_script_value_reward_tooltip_policy", "reward tooltip dominates without clear ScriptValue"
    if "ScopeGetter" in secondary or "ScopeGetterGuard" in guards:
        return "needs_artifact_script_value_scope_getter_policy", "artifact_script_value_scope_getter_policy", "scope/getter dominates without clear ScriptValue"
    if "DynamicToken" in secondary:
        return "needs_artifact_script_value_dynamic_parser_escape", "ck3_dynamic_symbolic_parser", "dynamic parser escape remains"
    return "artifact_script_value_blocked_uncertain", "human_review_or_evidence_collection", "insufficient ScriptValue evidence"


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_artifact_activity_script_value_policy_review"
    spec = reports_dir / f"{stamp}_artifact_activity_script_value_policy_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only artifact/activity ScriptValue policy review.")
    parser.add_argument("--artifact-activity-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()

    rows = source_rows(args.artifact_activity_jsonl)
    segment_ids = [int(row["segment_id"]) for row in rows]
    conn = connect_readonly()
    states = fetch_states(conn, args.segment_state_run_id, segment_ids)
    texts = fetch_texts(conn, segment_ids)

    results: list[dict[str, Any]] = []
    decision_counts: Counter[str] = Counter()
    artifact_counts: Counter[str] = Counter()
    script_counts: Counter[str] = Counter()
    guard_counts: Counter[str] = Counter()
    secondary_counts: Counter[str] = Counter()
    pending_count = 0

    for row in rows:
        segment_id = int(row["segment_id"])
        state = states.get(segment_id)
        text = texts.get(segment_id, {})
        old_text = str(row.get("old_text") or text.get("old_text") or "")
        confirmed_text = str(row.get("confirmed_text") or text.get("confirmed_text") or "")
        output_text = str(row.get("output_text") or text.get("output_text") or "")
        if state and state["state_group"] == "pending" and int(state["is_closed"] or 0) == 0:
            pending_count += 1
        blob = " ".join(
            [
                str(row.get("relative_path") or ""),
                str(row.get("source_key") or ""),
                old_text,
                confirmed_text,
                output_text,
                " ".join(row.get("families_open") or []),
                " ".join(row.get("artifact_markers") or []),
                " ".join(row.get("activity_markers") or []),
                " ".join(row.get("secondary_markers") or []),
            ]
        )
        artifact_markers = sorted(set(row.get("artifact_markers") or []) | set(detect(ARTIFACT_MARKERS, blob)))
        script_value_markers = detect(SCRIPT_VALUE_MARKERS, blob)
        guard_markers = detect(GUARD_MARKERS, blob)
        secondary_markers = sorted(set(row.get("secondary_markers") or []) | set(detect(SECONDARY_MARKERS, blob)))
        decision, next_component, rationale = classify(state, script_value_markers, guard_markers, secondary_markers)
        if decision not in ALLOWED_DECISIONS:
            raise SystemExit(f"unknown decision {decision} for segment_id {segment_id}")
        decision_counts[decision] += 1
        artifact_counts.update(artifact_markers or ["NoArtifactMarker"])
        script_counts.update(script_value_markers or ["NoScriptValueMarker"])
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
                "old_text": old_text,
                "confirmed_text": confirmed_text,
                "output_text": output_text,
                "artifact_markers": artifact_markers,
                "script_value_markers": script_value_markers,
                "guard_markers": guard_markers,
                "secondary_markers": secondary_markers,
                "artifact_script_value_decision": decision,
                "next_component": next_component,
                "requires_lifecycle_later": False,
                "requires_apply_later": False,
                "corrected_text": "",
                "rationale": rationale,
            }
        )

    terminal_count = sum(count for decision, count in decision_counts.items() if decision.startswith("artifact_script_value_terminal_policy"))
    terminal_policy = terminal_count == len(results)
    dominant, dominant_count = decision_counts.most_common(1)[0] if decision_counts else ("none", 0)
    if terminal_policy:
        next_prompt = "chat_exec_effect_list_gender_local_player_policy_review_prompt.md"
        stop_rule = "terminal_readonly_spec_save_for_batch_registration"
    else:
        next_prompt = "chat_exec_effect_list_gender_local_player_policy_review_prompt.md"
        stop_rule = "fragmented_close_artifact_activity_script_value_branch"

    spec = {
        "schema_version": 1,
        "created_for": "terminal_read_only_policy_spec",
        "parent_policy": PARENT_POLICY,
        "policy_id": "artifact_activity_script_value_policy",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": LEDGER_RUN_ID,
        "entry_conditions": [
            "artifact_activity_decision == needs_artifact_activity_script_value_policy",
            "primary_route == effect_list_multiline_policy",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
        ],
        "positive_markers": ["ScriptValue", "Subtract_CFixedPoint", "MakeScope", "EmptyScope", "numeric CK3 formatting"],
        "guard_markers": ["NumericGuard", "DomainGuard", "EventGuard", "ScopeGetterGuard"],
        "router_priority": "after artifact_activity_gender_local_player_policy and before generic dynamic parser",
        "recommended_pipeline_stage": "requirement_effect_router",
        "fallback_stage": "parser_backed_dynamic_expression",
        "outputs": [{"decision": decision, "sampled": count} for decision, count in decision_counts.most_common()],
        "blocked_conditions": ["state guard failed", "visible residual", "ScriptValue/numeric marker absent"],
        "promotion_gate": "read_only_component_only_no_apply_no_lifecycle",
        "sampled": len(results),
        "terminal_policy": terminal_policy,
        "stop_rule": stop_rule,
    }

    txt_path, jsonl_path, spec_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({
            "record_type": "summary",
            "source_decision": SOURCE_DECISION,
            "primary_route": PRIMARY_ROUTE,
            "parent_policy": PARENT_POLICY,
            "total_reviewed": len(results),
            "pending_count": pending_count,
            "decision_counts": dict(decision_counts),
            "artifact_marker_counts": dict(artifact_counts),
            "script_value_marker_counts": dict(script_counts),
            "guard_marker_counts": dict(guard_counts),
            "secondary_marker_counts": dict(secondary_counts),
            "ready_lifecycle_future": 0,
            "apply_candidates_future": 0,
            "terminal_policy": terminal_policy,
            "dominant_subtype": dominant,
            "dominant_count": dominant_count,
            "stop_rule": stop_rule,
            "next_prompt": next_prompt,
            "requires_lifecycle_later": False,
            "requires_apply_later": False,
        }, ensure_ascii=False, sort_keys=True) + "\n")
        for decision, count in decision_counts.most_common():
            handle.write(json.dumps({"record_type": "decision_count", "artifact_script_value_decision": decision, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for counter_name, counter in [
            ("artifact_marker_count", artifact_counts),
            ("script_value_marker_count", script_counts),
            ("guard_marker_count", guard_counts),
            ("secondary_marker_count", secondary_counts),
        ]:
            for marker, count in counter.most_common():
                handle.write(json.dumps({"record_type": counter_name, "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for priority, (prompt, rationale) in enumerate([
            (next_prompt, "selected by small ScriptValue branch stop rule"),
            ("chat_exec_effect_list_trait_accolade_policy_review_prompt.md", "alternate next strong effect-list block"),
            ("chat_exec_effect_list_artifact_activity_policy_batch_registration_prompt.md", "batch-register artifact/activity terminal specs later"),
        ], 1):
            handle.write(json.dumps({"record_type": "strategy", "priority": priority, "next_prompt": prompt, "rationale": rationale}, ensure_ascii=False, sort_keys=True) + "\n")
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")

    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Artifact/activity ScriptValue policy review\n\n")
        handle.write(f"- total revisado: {len(results)}\n")
        handle.write("- ready_lifecycle_future: 0\n")
        handle.write("- apply_candidates_future: 0\n")
        handle.write(f"- terminal_policy: {terminal_policy}\n")
        handle.write(f"- subtipo dominante: {dominant}\n")
        handle.write(f"- dominant_count: {dominant_count}\n")
        handle.write(f"- regra_de_parada: {stop_rule}\n")
        handle.write(f"- proximo_prompt: {next_prompt}\n\n")
        handle.write("artifact_script_value_decision_counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nTop artifact markers:\n")
        for marker, count in artifact_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop ScriptValue markers:\n")
        for marker, count in script_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop guard markers:\n")
        for marker, count in guard_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop secondary markers:\n")
        for marker, count in secondary_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nAnalise\n")
        handle.write("- artifact_activity_script_value_policy deve ficar como pequena policy read-only terminal/guard.\n")
        handle.write("- Esta revisao nao gera lifecycle/apply.\n")
        handle.write("- Guardar spec para registro em lote de policies effect-list e seguir para o proximo bloco forte.\n")

    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"total_reviewed: {len(results)}")
    print(f"terminal_policy: {terminal_policy}")
    print("ready_lifecycle_future: 0")
    print("apply_candidates_future: 0")
    print(f"stop_rule: {stop_rule}")
    print("decision_counts:")
    for decision, count in decision_counts.most_common():
        print(f"  {decision}: {count}")


if __name__ == "__main__":
    main()
