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
SOURCE_DECISION = "needs_select_cstring_player_target_perspective_policy"

ALLOWED_DECISIONS = {
    "perspective_ready_false_reopen",
    "perspective_ready_lifecycle",
    "needs_player_target_direct_policy",
    "needs_local_player_perspective_policy",
    "needs_actor_target_recipient_policy",
    "needs_possessive_after_perspective_policy",
    "needs_es_helper_after_perspective_policy",
    "needs_custom_loc_after_perspective_policy",
    "needs_domain_context_after_perspective",
    "needs_event_context_after_perspective",
    "needs_residual_repair_after_perspective",
    "needs_dynamic_parser_after_perspective",
    "perspective_blocked_uncertain",
}

SELECT_CSTRING_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("SelectCString", re.compile(r"Select_CString\s*\(", re.I)),
    ("SelectCStringIsLocalPlayer", re.compile(r"Select_CString\s*\([^)]*IsLocalPlayer", re.I)),
    ("SelectCStringShortUIName", re.compile(r"Select_CString\s*\([^)]*GetShortUIName", re.I)),
    ("SelectCStringSameChoice", re.compile(r"Select_CString\s*\([^)]*'([^']+)'\s*,\s*'\1'", re.I)),
    ("MultipleSelectCString", re.compile(r"Select_CString.*Select_CString", re.I | re.S)),
]

PERSPECTIVE_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("SecondPersonPronoun", re.compile(r"\bvoce\b|\bvoc.\b|\bseu personagem\b", re.I)),
    ("SecondPersonVerb", re.compile(r"\bamas\b|\bdetestas\b|\brecebes\b|\bconquistar(?:as)?\b|\bsufriste\b|\bpasaras\b|\bolhas\b|\bvoce e\b", re.I)),
    ("NameFallback", re.compile(r"GetShortUIName", re.I)),
    ("DirectPlayerVsTarget", re.compile(r"Select_CString\s*\([^)]*'(?:voce|voc.|seu personagem|amas|detestas|conquistar|sufriste|pasaras|voce e)[^']*'\s*,\s*(?:'[^']+'|[^)]*GetShortUIName)", re.I)),
]

LOCAL_PLAYER_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("IsLocalPlayer", re.compile(r"IsLocalPlayer|CHARACTER\.IsLocalPlayer", re.I)),
    ("GetPlayer", re.compile(r"GetPlayer|GetLocalPlayer|local_player", re.I)),
]

SECONDARY_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("ActorTargetRecipient", re.compile(r"\bactor\b|\btarget\b|\brecipient\b|\baddressee\b|ROOT\.|FROM\.|TARGET_|TARGET\.|CHARACTER\.", re.I)),
    ("Possessive", re.compile(r"\bseu\b|\bsua\b|\bseus\b|\bsuas\b|\bdele\b|\bdela\b|\bteu\b|\btua\b|\bvosso\b|\bvossa\b", re.I)),
    ("ESHelper", re.compile(r"ES_ElLa|ES_DelDela|ES_OA|ES_XA_EA|ES_XA|ES_EA|Custom\('ES_", re.I)),
    ("CustomLoc", re.compile(r"CustomLoc|Custom2?\((?!'ES_)", re.I)),
    ("Domain", re.compile(r"culture|religion|faith|doctrine|tradition|dynasty|house|domain|province|building|accolade|livonia|islam", re.I)),
    ("Event", re.compile(r"event|\.desc|desc\.|option|toast|dialogue|story|scheme|interaction|memory|battle|activities/", re.I)),
    ("ResidualVisible", re.compile(r"Ãƒ|Ã‚|Â¿|Â¡|â€™|â€œ|â€�|�|\bsufrió\b|\bpasará\b|\bthe\b|\byour\b|\byou\b|\btheir\b|\bcannot\b", re.I)),
    ("DynamicToken", re.compile(r"Custom\(|Select_CString|Concept\(|ScriptValue|GetTrait|ROOT\.|FROM\.|SCOPE\.|TARGET\.|\[[^\]]+\]|\$[^$]+\$", re.I)),
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
        and row.get("select_cstring_requirement_decision") == SOURCE_DECISION
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


def normalize(text: str) -> str:
    return (
        text.replace("você", "voce")
        .replace("Você", "Voce")
        .replace("conquistarás", "conquistaras")
        .replace("pasarás", "pasaras")
        .replace("você é", "voce e")
    )


def markers(patterns: list[tuple[str, re.Pattern[str]]], blob: str) -> list[str]:
    normalized = normalize(blob)
    return [label for label, pattern in patterns if pattern.search(normalized)]


def classify(
    state: dict[str, Any] | None,
    select_markers: list[str],
    perspective_markers: list[str],
    local_markers: list[str],
    secondary_markers: list[str],
) -> tuple[str, str, bool, str]:
    select = set(select_markers)
    perspective = set(perspective_markers)
    local = set(local_markers)
    secondary = set(secondary_markers)
    if not state or state["state_group"] != "pending" or int(state["is_closed"] or 0) != 0:
        return "perspective_blocked_uncertain", "human_review_or_evidence_collection", False, "segment is not pending in selected state run"
    if int(state["needs_output_apply"] or 0) != 0 or int(state["confirmed_matches_output"] or 0) != 1:
        return "perspective_blocked_uncertain", "state_guard", False, "state guard failed: needs_output_apply or confirmed_matches_output"
    if "ResidualVisible" in secondary:
        return "needs_residual_repair_after_perspective", "residual_dependency_filtered_repair", False, "visible residual remains after perspective routing"
    if "ESHelper" in secondary:
        return "needs_es_helper_after_perspective_policy", "select_cstring_es_helper_policy", False, "ES helper remains after perspective routing"
    if "CustomLoc" in secondary:
        return "needs_custom_loc_after_perspective_policy", "select_cstring_custom_loc_policy", False, "CustomLoc remains after perspective routing"
    if "Possessive" in secondary:
        return "needs_possessive_after_perspective_policy", "select_cstring_possessive_policy", False, "possessive perspective remains"
    if "DirectPlayerVsTarget" in perspective or ("SecondPersonPronoun" in perspective and "NameFallback" in perspective) or "SecondPersonVerb" in perspective:
        return "needs_player_target_direct_policy", "select_cstring_player_target_direct_policy", False, "Select_CString directly chooses player-facing text versus third-person target/name"
    if local:
        return "needs_local_player_perspective_policy", "select_cstring_local_player_perspective_policy", False, "LocalPlayer/IsLocalPlayer predicate remains the controlling perspective"
    if "ActorTargetRecipient" in secondary:
        return "needs_actor_target_recipient_policy", "actor_target_recipient_policy", False, "actor/target/recipient perspective remains"
    if "Domain" in secondary:
        return "needs_domain_context_after_perspective", "domain_context_composer", False, "domain context remains after perspective routing"
    if "Event" in secondary:
        return "needs_event_context_after_perspective", "event_context_composer", False, "event context remains after perspective routing"
    if "DynamicToken" in secondary or select:
        return "needs_dynamic_parser_after_perspective", "ck3_dynamic_symbolic_parser", False, "dynamic token remains after perspective routing"
    if int(state["confirmed_matches_output"] or 0) == 1:
        if int(state["needs_reopen"] or 0) == 1:
            return "perspective_ready_false_reopen", "false_reopen_lifecycle_bridge", True, "plain perspective case may be future false-reopen candidate"
        return "perspective_ready_lifecycle", "perspective_lifecycle_bridge", True, "plain perspective case may be future lifecycle candidate"
    return "perspective_blocked_uncertain", "human_review_or_evidence_collection", False, "insufficient perspective marker evidence"


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_select_cstring_player_target_perspective_review"
    spec = reports_dir / f"{stamp}_select_cstring_player_target_perspective_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def build_spec(
    run_id: int,
    decisions: Counter[str],
    select_counts: Counter[str],
    perspective_counts: Counter[str],
    local_counts: Counter[str],
    secondary_counts: Counter[str],
    stop_rule: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": "select_cstring_requirement_policy",
        "policy_id": "select_cstring_player_target_perspective_policy",
        "segment_state_run_id": run_id,
        "ledger_run_id": LEDGER_RUN_ID,
        "entry_conditions": [
            "select_cstring_requirement_decision == needs_select_cstring_player_target_perspective_policy",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
        ],
        "perspective_types": [{"decision": decision, "sampled": count} for decision, count in decisions.most_common()],
        "resolution_order": [
            "state guard",
            "residual guard",
            "ES helper and CustomLoc guards",
            "possessive after perspective",
            "direct player/target perspective",
            "local-player predicate",
            "actor/target/recipient",
            "domain/event/dynamic parser handoff",
        ],
        "next_components": [
            "select_cstring_player_target_direct_policy",
            "select_cstring_local_player_perspective_policy",
            "actor_target_recipient_policy",
            "select_cstring_possessive_policy",
            "select_cstring_es_helper_policy",
            "select_cstring_custom_loc_policy",
            "domain_context_composer",
            "event_context_composer",
            "ck3_dynamic_symbolic_parser",
        ],
        "blocked_conditions": [
            "state guard failed",
            "visible residual",
            "ambiguous perspective choice",
            "no subdecision reaches stop-rule threshold",
        ],
        "promotion_gate": "Read-only component only; lifecycle/apply requires separate guarded prompt.",
        "stop_rule": stop_rule,
        "observed_select_cstring_marker_counts": dict(select_counts),
        "observed_perspective_marker_counts": dict(perspective_counts),
        "observed_local_player_marker_counts": dict(local_counts),
        "observed_secondary_marker_counts": dict(secondary_counts),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Select_CString player/target perspective review.")
    parser.add_argument("--select-cstring-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()

    rows = source_rows(args.select_cstring_jsonl)
    conn = connect_readonly()
    states = fetch_states(conn, args.segment_state_run_id, [int(row["segment_id"]) for row in rows])

    results: list[dict[str, Any]] = []
    decision_counts: Counter[str] = Counter()
    select_counts: Counter[str] = Counter()
    perspective_counts: Counter[str] = Counter()
    local_counts: Counter[str] = Counter()
    secondary_counts: Counter[str] = Counter()
    lifecycle_later = 0
    apply_later = 0
    pending_count = 0

    for row in rows:
        segment_id = int(row["segment_id"])
        state = states.get(segment_id)
        if state and state["state_group"] == "pending" and int(state["is_closed"] or 0) == 0:
            pending_count += 1
        blob = " ".join(str(row.get(key) or "") for key in ("relative_path", "source_key", "old_text", "confirmed_text", "output_text"))
        select_markers = markers(SELECT_CSTRING_MARKERS, blob)
        perspective_markers = markers(PERSPECTIVE_MARKERS, blob)
        local_player_markers = markers(LOCAL_PLAYER_MARKERS, blob)
        secondary_markers = markers(SECONDARY_MARKERS, blob)
        decision, component, lifecycle, rationale = classify(
            state,
            select_markers,
            perspective_markers,
            local_player_markers,
            secondary_markers,
        )
        if decision not in ALLOWED_DECISIONS:
            raise SystemExit(f"unknown decision {decision} for segment_id {segment_id}")
        requires_apply_later = False
        lifecycle_later += int(lifecycle)
        apply_later += int(requires_apply_later)
        decision_counts[decision] += 1
        select_counts.update(select_markers or ["NoSelectCStringMarker"])
        perspective_counts.update(perspective_markers or ["NoPerspectiveMarker"])
        local_counts.update(local_player_markers or ["NoLocalPlayerMarker"])
        secondary_counts.update(secondary_markers or ["NoSecondaryMarker"])
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
                "select_cstring_markers": select_markers,
                "perspective_markers": perspective_markers,
                "local_player_markers": local_player_markers,
                "secondary_markers": secondary_markers,
                "perspective_decision": decision,
                "next_component": component,
                "requires_lifecycle_later": lifecycle,
                "requires_apply_later": requires_apply_later,
                "corrected_text": "",
                "rationale": rationale,
            }
        )

    if apply_later != 0:
        raise SystemExit(f"requires_apply_later must be 0, got {apply_later}")

    dominant, dominant_count = decision_counts.most_common(1)[0] if decision_counts else ("none", 0)
    if dominant_count >= 10:
        next_prompt = "chat_exec_select_cstring_player_target_direct_policy_review_prompt.md" if dominant == "needs_player_target_direct_policy" else "chat_exec_select_cstring_local_player_perspective_review_prompt.md"
        stop_rule = f"continue_narrow_prompt: {dominant} reached {dominant_count} >= 10"
    else:
        next_prompt = "chat_exec_select_cstring_possessive_policy_review_prompt.md"
        stop_rule = "stop_branch: no perspective subdecision reached >= 10; return to possessive or ES helper"

    txt_path, jsonl_path, spec_path = output_paths()

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({
            "record_type": "summary",
            "source_decision": SOURCE_DECISION,
            "total_reviewed": len(results),
            "pending_count": pending_count,
            "decision_counts": dict(decision_counts),
            "select_cstring_marker_counts": dict(select_counts),
            "perspective_marker_counts": dict(perspective_counts),
            "local_player_marker_counts": dict(local_counts),
            "secondary_marker_counts": dict(secondary_counts),
            "ready_lifecycle_future": lifecycle_later,
            "apply_candidates_future": apply_later,
            "dominant_subtype": dominant,
            "dominant_count": dominant_count,
            "stop_rule": stop_rule,
            "next_prompt": next_prompt,
        }, ensure_ascii=False, sort_keys=True) + "\n")
        for decision, count in decision_counts.most_common():
            handle.write(json.dumps({"record_type": "decision_count", "perspective_decision": decision, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in select_counts.most_common():
            handle.write(json.dumps({"record_type": "select_cstring_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in perspective_counts.most_common():
            handle.write(json.dumps({"record_type": "perspective_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in local_counts.most_common():
            handle.write(json.dumps({"record_type": "local_player_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in secondary_counts.most_common():
            handle.write(json.dumps({"record_type": "secondary_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for priority, (prompt, rationale) in enumerate([
            (next_prompt, "selected by stop-rule and dominant perspective subtype"),
            ("chat_exec_select_cstring_possessive_policy_review_prompt.md", "larger sibling route if perspective branch should stop"),
            ("chat_exec_select_cstring_es_helper_policy_review_prompt.md", "second sibling route with sampled volume"),
        ], 1):
            handle.write(json.dumps({"record_type": "strategy", "priority": priority, "next_prompt": prompt, "rationale": rationale}, ensure_ascii=False, sort_keys=True) + "\n")
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")

    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(build_spec(args.segment_state_run_id, decision_counts, select_counts, perspective_counts, local_counts, secondary_counts, stop_rule), handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Select_CString player/target perspective review\n\n")
        handle.write(f"total_revisado: {len(results)}\n")
        handle.write(f"pending_count: {pending_count}\n")
        handle.write(f"ready_lifecycle_future: {lifecycle_later}\n")
        handle.write(f"apply_candidates_future: {apply_later}\n")
        handle.write(f"subtipo_dominante: {dominant}\n")
        handle.write(f"dominant_count: {dominant_count}\n")
        handle.write(f"regra_de_parada: {stop_rule}\n\n")
        handle.write("perspective_decision_counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nTop Select_CString markers:\n")
        for marker, count in select_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop perspective markers:\n")
        for marker, count in perspective_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop local-player markers:\n")
        for marker, count in local_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop secondary markers:\n")
        for marker, count in secondary_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nAnalise\n")
        handle.write("- select_cstring_player_target_perspective_policy deve virar componente read-only real apenas se mantiver threshold >= 10.\n")
        handle.write("- Lifecycle/apply curto: nao; zero candidatos nesta revisao.\n")
        handle.write(f"- Proximo prompt recomendado: {next_prompt}.\n")
        handle.write("- Select_CString deve resolver perspectiva antes do parser generico quando a decisao e player/target direta.\n")

    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"total_reviewed: {len(results)}")
    print(f"pending_count: {pending_count}")
    print(f"ready_lifecycle_future: {lifecycle_later}")
    print(f"apply_candidates_future: {apply_later}")
    print(f"stop_rule: {stop_rule}")
    print("decision_counts:")
    for decision, count in decision_counts.most_common():
        print(f"  {decision}: {count}")


if __name__ == "__main__":
    main()
