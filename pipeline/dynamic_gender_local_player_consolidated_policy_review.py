from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


ALLOWED_DECISIONS = {
    "dynamic_gender_local_player_ready_false_reopen",
    "dynamic_gender_local_player_ready_lifecycle",
    "needs_local_player_pronoun_possessive_policy",
    "needs_local_player_vs_target_policy",
    "needs_local_player_option_tooltip_policy",
    "needs_local_player_event_context_composer",
    "needs_local_player_subject_object_composer",
    "needs_local_player_select_cstring_policy",
    "needs_local_player_es_oa_policy",
    "needs_local_player_custom_loc_gender_policy",
    "needs_local_player_residual_repair",
    "needs_new_microagent",
    "blocked_uncertain",
}

SOURCE_DECISIONS = {
    "needs_local_player_pronoun_possessive_policy",
    "needs_es_oa_event_local_player_policy",
}

TOKEN_RE = re.compile(
    r"Select_CString|Custom\(|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla)|\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!",
    re.IGNORECASE,
)
SELECT_RE = re.compile(r"Select_CString\s*\(", re.IGNORECASE)
ES_OA_RE = re.compile(r"Custom\(\s*['\"]ES_OA['\"]\s*\)|ES_OA", re.IGNORECASE)
CUSTOM_GENDER_RE = re.compile(r"Custom\(|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla)", re.IGNORECASE)
LOCAL_PLAYER_RE = re.compile(r"IsLocalPlayer|GetPlayer|ROOT\.Char|seu personagem|sua personagem|voc", re.IGNORECASE)
TARGET_PARTICIPANT_RE = re.compile(r"\[(?:recipient|actor|host|woo_actor|local_character|target|[^.\]]+_actor|[^.\]]+_recipient)\.", re.IGNORECASE)
SUBJECT_OBJECT_RE = re.compile(r"GetSheHe|GetHerHim|GetShortUIName|GetFirstName|GetName|GetUIName", re.IGNORECASE)
OPTION_TOOLTIP_RE = re.compile(r"(?:\.tt$|\.trigger$|\.fallback$|\.average$|_tooltip|tooltip|_option|option|_log$|_key$)", re.IGNORECASE)
EVENT_LONGFORM_RE = re.compile(r'"|\.desc|desc\.|dialog|intro|yearly|interaction|travel_option|health|tournament|contest|hunt",?', re.IGNORECASE)
RESIDUAL_RE = re.compile(r"\b(?:vocę|nă|năo|săo|estăo|No#!|desconfortá|perdid|acordad|convencid)\b", re.IGNORECASE)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
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


def row_decision(row: dict[str, Any]) -> str | None:
    for key in ("local_player_decision", "es_oa_event_decision", "residual_decision"):
        value = row.get(key)
        if value in SOURCE_DECISIONS:
            return value
    return None


def collect_rows(paths: list[tuple[str, str]]) -> tuple[list[dict[str, Any]], int]:
    merged: dict[int, dict[str, Any]] = {}
    source_files: dict[int, set[str]] = defaultdict(set)
    source_decisions: dict[int, set[str]] = defaultdict(set)
    raw_total = 0

    for path_value, expected_decision in paths:
        path = db.project_path(path_value)
        for row in read_jsonl(path):
            decision = row_decision(row)
            if decision != expected_decision:
                continue
            raw_total += 1
            segment_id = int(row["segment_id"])
            if segment_id not in merged:
                merged[segment_id] = row
            source_files[segment_id].add(path_value)
            source_decisions[segment_id].add(decision)

    rows: list[dict[str, Any]] = []
    for segment_id in sorted(merged):
        row = dict(merged[segment_id])
        row["_source_files"] = sorted(source_files[segment_id])
        row["_source_decisions"] = sorted(source_decisions[segment_id])
        rows.append(row)
    return rows, raw_total


def fetch_states(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, final_state, state_group, needs_output_apply,
               confirmed_matches_output, needs_reopen, is_closed
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def state_is_pending_confirmed(state: dict[str, Any] | None) -> bool:
    if not state:
        return False
    return (
        state.get("state_group") == "pending"
        and int(state.get("needs_output_apply") or 0) == 0
        and int(state.get("confirmed_matches_output") or 0) == 1
        and int(state.get("is_closed") or 0) == 0
    )


def can_false_reopen(state: dict[str, Any] | None) -> bool:
    if not state_is_pending_confirmed(state):
        return False
    return (
        int(state.get("needs_reopen") or 0) == 1
        and state.get("final_state") == "reopen_auto_confirmed_autofix"
    )


def tokens_seen(text: str) -> list[str]:
    labels: list[str] = []
    for label, pattern in [
        ("Select_CString", SELECT_RE),
        ("ES_OA", ES_OA_RE),
        ("CustomLocGender", CUSTOM_GENDER_RE),
        ("LocalPlayer", LOCAL_PLAYER_RE),
        ("TargetParticipant", TARGET_PARTICIPANT_RE),
        ("SubjectObject", SUBJECT_OBJECT_RE),
        ("CK3DynamicToken", re.compile(r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|#!")),
    ]:
        if pattern.search(text):
            labels.append(label)
    return labels


def classify(row: dict[str, Any], state: dict[str, Any] | None) -> tuple[str, str, str]:
    text = row["current_text"]
    key = row["key"]
    haystack = " ".join([row["relative_path"], key, text])

    if not state_is_pending_confirmed(state):
        return "blocked_uncertain", "not_pending_in_segment_state", "not eligible in selected segment-state run"
    if not any(decision in SOURCE_DECISIONS for decision in row["_source_decisions"]):
        return "blocked_uncertain", "unexpected_source_decision", "source decision is outside the local-player allowlist"
    if RESIDUAL_RE.search(text):
        return "needs_local_player_residual_repair", "visible_residual_local_player", "visible residual remains tied to local-player surface"
    if SELECT_RE.search(text) and LOCAL_PLAYER_RE.search(text):
        return "needs_local_player_select_cstring_policy", "select_cstring_pronoun_possessive", "Select_CString chooses local-player possessive/name surface"
    if ES_OA_RE.search(text) and TARGET_PARTICIPANT_RE.search(text) and re.search(r"\bvoc", text, re.IGNORECASE):
        return "needs_local_player_vs_target_policy", "local_player_vs_target_participant", "text mixes second person with target/recipient gender surface"
    if ES_OA_RE.search(text) and LOCAL_PLAYER_RE.search(text):
        return "needs_local_player_es_oa_policy", "es_oa_local_player_gender", "ES_OA is applied to local player or player character"
    if CUSTOM_GENDER_RE.search(text):
        return "needs_local_player_custom_loc_gender_policy", "custom_loc_gender_local_player", "custom loc or gender helper is mixed with local-player perspective"
    if SUBJECT_OBJECT_RE.search(text):
        return "needs_local_player_subject_object_composer", "subject_object_dynamic", "dynamic subject/object token affects local-player agreement"
    if OPTION_TOOLTIP_RE.search(key):
        return "needs_local_player_option_tooltip_policy", "short_option_tooltip", "short option/tooltip needs a narrow policy"
    if len(text) > 160 or EVENT_LONGFORM_RE.search(haystack):
        return "needs_local_player_event_context_composer", "event_dialogue_or_longform", "event prose requires local-player contextual composition"
    if can_false_reopen(state):
        return "dynamic_gender_local_player_ready_false_reopen", "ready_false_reopen_clear", "short aligned false reopen candidate"
    return "dynamic_gender_local_player_ready_lifecycle", "ready_lifecycle_clear", "short aligned local-player lifecycle candidate"


def decide(row: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
    decision, subpolicy, notes = classify(row, state)
    return {
        "segment_id": int(row["segment_id"]),
        "key": row["key"],
        "relative_path": row["relative_path"],
        "current_text": row["current_text"],
        "source_files": row["_source_files"],
        "source_decisions": row["_source_decisions"],
        "local_player_decision": decision,
        "local_player_subpolicy": subpolicy,
        "tokens_seen": tokens_seen(row["current_text"]),
        "requires_lifecycle_later": decision
        in {"dynamic_gender_local_player_ready_false_reopen", "dynamic_gender_local_player_ready_lifecycle"},
        "requires_apply_later": False,
        "corrected_text": "",
        "notes": notes,
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_dynamic_gender_local_player_consolidated_policy_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def write_reports(
    rows: list[dict[str, Any]], raw_total: int, segment_state_run_id: int
) -> tuple[Path, Path, Counter[str], Counter[str]]:
    jsonl_path, txt_path = output_paths()
    decision_counts = Counter(row["local_player_decision"] for row in rows)
    subpolicy_counts = Counter(row["local_player_subpolicy"] for row in rows)
    ready_count = sum(1 for row in rows if row["requires_lifecycle_later"])
    apply_count = sum(1 for row in rows if row["requires_apply_later"])

    if ready_count >= 5:
        recommendation = "prepare_narrow_readonly_local_player_lifecycle"
    elif apply_count >= 5:
        recommendation = "prepare_separate_protected_apply_prompt"
    else:
        needs_counts = Counter({key: value for key, value in decision_counts.items() if key.startswith("needs_local_player_")})
        if needs_counts and needs_counts.most_common(1)[0][1] >= 10:
            recommendation = f"register_specific_microagent_or_policy_for_{needs_counts.most_common(1)[0][0]}_and_close_branch"
        else:
            recommendation = "close_tree_and_return_to_global_diagnostic"

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Dynamic gender local player consolidated policy review",
        "",
        f"segment_state_run_id: {segment_state_run_id}",
        f"raw_total: {raw_total}",
        f"deduplicated_total: {len(rows)}",
        "",
        "Decision counts:",
    ]
    lines.extend(f"- {key}: {value}" for key, value in sorted(decision_counts.items()))
    lines.extend(["", "Subpolicy counts:"])
    lines.extend(f"- {key}: {value}" for key, value in sorted(subpolicy_counts.items()))
    lines.extend(
        [
            "",
            f"ready_lifecycle_count: {ready_count}",
            f"future_apply_count: {apply_count}",
            f"recommendation: {recommendation}",
            "",
            "Safety: read-only review; no lifecycle, apply, segment-state, confirmations, production, reindex, training, source edits, or output edits.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path, decision_counts, subpolicy_counts


def validate_rows(rows: list[dict[str, Any]]) -> None:
    required = {
        "segment_id",
        "key",
        "relative_path",
        "current_text",
        "source_files",
        "source_decisions",
        "local_player_decision",
        "local_player_subpolicy",
        "tokens_seen",
        "requires_lifecycle_later",
        "requires_apply_later",
        "corrected_text",
        "notes",
    }
    for row in rows:
        missing = required - set(row)
        if missing:
            raise SystemExit(f"missing fields for {row.get('segment_id')}: {sorted(missing)}")
        if not set(row["source_decisions"]) <= SOURCE_DECISIONS:
            raise SystemExit(f"unexpected source decision for {row['segment_id']}: {row['source_decisions']}")
        if row["local_player_decision"] not in ALLOWED_DECISIONS:
            raise SystemExit(f"unexpected local-player decision for {row['segment_id']}: {row['local_player_decision']}")
        if row["requires_apply_later"] and not row["corrected_text"]:
            raise SystemExit(f"apply candidate without corrected_text: {row['segment_id']}")
        if row["corrected_text"] and TOKEN_RE.findall(row["current_text"]) != TOKEN_RE.findall(row["corrected_text"]):
            raise SystemExit(f"token mismatch in corrected_text: {row['segment_id']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--select-cstring-local-player-jsonl", required=True)
    parser.add_argument("--es-oa-event-jsonl", required=True)
    parser.add_argument("--es-oa-residual-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    args = parser.parse_args()

    source_rows, raw_total = collect_rows(
        [
            (args.select_cstring_local_player_jsonl, "needs_local_player_pronoun_possessive_policy"),
            (args.es_oa_event_jsonl, "needs_es_oa_event_local_player_policy"),
            (args.es_oa_residual_jsonl, "needs_es_oa_event_local_player_policy"),
        ]
    )
    segment_ids = [int(row["segment_id"]) for row in source_rows]
    with connect_readonly() as conn:
        states = fetch_states(conn, args.segment_state_run_id, segment_ids)

    reviewed = [decide(row, states.get(int(row["segment_id"]))) for row in source_rows]
    validate_rows(reviewed)
    jsonl_path, txt_path, decision_counts, subpolicy_counts = write_reports(reviewed, raw_total, args.segment_state_run_id)

    print(f"wrote_jsonl={jsonl_path}")
    print(f"wrote_txt={txt_path}")
    print(f"segment_state_run_id={args.segment_state_run_id}")
    print(f"raw_total={raw_total}")
    print(f"deduplicated_total={len(reviewed)}")
    print("decision_counts=" + json.dumps(dict(sorted(decision_counts.items())), ensure_ascii=False, sort_keys=True))
    print("subpolicy_counts=" + json.dumps(dict(sorted(subpolicy_counts.items())), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
