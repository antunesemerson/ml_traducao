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


SOURCE_DECISION = "gender_policy_needs_select_cstring_player_resolution"

ALLOWED_DECISIONS = {
    "select_cstring_player_ready_false_reopen",
    "select_cstring_player_ready_lifecycle",
    "select_cstring_needs_local_player_perspective",
    "select_cstring_needs_player_vs_target_perspective",
    "select_cstring_needs_actor_target_resolution",
    "select_cstring_needs_recipient_resolution",
    "select_cstring_needs_custom_loc_scope_parser",
    "select_cstring_needs_name_nickname_guard",
    "select_cstring_needs_es_helper_resolution",
    "select_cstring_needs_possessive_resolution",
    "select_cstring_needs_event_context",
    "select_cstring_needs_domain_context",
    "select_cstring_needs_residual_repair_after_resolution",
    "select_cstring_blocked_uncertain",
}

SELECT_RE = re.compile(r"Select_CString|SelectLocalization", re.I)
LOCAL_PLAYER_RE = re.compile(r"IsLocalPlayer|GetPlayer|LocalPlayer|GetLocalPlayer", re.I)
PLAYER_VS_TARGET_RE = re.compile(r"Or\([^)]*IsLocalPlayer|And\([^)]*IsLocalPlayer|CHARACTER\.IsLocalPlayer|TARGET_CHARACTER\.IsLocalPlayer", re.I)
ACTOR_TARGET_RE = re.compile(r"\bCHARACTER\.|\bTARGET_CHARACTER\.|\bROOT\.|\bFROM\.|\bTARGET\.|\bSCOPE\.|\bactor\b|\btarget\b", re.I)
RECIPIENT_RE = re.compile(r"recipient|addressee|destinat[aá]rio|para você|para ti", re.I)
CUSTOM_SCOPE_RE = re.compile(r"Custom\(|Get[A-Za-z0-9_]+\(|PROVINCE\.|SCOPE\.|ROOT\.|TARGET\.|CHARACTER\.", re.I)
ES_HELPER_RE = re.compile(r"ES_(?:OA|XA|EA|ElLa|DelDela|AlAla|A|O)|IsFemale|IsMale", re.I)
POSSESSIVE_RE = re.compile(r"\bseu personagem\b|\bseu\b|\bsua\b|\bseus\b|\bsuas\b|\bdele\b|\bdela\b|\bvocê\b|\bteu\b|\btua\b", re.I)
NAME_RE = re.compile(r"GetShortUIName|GetName|GetDynasty|nickname|nick_|dynasty|house|nome|casa", re.I)
EVENT_RE = re.compile(r"event|\.desc|desc\.|option|coronation|lover|friend|gift|journey|activity|interaction|story|memory", re.I)
DOMAIN_RE = re.compile(r"religion|faith|culture|tradition|doctrine|artifact|title|law|government|contract|war|coroa[cç][aã]o", re.I)
RESIDUAL_RE = re.compile(r"\b(sentisteis|sintieron|buena mujer|buen hombre|se agradecem|the|your|you|their|has|will|cannot)\b|NÃ|Ãƒ|Â", re.I)
TOKEN_RE = re.compile(r"\[[^\]]+\]|\$[^$]+\$|Custom\(|Select_CString|SelectLocalization|Concept\(|ScriptValue|Get[A-Za-z0-9_]+\(", re.I)


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


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def source_rows(path: Path) -> list[dict[str, Any]]:
    rows = [row for row in read_jsonl(path) if row.get("record_type") == "sample_review" and row.get("gender_policy_decision") == SOURCE_DECISION]
    seen: set[int] = set()
    for row in rows:
        segment_id = int(row["segment_id"])
        if segment_id in seen:
            raise SystemExit(f"duplicate source segment_id: {segment_id}")
        seen.add(segment_id)
    return rows


def fetch_state_family(conn: sqlite3.Connection, run_id: int, ledger_run_id: int, ids: list[int]) -> tuple[dict[int, dict[str, Any]], dict[int, tuple[str, ...]]]:
    if not ids:
        return {}, {}
    ph = ",".join("?" for _ in ids)
    states = {
        int(row["segment_id"]): dict(row)
        for row in conn.execute(
            f"""
            SELECT segment_id, state_group, is_closed, needs_output_apply, confirmed_matches_output
            FROM segment_state_items
            WHERE run_id = ? AND segment_id IN ({ph})
            """,
            (run_id, *ids),
        )
    }
    fams: dict[int, set[str]] = {}
    for row in conn.execute(
        f"""
        SELECT segment_id, issue_family
        FROM ml_issue_ledger_items
        WHERE run_id = ? AND status = 'open' AND segment_id IN ({ph})
        """,
        (ledger_run_id, *ids),
    ):
        fams.setdefault(int(row["segment_id"]), set()).add(str(row["issue_family"]))
    return states, {sid: tuple(sorted(values)) for sid, values in fams.items()}


def state_ready(state: dict[str, Any] | None) -> bool:
    return bool(
        state
        and state.get("state_group") == "pending"
        and int(state.get("is_closed") or 0) == 0
        and int(state.get("needs_output_apply") or 0) == 0
        and int(state.get("confirmed_matches_output") or 0) == 1
    )


def markers(text: str, mapping: dict[str, re.Pattern[str]]) -> list[str]:
    return [name for name, pattern in mapping.items() if pattern.search(text)]


def classify(row: dict[str, Any], state: dict[str, Any] | None) -> tuple[str, str, str]:
    old = str(row.get("old_text") or "")
    confirmed = str(row.get("confirmed_text") or "")
    output = str(row.get("output_text") or "")
    blob = " ".join([str(row.get("relative_path") or ""), str(row.get("source_key") or ""), old, confirmed, output])

    if not state_ready(state):
        return "select_cstring_blocked_uncertain", "state guard failed in selected segment-state run", "blocked_uncertain"
    if RESIDUAL_RE.search(blob):
        return "select_cstring_needs_residual_repair_after_resolution", "visible residual must wait until player perspective is resolved", "residual_after_resolution"
    if PLAYER_VS_TARGET_RE.search(blob):
        return "select_cstring_needs_player_vs_target_perspective", "Select_CString compares local player with target/character", "player_vs_target"
    if LOCAL_PLAYER_RE.search(blob):
        return "select_cstring_needs_local_player_perspective", "local-player branch controls person/perspective", "local_player_perspective"
    if POSSESSIVE_RE.search(output):
        return "select_cstring_needs_possessive_resolution", "possessive/pronoun surface depends on player perspective", "possessive_resolution"
    if RECIPIENT_RE.search(blob):
        return "select_cstring_needs_recipient_resolution", "recipient/addressee perspective is explicit", "recipient_resolution"
    if ES_HELPER_RE.search(blob):
        return "select_cstring_needs_es_helper_resolution", "gender helper or IsFemale/IsMale branch remains", "es_helper_resolution"
    if NAME_RE.search(blob):
        return "select_cstring_needs_name_nickname_guard", "dynamic name/nickname guard is needed", "name_nickname_guard"
    if REQUIRE_CUSTOM_SCOPE(blob):
        return "select_cstring_needs_custom_loc_scope_parser", "CustomLoc/scope/getter parsing is still needed", "custom_scope_parser"
    if EVENT_RE.search(blob):
        return "select_cstring_needs_event_context", "event/narrative context is needed", "event_context"
    if DOMAIN_RE.search(blob):
        return "select_cstring_needs_domain_context", "domain context is needed", "domain_context"
    if ACTOR_TARGET_RE.search(blob):
        return "select_cstring_needs_actor_target_resolution", "actor/target/root scope remains ambiguous", "actor_target_resolution"
    if confirmed and output and confirmed == output and not TOKEN_RE.search(blob):
        return "select_cstring_player_ready_lifecycle", "confirmed/output aligned and no unresolved token remains", "ready_lifecycle"
    return "select_cstring_blocked_uncertain", "no safe Select_CString resolution substage matched", "blocked_uncertain"


def REQUIRE_CUSTOM_SCOPE(blob: str) -> bool:
    return bool(CUSTOM_SCOPE_RE.search(blob))


def build_spec(run_id: int, ledger_run_id: int, decision_counts: Counter[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_for": "read_only_policy_stage_design",
        "parent_policy": "gender_local_player_policy",
        "stage_id": "resolve_select_cstring_player",
        "segment_state_run_id": run_id,
        "ledger_run_id": ledger_run_id,
        "entry_conditions": ["Select_CString/SelectLocalization", "GetPlayer/LocalPlayer/IsLocalPlayer", "gender_policy_needs_select_cstring_player_resolution"],
        "resolution_order": [
            "block visible residual until perspective is resolved",
            "resolve player_vs_target and IsLocalPlayer branches",
            "resolve direct LocalPlayer/GetPlayer perspective",
            "resolve possessive surface",
            "route ES helper branches",
            "route name/custom-scope/event/domain guards",
        ],
        "next_components": [
            "gender_local_player_es_helper_resolution",
            "local_player_possessive_resolution",
            "actor_target_recipient_policy",
            "custom_loc_scope_parser",
            "event_context_composer",
            "domain_context_composer",
            "residual_dependency_filtered_repair",
        ],
        "blocked_conditions": ["visible residual", "actor/target ambiguity", "confirmed/output mismatch", "unparsed CustomLoc/scope stack"],
        "promotion_gate": "read-only validation, zero false-safe sample, no apply until protected prompt",
        "observed_decision_counts": dict(decision_counts),
    }


def output_paths() -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_gender_local_player_select_cstring_resolution_review"
    spec = reports_dir / f"{stamp}_gender_local_player_select_cstring_resolution_spec.json"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), spec


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Select_CString local-player resolution review.")
    parser.add_argument("--gender-jsonl", required=True, type=Path)
    parser.add_argument("--segment-state-run-id", required=True, type=int)
    parser.add_argument("--ledger-run-id", type=int, default=76)
    args = parser.parse_args()

    source = source_rows(args.gender_jsonl)
    ids = [int(row["segment_id"]) for row in source]
    conn = connect_readonly()
    states, families = fetch_state_family(conn, args.segment_state_run_id, args.ledger_run_id, ids)
    results: list[dict[str, Any]] = []
    for row in source:
        segment_id = int(row["segment_id"])
        decision, rationale, stage = classify(row, states.get(segment_id))
        if decision not in ALLOWED_DECISIONS:
            raise SystemExit(f"invalid decision: {decision}")
        blob = " ".join(str(row.get(k) or "") for k in ("old_text", "confirmed_text", "output_text", "relative_path", "source_key"))
        results.append(
            {
                "record_type": "sample_review",
                "segment_id": segment_id,
                "relative_path": row.get("relative_path") or "",
                "source_key": row.get("source_key") or "",
                "families_open": list(families.get(segment_id, ())),
                "cohort_key": row.get("cohort_key") or "",
                "old_text": row.get("old_text") or "",
                "confirmed_text": row.get("confirmed_text") or "",
                "output_text": row.get("output_text") or "",
                "select_cstring_markers": markers(blob, {"Select_CString": SELECT_RE, "IsLocalPlayer": LOCAL_PLAYER_RE}),
                "local_player_markers": markers(blob, {"LocalPlayer": LOCAL_PLAYER_RE, "possessive": POSSESSIVE_RE}),
                "scope_markers": markers(blob, {"actor_target": ACTOR_TARGET_RE, "recipient": RECIPIENT_RE, "custom_scope": CUSTOM_SCOPE_RE}),
                "select_cstring_decision": decision,
                "next_component": stage,
                "requires_lifecycle_later": decision in {"select_cstring_player_ready_false_reopen", "select_cstring_player_ready_lifecycle"},
                "requires_apply_later": False,
                "corrected_text": "",
                "rationale": rationale,
            }
        )
    if len(results) != 64:
        raise SystemExit(f"expected 64 rows, got {len(results)}")
    if len({row["segment_id"] for row in results}) != len(results):
        raise SystemExit("duplicate segment_id in results")
    if any(row["requires_apply_later"] for row in results):
        raise SystemExit("unexpected apply candidate")

    decision_counts = Counter(row["select_cstring_decision"] for row in results)
    marker_counts = Counter(marker for row in results for marker in row["select_cstring_markers"] + row["local_player_markers"] + row["scope_markers"])
    txt_path, jsonl_path, spec_path = output_paths()
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"record_type": "summary", "total_reviewed": len(results), "decision_counts": dict(decision_counts), "marker_counts": dict(marker_counts)}, ensure_ascii=False, sort_keys=True) + "\n")
        for row in results:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    with txt_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("Gender/local-player Select_CString resolution review\n\n")
        handle.write(f"total_reviewed: {len(results)}\n")
        handle.write(f"ready_lifecycle_future: {sum(1 for row in results if row['requires_lifecycle_later'])}\n")
        handle.write(f"future_apply_candidates: {sum(1 for row in results if row['requires_apply_later'])}\n")
        handle.write("select_cstring_decision_counts:\n")
        for decision, count in decision_counts.most_common():
            handle.write(f"- {decision}: {count}\n")
        handle.write("\nmarker_counts:\n")
        for marker, count in marker_counts.most_common():
            handle.write(f"- {marker}: {count}\n")
        handle.write("\nAnalise\n")
        handle.write("- resolve_select_cstring_player deve virar substage read-only real.\n")
        handle.write("- Nao gera lifecycle/apply no curto prazo; e roteador de perspectiva e guards.\n")
        handle.write("- Proximo substage recomendado: ES helpers, seguido de possessive/local-player e actor-target.\n")
        handle.write("- Isso reforca ajuste de prioridade do router: gender/local-player antes do parser generico em Select_CString explicito.\n")
        handle.write("\nProximos prompts\n")
        handle.write("1. chat_exec_gender_local_player_es_helper_resolution_prompt.md\n")
        handle.write("2. chat_exec_gender_local_player_possessive_resolution_prompt.md\n")
        handle.write("3. chat_exec_parser_backed_dynamic_expression_design_prompt.md\n")
    spec = build_spec(args.segment_state_run_id, args.ledger_run_id, decision_counts)
    with spec_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    print(f"txt: {txt_path}")
    print(f"jsonl: {jsonl_path}")
    print(f"spec: {spec_path}")
    print(f"total_reviewed: {len(results)}")
    print("select_cstring_decision_counts:")
    for decision, count in decision_counts.most_common():
        print(f"  {decision}: {count}")


if __name__ == "__main__":
    main()
