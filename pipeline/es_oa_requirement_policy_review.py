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


LEDGER_RUN_ID = 76
SOURCE_DECISION = "needs_es_oa_requirement_policy"

ALLOWED_DECISIONS = {
    "es_oa_terminal_policy",
    "es_oa_terminal_policy_with_local_player_guard",
    "es_oa_terminal_policy_with_actor_target_guard",
    "es_oa_terminal_policy_with_domain_guard",
    "needs_es_oa_residual_repair",
    "needs_es_oa_dynamic_parser_escape",
    "es_oa_blocked_uncertain",
}

ES_MARKERS = [
    ("ES_OA", re.compile(r"ES_OA", re.I)),
    ("ES_XA", re.compile(r"ES_XA|ES_XA_EA", re.I)),
    ("ES_AlAla", re.compile(r"ES_AlAla", re.I)),
    ("CustomES", re.compile(r"Custom\('ES_", re.I)),
]

GUARD_MARKERS = [
    ("LocalPlayerGuard", re.compile(r"IsLocalPlayer|LocalPlayer|GetPlayer|\bvoce\b|\bvoc.\b", re.I)),
    ("ActorTargetGuard", re.compile(r"Character\.|ROOT\.|FROM\.|TARGET\.|GetProtagonist|GetNameNoTooltip|GetTitleAsName", re.I)),
    ("DomainGuard", re.compile(r"accolade|galardão|cavaleiro|coronation|coroação|legend|faith|HighGodName|cerimônia", re.I)),
]

SECONDARY_MARKERS = [
    ("ResidualVisible", re.compile(r"Ãƒ|Ã‚|Â¿|Â¡|â€™|â€œ|â€�|�|\bthe\b|\byour\b|\byou\b|\btheir\b|\bcannot\b", re.I)),
    ("DynamicToken", re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)),
    ("Domain", re.compile(r"accolade|galardão|cavaleiro|coronation|coroação|legend|faith|HighGodName|cerimônia", re.I)),
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
        and row.get("gender_requirement_decision") == SOURCE_DECISION
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
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, state_group, is_closed, needs_output_apply,
               confirmed_matches_output, needs_reopen
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def normalize(text: str) -> str:
    return text.replace("Você", "Voce").replace("você", "voce")


def markers(patterns: list[tuple[str, re.Pattern[str]]], blob: str) -> list[str]:
    normalized = normalize(blob)
    return [label for label, pattern in patterns if pattern.search(normalized)]


def classify(state: dict[str, Any] | None, guards: list[str], secondary: list[str]) -> tuple[str, str, str]:
    guard_set = set(guards)
    secondary_set = set(secondary)
    if not state or state["state_group"] != "pending" or int(state["is_closed"] or 0) != 0:
        return "es_oa_blocked_uncertain", "human_review_or_evidence_collection", "segment is not pending in selected state run"
    if int(state["needs_output_apply"] or 0) != 0 or int(state["confirmed_matches_output"] or 0) != 1:
        return "es_oa_blocked_uncertain", "state_guard", "state guard failed: needs_output_apply or confirmed_matches_output"
    if "ResidualVisible" in secondary_set:
        return "needs_es_oa_residual_repair", "residual_dependency_filtered_repair", "visible residual remains"
    if "DomainGuard" in guard_set:
        return "es_oa_terminal_policy_with_domain_guard", "terminal_router_policy", "small terminal ES helper rule with domain guard"
    if "ActorTargetGuard" in guard_set:
        return "es_oa_terminal_policy_with_actor_target_guard", "terminal_router_policy", "small terminal ES helper rule with actor/target guard"
    if "LocalPlayerGuard" in guard_set:
        return "es_oa_terminal_policy_with_local_player_guard", "terminal_router_policy", "small terminal ES helper rule with local-player guard"
    if "DynamicToken" in secondary_set:
        return "needs_es_oa_dynamic_parser_escape", "ck3_dynamic_symbolic_parser", "dynamic parser escape remains"
    return "es_oa_terminal_policy", "terminal_router_policy", "small terminal ES helper rule"


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_es_oa_requirement_policy_review"
    spec = reports_dir / f"{stamp}_es_oa_requirement_policy_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only ES_OA requirement terminal policy review.")
    parser.add_argument("--gender-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()

    rows = source_rows(args.gender_jsonl)
    conn = connect_readonly()
    states = fetch_states(conn, args.segment_state_run_id, [int(row["segment_id"]) for row in rows])

    results = []
    decision_counts: Counter[str] = Counter()
    es_counts: Counter[str] = Counter()
    guard_counts: Counter[str] = Counter()
    secondary_counts: Counter[str] = Counter()
    pending_count = 0

    for row in rows:
        segment_id = int(row["segment_id"])
        state = states.get(segment_id)
        if state and state["state_group"] == "pending" and int(state["is_closed"] or 0) == 0:
            pending_count += 1
        blob = " ".join(str(row.get(key) or "") for key in ("relative_path", "source_key", "old_text", "confirmed_text", "output_text"))
        es_markers = markers(ES_MARKERS, blob)
        guard_markers = markers(GUARD_MARKERS, blob)
        secondary_markers = markers(SECONDARY_MARKERS, blob)
        decision, router_recommendation, rationale = classify(state, guard_markers, secondary_markers)
        if decision not in ALLOWED_DECISIONS:
            raise SystemExit(f"unknown decision {decision} for segment_id {segment_id}")
        decision_counts[decision] += 1
        es_counts.update(es_markers or ["NoESMarker"])
        guard_counts.update(guard_markers or ["NoGuardMarker"])
        secondary_counts.update(secondary_markers or ["NoSecondaryMarker"])
        results.append({
            "record_type": "sample_review",
            "segment_id": segment_id,
            "relative_path": str(row.get("relative_path") or ""),
            "source_key": str(row.get("source_key") or ""),
            "families_open": list(row.get("families_open") or []),
            "source_decision": SOURCE_DECISION,
            "old_text": str(row.get("old_text") or ""),
            "confirmed_text": str(row.get("confirmed_text") or ""),
            "output_text": str(row.get("output_text") or ""),
            "es_oa_markers": es_markers,
            "guard_markers": guard_markers,
            "secondary_markers": secondary_markers,
            "es_oa_decision": decision,
            "router_recommendation": router_recommendation,
            "requires_lifecycle_later": False,
            "requires_apply_later": False,
            "corrected_text": "",
            "rationale": rationale,
        })

    terminal_count = sum(count for decision, count in decision_counts.items() if decision.startswith("es_oa_terminal_policy"))
    is_terminal = terminal_count == len(results)
    next_prompt = "chat_exec_script_value_requirement_policy_review_prompt.md"

    spec = {
        "schema_version": 1,
        "created_for": "terminal_read_only_policy_spec",
        "parent_policy": "gender_local_player_requirement_policy",
        "policy_id": "es_oa_requirement_policy",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": LEDGER_RUN_ID,
        "entry_conditions": [
            "gender_requirement_decision == needs_es_oa_requirement_policy",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
        ],
        "positive_markers": ["Custom('ES_*')", "ES_XA", "ES_AlAla", "small ES helper remainder"],
        "guard_markers": dict(guard_counts),
        "router_priority": "small embedded rule inside gender_local_player_requirement_policy",
        "recommended_pipeline_stage": "gender_local_player_requirement_policy",
        "fallback_stage": "parser_backed_dynamic_expression",
        "outputs": ["terminal router classification only", "no lifecycle candidate", "no apply candidate"],
        "blocked_conditions": ["state guard failed", "visible residual"],
        "promotion_gate": "read_only_component_only_no_apply_no_lifecycle",
        "terminal_policy": is_terminal,
        "stop_rule": "terminal_micro_branch_closed_return_to_larger_blocks",
        "es_oa_types": [{"decision": decision, "sampled": count} for decision, count in decision_counts.most_common()],
    }

    txt_path, jsonl_path, spec_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({
            "record_type": "summary",
            "source_decision": SOURCE_DECISION,
            "total_reviewed": len(results),
            "pending_count": pending_count,
            "decision_counts": dict(decision_counts),
            "es_oa_marker_counts": dict(es_counts),
            "guard_marker_counts": dict(guard_counts),
            "secondary_marker_counts": dict(secondary_counts),
            "ready_lifecycle_future": 0,
            "apply_candidates_future": 0,
            "terminal_policy": is_terminal,
            "close_gender_local_player_requirement_branch": True,
            "next_prompt": next_prompt,
        }, ensure_ascii=False, sort_keys=True) + "\n")
        for decision, count in decision_counts.most_common():
            handle.write(json.dumps({"record_type": "decision_count", "es_oa_decision": decision, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in es_counts.most_common():
            handle.write(json.dumps({"record_type": "es_oa_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in guard_counts.most_common():
            handle.write(json.dumps({"record_type": "guard_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in secondary_counts.most_common():
            handle.write(json.dumps({"record_type": "secondary_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for priority, (prompt, rationale) in enumerate([
            (next_prompt, "ES_OA micro-branch closed; return to larger requirement tooltip block"),
            ("chat_exec_concept_requirement_policy_review_prompt.md", "alternate larger sibling block"),
            ("chat_exec_requirement_effect_router_component_integration_review_prompt.md", "architecture/integration option after terminal policy consolidation"),
        ], 1):
            handle.write(json.dumps({"record_type": "strategy", "priority": priority, "next_prompt": prompt, "rationale": rationale}, ensure_ascii=False, sort_keys=True) + "\n")
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")

    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("ES_OA requirement terminal policy review\n\n")
        handle.write(f"total_revisado: {len(results)}\n")
        handle.write(f"pending_count: {pending_count}\n")
        handle.write("ready_lifecycle_future: 0\n")
        handle.write("apply_candidates_future: 0\n")
        handle.write(f"terminal_policy: {is_terminal}\n")
        handle.write("close_gender_local_player_requirement_branch: True\n")
        handle.write(f"proximo_foco: {next_prompt}\n\n")
        handle.write("es_oa_decision_counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nTop markers:\n")
        for marker, count in es_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop guard markers:\n")
        for marker, count in guard_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop secondary markers:\n")
        for marker, count in secondary_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nAnalise\n")
        handle.write("- es_oa_requirement_policy deve ficar como regra pequena dentro de gender_local_player_requirement_policy, nao como componente amplo isolado.\n")
        handle.write("- Este micro-ramo deve encerrar; nao gera lifecycle/apply.\n")

    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"total_reviewed: {len(results)}")
    print(f"pending_count: {pending_count}")
    print("ready_lifecycle_future: 0")
    print("apply_candidates_future: 0")
    print(f"terminal_policy: {is_terminal}")
    print("decision_counts:")
    for decision, count in decision_counts.most_common():
        print(f"  {decision}: {count}")


if __name__ == "__main__":
    main()
