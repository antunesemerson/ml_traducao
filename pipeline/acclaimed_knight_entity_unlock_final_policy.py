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
SOURCE_DECISION = "needs_entity_requirement_acclaimed_knight_unlock_policy"

ALLOWED_DECISIONS = {
    "final_policy_acclaimed_knight_unlock_requirement",
    "final_policy_acclaimed_knight_unlock_requirement_with_trait_guard",
    "final_policy_acclaimed_knight_unlock_requirement_with_scope_guard",
    "final_policy_acclaimed_knight_unlock_requirement_with_domain_guard",
    "final_policy_acclaimed_knight_unlock_requirement_with_event_guard",
    "needs_final_policy_residual_repair",
    "needs_final_policy_dynamic_parser_escape",
    "final_policy_blocked_uncertain",
}

UNLOCK_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("AttributeUnlock", re.compile(r"_attribute|GetAccoladeType\([^)]*attribute", re.I)),
    ("CanBecome", re.compile(r"pode se tornar|pode tornar-se|pode converter-se|pode ser tornado|pode ser reconhecido", re.I)),
    ("UnlockKey", re.compile(r"\bunlock\b|UNLOCK_", re.I)),
]

ACCLAIMED_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("AcclaimedKnight", re.compile(r"acclaimed|acclaimed_knight", re.I)),
    ("AccoladeKnight", re.compile(r"accolade_knight|Accolade\.|accolade knight", re.I)),
    ("Knight", re.compile(r"knight|cavaleir|cavalaria|prowess", re.I)),
    ("Attribute", re.compile(r"_attribute|attribute|GetAccoladeType\([^)]*attribute", re.I)),
]

GUARD_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("TraitGuard", re.compile(r"GetTrait\(|\btrait\b|traits|trait_level_track", re.I)),
    ("ScopeGuard", re.compile(r"ROOT\.|FROM\.|TARGET\.|SCOPE\.|Character\.|Get[A-Za-z0-9_]+\(", re.I)),
    ("DomainGuard", re.compile(r"culture|religion|faith|doctrine|tradition|domain|\[[A-Za-z0-9_]+\|[^\]]+\]", re.I)),
    ("EventGuard", re.compile(r"event|\.desc|desc\.|option|toast|dialogue|story|scheme|interaction|memory|battle", re.I)),
    ("ResidualVisible", re.compile(r"NÃƒ|ÃƒÆ’|Ã‚|ï¿½|\b(?:the|your|you|their|cannot|consiguio|consiguiÃ³|sentisteis|sintieron|sera|serÃ¡|mas|mÃ¡s|facil|fÃ¡cil)\b", re.I)),
    ("DynamicToken", re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|GetAccoladeType|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)),
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
        and row.get("entity_requirement_decision") == SOURCE_DECISION
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
        SELECT segment_id, final_state, state_group, is_closed, needs_output_apply,
               confirmed_matches_output, needs_reopen
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def markers(patterns: list[tuple[str, re.Pattern[str]]], blob: str) -> list[str]:
    return [label for label, pattern in patterns if pattern.search(blob)]


def classify(state: dict[str, Any] | None, unlock_markers: list[str], acclaimed_markers: list[str], guard_markers: list[str]) -> tuple[str, str, str]:
    guards = set(guard_markers)
    if not state or state["state_group"] != "pending" or int(state["is_closed"] or 0) != 0:
        return "final_policy_blocked_uncertain", "human_review_or_evidence_collection", "segment is not pending in selected state run"
    if int(state["needs_output_apply"] or 0) != 0 or int(state["confirmed_matches_output"] or 0) != 1:
        return "final_policy_blocked_uncertain", "state_guard", "state guard failed: needs_output_apply or confirmed_matches_output"
    if "ResidualVisible" in guards:
        return "needs_final_policy_residual_repair", "residual_dependency_filtered_repair", "visible residual/mojibake remains"
    if not unlock_markers or not acclaimed_markers:
        return "final_policy_blocked_uncertain", "human_review_or_evidence_collection", "missing positive unlock/acclaimed markers"
    if "EventGuard" in guards:
        return "final_policy_acclaimed_knight_unlock_requirement_with_event_guard", "requirement_effect_router", "terminal pattern with event/battle guard"
    if "TraitGuard" in guards:
        return "final_policy_acclaimed_knight_unlock_requirement_with_trait_guard", "requirement_effect_router", "terminal pattern with trait guard"
    if "DomainGuard" in guards:
        return "final_policy_acclaimed_knight_unlock_requirement_with_domain_guard", "requirement_effect_router", "terminal pattern with concept/domain guard"
    if "ScopeGuard" in guards:
        return "final_policy_acclaimed_knight_unlock_requirement_with_scope_guard", "requirement_effect_router", "terminal pattern with scope/getter guard"
    if "DynamicToken" in guards:
        return "final_policy_acclaimed_knight_unlock_requirement", "requirement_effect_router", "dynamic token is the expected GetAccoladeType attribute surface"
    return "final_policy_acclaimed_knight_unlock_requirement", "requirement_effect_router", "direct Acclaimed Knight attribute unlock requirement"


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_acclaimed_knight_entity_unlock_final_policy"
    spec = reports_dir / f"{stamp}_acclaimed_knight_entity_unlock_final_policy_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def build_spec(run_id: int, decisions: Counter[str], unlock_counts: Counter[str], acclaimed_counts: Counter[str], guard_counts: Counter[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_for": "terminal_read_only_policy_spec",
        "parent_policy": "acclaimed_knight_entity_requirement_policy",
        "policy_id": "acclaimed_knight_entity_unlock_final_policy",
        "segment_state_run_id": run_id,
        "ledger_run_id": LEDGER_RUN_ID,
        "entry_conditions": [
            "entity_requirement_decision == needs_entity_requirement_acclaimed_knight_unlock_policy",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
            "requires_apply_later == false",
        ],
        "positive_markers": [{"marker": marker, "sampled": count} for marker, count in (unlock_counts + acclaimed_counts).most_common()],
        "guard_markers": [{"marker": marker, "sampled": count} for marker, count in guard_counts.most_common()],
        "router_priority": "after requirement_tooltip/scope_getter/get_trait/accolade/knight_attribute/unlock detection and before generic parser",
        "recommended_pipeline_stage": "requirement_effect_router",
        "fallback_stage": "parser_backed_dynamic_expression",
        "outputs": [{"decision": decision, "sampled": count} for decision, count in decisions.most_common()],
        "blocked_conditions": [
            "state guard failed",
            "missing positive unlock/acclaimed markers",
            "visible residual/mojibake",
            "dynamic structure outside GetAccoladeType attribute surface",
        ],
        "promotion_gate": "read_only_component_only_no_apply_no_lifecycle",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Terminal read-only Acclaimed Knight entity unlock policy.")
    parser.add_argument("--entity-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()

    rows = source_rows(args.entity_jsonl)
    conn = connect_readonly()
    states = fetch_states(conn, args.segment_state_run_id, [int(row["segment_id"]) for row in rows])

    results: list[dict[str, Any]] = []
    decision_counts: Counter[str] = Counter()
    unlock_counts: Counter[str] = Counter()
    acclaimed_counts: Counter[str] = Counter()
    guard_counts: Counter[str] = Counter()
    router_counts: Counter[str] = Counter()
    pending_count = 0

    for row in rows:
        segment_id = int(row["segment_id"])
        state = states.get(segment_id)
        if state and state["state_group"] == "pending" and int(state["is_closed"] or 0) == 0:
            pending_count += 1
        blob = " ".join(str(row.get(key) or "") for key in ("relative_path", "source_key", "old_text", "confirmed_text", "output_text"))
        unlock_markers = markers(UNLOCK_MARKERS, blob)
        acclaimed_markers = markers(ACCLAIMED_MARKERS, blob)
        guard_markers = markers(GUARD_MARKERS, blob)
        decision, router_recommendation, rationale = classify(state, unlock_markers, acclaimed_markers, guard_markers)
        if decision not in ALLOWED_DECISIONS:
            raise SystemExit(f"unknown decision {decision} for segment_id {segment_id}")
        decision_counts[decision] += 1
        unlock_counts.update(unlock_markers or ["NoUnlockMarker"])
        acclaimed_counts.update(acclaimed_markers or ["NoAcclaimedKnightMarker"])
        guard_counts.update(guard_markers or ["NoGuardMarker"])
        router_counts[router_recommendation] += 1
        results.append(
            {
                "record_type": "sample_review",
                "segment_id": segment_id,
                "relative_path": str(row.get("relative_path") or ""),
                "source_key": str(row.get("source_key") or ""),
                "families_open": list(row.get("families_open") or []),
                "source_decision": SOURCE_DECISION,
                "old_text": str(row.get("old_text") or ""),
                "confirmed_text": str(row.get("confirmed_text") or ""),
                "output_text": str(row.get("output_text") or ""),
                "unlock_markers": unlock_markers,
                "acclaimed_knight_markers": acclaimed_markers,
                "guard_markers": guard_markers,
                "final_unlock_policy_decision": decision,
                "router_recommendation": router_recommendation,
                "requires_lifecycle_later": False,
                "requires_apply_later": False,
                "corrected_text": "",
                "rationale": rationale,
            }
        )

    txt_path, jsonl_path, spec_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({
            "record_type": "summary",
            "source_decision": SOURCE_DECISION,
            "total_reviewed": len(results),
            "pending_count": pending_count,
            "decision_counts": dict(decision_counts),
            "unlock_marker_counts": dict(unlock_counts),
            "acclaimed_knight_marker_counts": dict(acclaimed_counts),
            "guard_marker_counts": dict(guard_counts),
            "router_recommendation_counts": dict(router_counts),
            "ready_lifecycle_future": 0,
            "apply_candidates_future": 0,
            "terminal_policy": True,
            "next_focus": "needs_gender_local_player_requirement_policy",
        }, ensure_ascii=False, sort_keys=True) + "\n")
        for decision, count in decision_counts.most_common():
            handle.write(json.dumps({"record_type": "decision_count", "final_unlock_policy_decision": decision, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in unlock_counts.most_common():
            handle.write(json.dumps({"record_type": "unlock_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in acclaimed_counts.most_common():
            handle.write(json.dumps({"record_type": "acclaimed_knight_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in guard_counts.most_common():
            handle.write(json.dumps({"record_type": "guard_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for priority, (prompt, rationale) in enumerate([
            ("chat_exec_gender_local_player_requirement_policy_review_prompt.md", "larger remaining requirement-tooltip block from prior split"),
            ("chat_exec_requirement_effect_list_multiline_subpolicy_review_prompt.md", "second-largest parent route after tooltip"),
            ("chat_exec_requirement_tooltip_after_acclaimed_knight_closeout_focus_prompt.md", "optional focus recalculation if a fresh router decision is desired"),
        ], 1):
            handle.write(json.dumps({"record_type": "strategy", "priority": priority, "next_prompt": prompt, "rationale": rationale}, ensure_ascii=False, sort_keys=True) + "\n")
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")

    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(build_spec(args.segment_state_run_id, decision_counts, unlock_counts, acclaimed_counts, guard_counts), handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Acclaimed Knight entity unlock final policy\n\n")
        handle.write(f"total_revisado: {len(results)}\n")
        handle.write(f"pending_count: {pending_count}\n")
        handle.write("ready_lifecycle_future: 0\n")
        handle.write("apply_candidates_future: 0\n")
        handle.write("terminal_policy: true\n\n")
        handle.write("final_unlock_policy_decision_counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nTop unlock markers:\n")
        for marker, count in unlock_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop acclaimed knight markers:\n")
        for marker, count in acclaimed_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop guard markers:\n")
        for marker, count in guard_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nAnalise\n")
        handle.write("- Esta policy deve virar componente read-only real no requirement_effect_router.\n")
        handle.write("- Ela nao deve gerar lifecycle/apply em curto prazo.\n")
        handle.write("- Ela e terminal deste micro-ramo; nao abrir prompt mais estreito.\n")
        handle.write("- A cadeia requirement_tooltip -> scope_getter -> GetTrait -> accolade -> knight attribute -> unlock -> entity requirement -> final unlock policy fica registrada como conhecimento do roteador.\n")
        handle.write("- Proximo foco recomendado fora do micro-ramo: needs_gender_local_player_requirement_policy.\n")

    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"total_reviewed: {len(results)}")
    print(f"pending_count: {pending_count}")
    print("ready_lifecycle_future: 0")
    print("apply_candidates_future: 0")
    print("decision_counts:")
    for decision, count in decision_counts.most_common():
        print(f"  {decision}: {count}")


if __name__ == "__main__":
    main()
