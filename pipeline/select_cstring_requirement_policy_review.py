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
SOURCE_DECISION = "needs_select_cstring_requirement_policy"

ALLOWED_DECISIONS = {
    "select_cstring_requirement_ready_false_reopen",
    "select_cstring_requirement_ready_lifecycle",
    "needs_select_cstring_player_target_perspective_policy",
    "needs_select_cstring_local_player_policy",
    "needs_select_cstring_actor_target_policy",
    "needs_select_cstring_es_helper_policy",
    "needs_select_cstring_possessive_policy",
    "needs_select_cstring_custom_loc_policy",
    "needs_select_cstring_scope_getter_policy",
    "needs_select_cstring_script_value_policy",
    "needs_select_cstring_effect_list_policy",
    "needs_select_cstring_domain_context",
    "needs_select_cstring_event_context",
    "needs_select_cstring_residual_repair",
    "needs_select_cstring_dynamic_parser_after_policy",
    "select_cstring_requirement_blocked_uncertain",
}

SELECT_CSTRING_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("SelectCString", re.compile(r"Select_CString\s*\(", re.I)),
    ("SelectCStringIsLocalPlayer", re.compile(r"Select_CString\s*\([^)]*IsLocalPlayer", re.I)),
    ("SelectCStringSecondPerson", re.compile(r"Select_CString\s*\([^)]*'(?:voce|voc.|te |te\b|tu\b|teu|tua|seu personagem|amas|detestas|recebes|conquistar)", re.I)),
    ("SelectCStringShortUIName", re.compile(r"Select_CString\s*\([^)]*GetShortUIName", re.I)),
    ("SelectCStringSameChoice", re.compile(r"Select_CString\s*\([^)]*'([^']+)'\s*,\s*'\1'", re.I)),
]

LOCAL_PLAYER_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("IsLocalPlayer", re.compile(r"IsLocalPlayer|CHARACTER\.IsLocalPlayer", re.I)),
    ("GetPlayer", re.compile(r"GetPlayer|GetLocalPlayer|local_player", re.I)),
    ("SecondPerson", re.compile(r"\bvoce\b|\bvoc.\b|\btu\b|\bteu\b|\btua\b|\bseu personagem\b|\bamas\b|\bdetestas\b|\brecebes\b|\bconquistar", re.I)),
]

GENDER_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("ES_ElLa", re.compile(r"ES_ElLa", re.I)),
    ("ES_DelDela", re.compile(r"ES_DelDela", re.I)),
    ("ES_OA", re.compile(r"ES_OA", re.I)),
    ("ES_XA_EA", re.compile(r"ES_XA_EA|ES_XA|ES_EA", re.I)),
    ("GenderAgreement", re.compile(r"(?:adotad|temid|conhecid|nascid|chamad)\[[^\]]*ES_OA|Custom\('ES_", re.I)),
]

REQUIREMENT_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("Tooltip", re.compile(r"tooltip|_tt\b|#T\b", re.I)),
    ("Requirement", re.compile(r"requirement|required|trigger|valid|allowed|cannot|can_|unlock|available|need|must|requisito", re.I)),
    ("Condition", re.compile(r"NO_CHANCE|invalid|valid|blocked|disabled|missing|has_|is_|not_", re.I)),
]

SECONDARY_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("ActorTarget", re.compile(r"\bactor\b|\btarget\b|\brecipient\b|\baddressee\b|ROOT\.|FROM\.|TARGET_|TARGET\.|CHARACTER\.|guardian\.|beneficiary\.", re.I)),
    ("PlayerTargetPerspective", re.compile(r"IsLocalPlayer[^)]*GetShortUIName|GetShortUIName[^)]*IsLocalPlayer|TARGET_CHARACTER|guardian\.GetShortUIName|beneficiary\.GetShortUIName", re.I)),
    ("Possessive", re.compile(r"\bseu\b|\bsua\b|\bseus\b|\bsuas\b|\bdele\b|\bdela\b|\bteu\b|\btua\b|\bvosso\b|\bvossa\b", re.I)),
    ("CustomLoc", re.compile(r"CustomLoc|Custom2?\((?!'ES_)", re.I)),
    ("ScopeGetter", re.compile(r"ROOT\.|FROM\.|SCOPE\.|TARGET\.|CHARACTER\.|Get[A-Za-z0-9_]+\(", re.I)),
    ("ScriptValue", re.compile(r"ScriptValue|GetScriptValue|\|V[0-9]?|\|=\+?0|[0-9]+%", re.I)),
    ("EffectList", re.compile(r"\\n|\n|\$EFFECT_LIST_BULLET\$|#indent|#weak|#bold|#high|#low|#P|#N", re.I)),
    ("Domain", re.compile(r"culture|religion|faith|doctrine|tradition|dynasty|house|domain|province|building|accolade", re.I)),
    ("Event", re.compile(r"event|\.desc|desc\.|option|toast|dialogue|story|scheme|interaction|memory|battle|activities/", re.I)),
    ("ResidualVisible", re.compile(r"Ãƒ|Ã‚|Â¿|Â¡|â€™|â€œ|â€�|�|\b(?:the|your|you|their|cannot|consiguio|sentisteis|sintieron)\b", re.I)),
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
        and row.get("gender_requirement_decision") == SOURCE_DECISION
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
    normalized = (
        blob.replace("você", "voce")
        .replace("Você", "Voce")
        .replace("conquistarás", "conquistaras")
    )
    return [label for label, pattern in patterns if pattern.search(normalized)]


def classify(
    state: dict[str, Any] | None,
    select_markers: list[str],
    local_markers: list[str],
    gender_markers: list[str],
    requirement_markers: list[str],
    secondary_markers: list[str],
) -> tuple[str, str, bool, str]:
    select = set(select_markers)
    local = set(local_markers)
    gender = set(gender_markers)
    req = set(requirement_markers)
    secondary = set(secondary_markers)
    if not state or state["state_group"] != "pending" or int(state["is_closed"] or 0) != 0:
        return "select_cstring_requirement_blocked_uncertain", "human_review_or_evidence_collection", False, "segment is not pending in selected state run"
    if int(state["needs_output_apply"] or 0) != 0 or int(state["confirmed_matches_output"] or 0) != 1:
        return "select_cstring_requirement_blocked_uncertain", "state_guard", False, "state guard failed: needs_output_apply or confirmed_matches_output"
    if "ResidualVisible" in secondary:
        return "needs_select_cstring_residual_repair", "residual_dependency_filtered_repair", False, "visible residual/mojibake remains"
    if gender:
        return "needs_select_cstring_es_helper_policy", "select_cstring_es_helper_policy", False, "Select_CString is mixed with ES/gender helper"
    if "Possessive" in secondary:
        return "needs_select_cstring_possessive_policy", "select_cstring_possessive_policy", False, "Select_CString controls possessive perspective"
    if "CustomLoc" in secondary:
        return "needs_select_cstring_custom_loc_policy", "select_cstring_custom_loc_policy", False, "Select_CString is mixed with non-ES CustomLoc"
    if "PlayerTargetPerspective" in secondary or "SelectCStringSecondPerson" in select or "SelectCStringShortUIName" in select:
        return "needs_select_cstring_player_target_perspective_policy", "select_cstring_player_target_perspective_policy", False, "Select_CString chooses player-facing second person versus target/name"
    if local:
        return "needs_select_cstring_local_player_policy", "select_cstring_local_player_policy", False, "Select_CString uses LocalPlayer/GetPlayer/IsLocalPlayer"
    if "ActorTarget" in secondary:
        return "needs_select_cstring_actor_target_policy", "select_cstring_actor_target_policy", False, "actor/target/recipient perspective remains"
    if "ScriptValue" in secondary:
        return "needs_select_cstring_script_value_policy", "select_cstring_script_value_policy", False, "ScriptValue/numeric dependency remains"
    if "EffectList" in secondary:
        return "needs_select_cstring_effect_list_policy", "select_cstring_effect_list_policy", False, "effect-list/multiline dependency remains"
    if "ScopeGetter" in secondary:
        return "needs_select_cstring_scope_getter_policy", "select_cstring_scope_getter_policy", False, "scope/getter dependency remains"
    if "Domain" in secondary:
        return "needs_select_cstring_domain_context", "domain_context_composer", False, "domain context remains"
    if "Event" in secondary:
        return "needs_select_cstring_event_context", "event_context_composer", False, "event context remains"
    if "DynamicToken" in secondary:
        return "needs_select_cstring_dynamic_parser_after_policy", "ck3_dynamic_symbolic_parser", False, "dynamic token remains after Select_CString routing"
    if req and int(state["confirmed_matches_output"] or 0) == 1:
        if int(state["needs_reopen"] or 0) == 1:
            return "select_cstring_requirement_ready_false_reopen", "false_reopen_lifecycle_bridge", True, "plain Select_CString requirement may be future false-reopen candidate"
        return "select_cstring_requirement_ready_lifecycle", "select_cstring_requirement_lifecycle_bridge", True, "plain Select_CString requirement may be future lifecycle candidate"
    return "select_cstring_requirement_blocked_uncertain", "human_review_or_evidence_collection", False, "insufficient Select_CString marker evidence"


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_select_cstring_requirement_policy_review"
    spec = reports_dir / f"{stamp}_select_cstring_requirement_policy_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def build_spec(
    run_id: int,
    decisions: Counter[str],
    select_counts: Counter[str],
    local_counts: Counter[str],
    gender_counts: Counter[str],
    secondary_counts: Counter[str],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_for": "read_only_subpolicy_design",
        "parent_policy": "gender_local_player_requirement_policy",
        "policy_id": "select_cstring_requirement_policy",
        "segment_state_run_id": run_id,
        "ledger_run_id": LEDGER_RUN_ID,
        "entry_conditions": [
            "gender_requirement_decision == needs_select_cstring_requirement_policy",
            "segment remains pending",
            "needs_output_apply == 0",
            "confirmed_matches_output == 1",
        ],
        "select_cstring_types": [{"decision": decision, "sampled": count} for decision, count in decisions.most_common()],
        "resolution_order": [
            "state guard",
            "residual visible guard",
            "ES helper/gender agreement mixed with Select_CString",
            "possessive and CustomLoc composition",
            "player/target perspective",
            "local-player and actor/target perspective",
            "scope/getter, ScriptValue, effect-list, domain/event",
            "dynamic parser handoff",
        ],
        "next_components": [
            "select_cstring_player_target_perspective_policy",
            "select_cstring_local_player_policy",
            "select_cstring_actor_target_policy",
            "select_cstring_es_helper_policy",
            "select_cstring_possessive_policy",
            "select_cstring_custom_loc_policy",
            "select_cstring_scope_getter_policy",
            "domain_context_composer",
            "event_context_composer",
            "ck3_dynamic_symbolic_parser",
        ],
        "blocked_conditions": [
            "state guard failed",
            "visible residual/mojibake",
            "ambiguous player/target perspective",
            "missing Select_CString marker evidence",
        ],
        "promotion_gate": "Read-only component only; lifecycle/apply requires separate guarded prompt.",
        "observed_select_cstring_marker_counts": dict(select_counts),
        "observed_local_player_marker_counts": dict(local_counts),
        "observed_gender_marker_counts": dict(gender_counts),
        "observed_secondary_marker_counts": dict(secondary_counts),
    }


def next_prompt_for(dominant: str) -> str:
    if dominant == "needs_select_cstring_player_target_perspective_policy":
        return "chat_exec_select_cstring_player_target_perspective_review_prompt.md"
    if dominant == "needs_select_cstring_local_player_policy":
        return "chat_exec_select_cstring_local_player_policy_review_prompt.md"
    if dominant == "needs_select_cstring_actor_target_policy":
        return "chat_exec_select_cstring_actor_target_policy_review_prompt.md"
    if dominant == "needs_select_cstring_es_helper_policy":
        return "chat_exec_select_cstring_es_helper_policy_review_prompt.md"
    if dominant == "needs_select_cstring_possessive_policy":
        return "chat_exec_select_cstring_possessive_policy_review_prompt.md"
    if dominant == "needs_select_cstring_custom_loc_policy":
        return "chat_exec_select_cstring_custom_loc_policy_review_prompt.md"
    if dominant == "needs_select_cstring_scope_getter_policy":
        return "chat_exec_select_cstring_scope_getter_policy_review_prompt.md"
    return "chat_exec_requirement_effect_list_multiline_subpolicy_review_prompt.md"


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Select_CString requirement policy review.")
    parser.add_argument("--gender-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    args = parser.parse_args()

    rows = source_rows(args.gender_jsonl)
    conn = connect_readonly()
    states = fetch_states(conn, args.segment_state_run_id, [int(row["segment_id"]) for row in rows])

    results: list[dict[str, Any]] = []
    decision_counts: Counter[str] = Counter()
    select_counts: Counter[str] = Counter()
    local_counts: Counter[str] = Counter()
    gender_counts: Counter[str] = Counter()
    req_counts: Counter[str] = Counter()
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
        local_player_markers = markers(LOCAL_PLAYER_MARKERS, blob)
        gender_markers = markers(GENDER_MARKERS, blob)
        requirement_markers = markers(REQUIREMENT_MARKERS, blob)
        secondary_markers = markers(SECONDARY_MARKERS, blob)
        decision, component, lifecycle, rationale = classify(
            state,
            select_markers,
            local_player_markers,
            gender_markers,
            requirement_markers,
            secondary_markers,
        )
        if decision not in ALLOWED_DECISIONS:
            raise SystemExit(f"unknown decision {decision} for segment_id {segment_id}")
        requires_apply_later = False
        lifecycle_later += int(lifecycle)
        apply_later += int(requires_apply_later)
        decision_counts[decision] += 1
        select_counts.update(select_markers or ["NoSelectCStringMarker"])
        local_counts.update(local_player_markers or ["NoLocalPlayerMarker"])
        gender_counts.update(gender_markers or ["NoGenderMarker"])
        req_counts.update(requirement_markers or ["NoRequirementMarker"])
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
                "local_player_markers": local_player_markers,
                "gender_markers": gender_markers,
                "requirement_markers": requirement_markers,
                "secondary_markers": secondary_markers,
                "select_cstring_requirement_decision": decision,
                "next_component": component,
                "requires_lifecycle_later": lifecycle,
                "requires_apply_later": requires_apply_later,
                "corrected_text": "",
                "rationale": rationale,
            }
        )

    if apply_later != 0:
        raise SystemExit(f"requires_apply_later must be 0, got {apply_later}")

    dominant = decision_counts.most_common(1)[0][0] if decision_counts else "none"
    next_prompt = next_prompt_for(dominant)
    txt_path, jsonl_path, spec_path = output_paths()

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({
            "record_type": "summary",
            "source_decision": SOURCE_DECISION,
            "total_reviewed": len(results),
            "pending_count": pending_count,
            "decision_counts": dict(decision_counts),
            "select_cstring_marker_counts": dict(select_counts),
            "local_player_marker_counts": dict(local_counts),
            "gender_marker_counts": dict(gender_counts),
            "requirement_marker_counts": dict(req_counts),
            "secondary_marker_counts": dict(secondary_counts),
            "ready_lifecycle_future": lifecycle_later,
            "apply_candidates_future": apply_later,
            "dominant_subtype": dominant,
            "next_prompt": next_prompt,
        }, ensure_ascii=False, sort_keys=True) + "\n")
        for decision, count in decision_counts.most_common():
            handle.write(json.dumps({"record_type": "decision_count", "select_cstring_requirement_decision": decision, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in select_counts.most_common():
            handle.write(json.dumps({"record_type": "select_cstring_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in local_counts.most_common():
            handle.write(json.dumps({"record_type": "local_player_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in gender_counts.most_common():
            handle.write(json.dumps({"record_type": "gender_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in req_counts.most_common():
            handle.write(json.dumps({"record_type": "requirement_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for marker, count in secondary_counts.most_common():
            handle.write(json.dumps({"record_type": "secondary_marker_count", "marker": marker, "segments": count}, ensure_ascii=False, sort_keys=True) + "\n")
        for priority, (prompt, rationale) in enumerate([
            (next_prompt, "dominant subtype from Select_CString requirement review"),
            ("chat_exec_select_cstring_es_helper_policy_review_prompt.md", "helper branch if mixed gender agreement is selected next"),
            ("chat_exec_requirement_effect_list_multiline_subpolicy_review_prompt.md", "larger sibling route if Select_CString branch fragments"),
        ], 1):
            handle.write(json.dumps({"record_type": "strategy", "priority": priority, "next_prompt": prompt, "rationale": rationale}, ensure_ascii=False, sort_keys=True) + "\n")
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")

    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(build_spec(args.segment_state_run_id, decision_counts, select_counts, local_counts, gender_counts, secondary_counts), handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Select_CString requirement policy review\n\n")
        handle.write(f"total_revisado: {len(results)}\n")
        handle.write(f"pending_count: {pending_count}\n")
        handle.write(f"ready_lifecycle_future: {lifecycle_later}\n")
        handle.write(f"apply_candidates_future: {apply_later}\n")
        handle.write(f"subtipo_dominante: {dominant}\n\n")
        handle.write("select_cstring_requirement_decision_counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nTop Select_CString markers:\n")
        for marker, count in select_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop local-player markers:\n")
        for marker, count in local_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop gender markers:\n")
        for marker, count in gender_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop requirement markers:\n")
        for marker, count in req_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nTop secondary markers:\n")
        for marker, count in secondary_counts.most_common(15):
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nAnalise\n")
        handle.write("- select_cstring_requirement_policy deve virar componente read-only real antes do parser generico.\n")
        handle.write("- Lifecycle/apply curto: nao; zero candidatos nesta revisao.\n")
        handle.write(f"- Proximo prompt recomendado: {next_prompt}.\n")
        handle.write("- Select_CString confirma override de gender/local-player antes do parser generico em requisitos/tooltips.\n")

    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"total_reviewed: {len(results)}")
    print(f"pending_count: {pending_count}")
    print(f"ready_lifecycle_future: {lifecycle_later}")
    print(f"apply_candidates_future: {apply_later}")
    print("decision_counts:")
    for decision, count in decision_counts.most_common():
        print(f"  {decision}: {count}")


if __name__ == "__main__":
    main()
