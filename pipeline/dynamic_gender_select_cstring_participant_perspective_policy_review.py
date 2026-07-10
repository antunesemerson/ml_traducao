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


SELECT_RE = re.compile(r"Select_CString", re.IGNORECASE)
CUSTOM_RE = re.compile(r"Custom\(|ES_(?:OA|XA|EA|ElLa|DelDela|AlAla)", re.IGNORECASE)
LOCAL_PLAYER_RE = re.compile(r"IsLocalPlayer|Seu personagem|seu personagem|player character", re.IGNORECASE)
ACTOR_TARGET_RE = re.compile(r"\b(?:actor|recipient|target|TARGET_CHARACTER|CHARACTER)\b", re.IGNORECASE)
ACTIVITY_ROLE_RE = re.compile(r"\b(?:participant|guest|host|contestant|traveler|knight|performer|hunter|hunt|activity)\b", re.IGNORECASE)
SUBJECT_OBJECT_RE = re.compile(r"GetShortUIName|GetUIName|GetHerHim|GetSheHe|TARGET_CHARACTER|CHARACTER", re.IGNORECASE)
OPTION_TOOLTIP_RE = re.compile(r"_tt$|tooltip|option|#weak|#V|@warning_icon", re.IGNORECASE)
EVENT_LONGFORM_RE = re.compile(r"event|\.desc|desc\.|_log$|_key$|flavor", re.IGNORECASE)
RESIDUAL_RE = re.compile(r"\b(?:o[ií]ste|oy[oó]|pasasteis|pasaron|lugareñ[ao]|fracasaste|descubriste)\b", re.IGNORECASE)
TOKEN_BOUNDARY_RE = re.compile(r"\w\?\w|[\[\]]{2,}|\$\s*\$")


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


def fetch_states(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, final_state, state_group, needs_output_apply, confirmed_matches_output, needs_reopen, is_closed
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def collect_source_rows(path_value: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in read_jsonl(db.project_path(path_value)):
        if row.get("activity_context_decision") != "needs_activity_select_cstring_participant_perspective_policy":
            continue
        if row.get("activity_context_subpolicy") != "participant_perspective":
            continue
        segment_id = int(row["segment_id"])
        if segment_id in seen:
            continue
        seen.add(segment_id)
        rows.append(row)
    return rows


def tokens_seen(text: str) -> list[str]:
    labels: list[str] = []
    for label, pattern in [
        ("Select_CString", SELECT_RE),
        ("CustomOrGenderLoc", CUSTOM_RE),
        ("LocalPlayer", LOCAL_PLAYER_RE),
        ("ActorTargetScope", ACTOR_TARGET_RE),
        ("ActivityRole", ACTIVITY_ROLE_RE),
        ("SubjectObject", SUBJECT_OBJECT_RE),
    ]:
        if pattern.search(text):
            labels.append(label)
    return labels


def ready_decision(row: dict[str, Any], state: dict[str, Any] | None) -> str | None:
    text = row["current_text"]
    haystack = " ".join([row["relative_path"], row["key"], text])
    if not state:
        return None
    if state.get("state_group") != "pending" or int(state.get("needs_output_apply") or 0) != 0:
        return None
    if int(state.get("confirmed_matches_output") or 0) != 1 or int(state.get("is_closed") or 0) != 0:
        return None
    if not SELECT_RE.search(text):
        return None
    if any(pattern.search(haystack) for pattern in (CUSTOM_RE, RESIDUAL_RE, LOCAL_PLAYER_RE, ACTOR_TARGET_RE, ACTIVITY_ROLE_RE, SUBJECT_OBJECT_RE)):
        return None
    if TOKEN_BOUNDARY_RE.search(text) or text.count("[") != text.count("]") or text.count("$") % 2 != 0:
        return None
    if int(state.get("needs_reopen") or 0) == 1 and state.get("final_state") == "reopen_auto_confirmed_autofix":
        return "participant_perspective_ready_false_reopen"
    if len(text) <= 120:
        return "participant_perspective_ready_lifecycle"
    return None


def policy_decision(row: dict[str, Any]) -> tuple[str, str]:
    text = row["current_text"]
    haystack = " ".join([row["relative_path"], row["key"], text])
    if RESIDUAL_RE.search(text):
        return "needs_participant_perspective_residual_repair", "visible_spanish_or_fluency_residual"
    if CUSTOM_RE.search(text):
        return "needs_participant_perspective_custom_loc_gender_policy", "custom_loc_gender"
    if OPTION_TOOLTIP_RE.search(haystack):
        return "needs_participant_perspective_option_tooltip_policy", "option_tooltip"
    if LOCAL_PLAYER_RE.search(text):
        return "needs_participant_perspective_local_player_policy", "local_player_perspective"
    if re.search(r"TARGET_CHARACTER", text):
        return "needs_participant_perspective_actor_target_policy", "actor_target_scope"
    if SUBJECT_OBJECT_RE.search(text):
        return "needs_participant_perspective_subject_object_composer", "dynamic_subject_object"
    if ACTIVITY_ROLE_RE.search(haystack):
        return "needs_participant_perspective_activity_role_policy", "activity_role"
    if EVENT_LONGFORM_RE.search(haystack):
        return "needs_participant_perspective_event_context_composer", "event_or_longform_context"
    if SELECT_RE.search(text):
        return "needs_new_microagent", "participant_perspective_uncategorized"
    return "blocked_uncertain", "blocked_uncertain"


def decide(row: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
    ready = ready_decision(row, state)
    if ready:
        return {
            "participant_perspective_decision": ready,
            "participant_perspective_subpolicy": ready.removeprefix("participant_perspective_ready_").removesuffix("_lifecycle").removesuffix("_false_reopen"),
            "tokens_seen": tokens_seen(row["current_text"]),
            "requires_lifecycle_later": True,
            "requires_apply_later": False,
            "notes": "participant perspective Select_CString surface appears aligned for future narrow lifecycle",
        }
    decision, subpolicy = policy_decision(row)
    return {
        "participant_perspective_decision": decision,
        "participant_perspective_subpolicy": subpolicy,
        "tokens_seen": tokens_seen(row["current_text"]),
        "requires_lifecycle_later": False,
        "requires_apply_later": False,
        "notes": f"routed to {decision}; no apply or lifecycle emitted by this review",
    }


def output_paths() -> tuple[Path, Path]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_dynamic_gender_select_cstring_participant_perspective_policy_review"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def write_reports(rows: list[dict[str, Any]]) -> tuple[Path, Path, Counter[str], Counter[str]]:
    jsonl_path, txt_path = output_paths()
    decision_counts = Counter(row["participant_perspective_decision"] for row in rows)
    subpolicy_counts = Counter(row["participant_perspective_subpolicy"] for row in rows)
    ready_count = sum(1 for row in rows if row["participant_perspective_decision"].startswith("participant_perspective_ready_"))
    top_need = next(
        ((decision, count) for decision, count in decision_counts.most_common() if decision.startswith("needs_participant_perspective_")),
        ("", 0),
    )
    if ready_count >= 5:
        recommendation = "prepare_narrow_readonly_lifecycle"
    elif top_need[1] >= 8:
        recommendation = f"record_future_policy_microagent:{top_need[0]}"
    else:
        recommendation = "close_tree_and_migrate_to_es_oa_or_event_context_or_global_diagnostic"

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Dynamic gender Select_CString participant perspective policy review",
        "",
        f"total_reviewed: {len(rows)}",
        "",
        "Decision counts:",
    ]
    lines.extend(f"- {key}: {value}" for key, value in sorted(decision_counts.items()))
    lines.extend(["", "Subpolicy counts:"])
    lines.extend(f"- {key}: {value}" for key, value in sorted(subpolicy_counts.items()))
    lines.extend(
        [
            "",
            f"ready_for_future_lifecycle: {ready_count}",
            "apply_candidates_future: 0",
            f"Recommendation: {recommendation}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path, decision_counts, subpolicy_counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--activity-context-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    args = parser.parse_args()

    source_rows = collect_source_rows(args.activity_context_jsonl)
    segment_ids = [int(row["segment_id"]) for row in source_rows]
    with connect_readonly() as conn:
        states = fetch_states(conn, args.segment_state_run_id, segment_ids)

    reviewed: list[dict[str, Any]] = []
    for row in source_rows:
        segment_id = int(row["segment_id"])
        reviewed.append(
            {
                "segment_id": segment_id,
                "key": row["key"],
                "relative_path": row["relative_path"],
                "current_text": row["current_text"],
                "source_activity_context_decision": "needs_activity_select_cstring_participant_perspective_policy",
                **decide(row, states.get(segment_id)),
            }
        )

    jsonl_path, txt_path, decision_counts, subpolicy_counts = write_reports(reviewed)
    print(f"total_reviewed={len(reviewed)}")
    print(f"ready_for_future_lifecycle={sum(1 for row in reviewed if row['participant_perspective_decision'].startswith('participant_perspective_ready_'))}")
    print("apply_candidates_future=0")
    print(f"jsonl_report={jsonl_path}")
    print(f"txt_report={txt_path}")
    print(f"decision_counts={json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True)}")
    print(f"subpolicy_counts={json.dumps(dict(subpolicy_counts), ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
