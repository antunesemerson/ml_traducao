from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_faith_holy_site_effect_name_segment_state_delta"
FROM_RUN_ID = 510
TO_RUN_ID = 512
ALREADY_OK_SEGMENT_IDS = (239348, 239939, 239606, 239411, 239851, 239369, 239469, 239575, 239935)
BLOCKED_CORRECTION_SEGMENT_IDS = (237388, 239477, 239479, 239507, 239509, 239511)
HELD_SEGMENT_IDS = (237382,)
SEGMENT_IDS = ALREADY_OK_SEGMENT_IDS + BLOCKED_CORRECTION_SEGMENT_IDS + HELD_SEGMENT_IDS


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def fetch_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM segment_state_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise SystemExit(f"missing segment_state_run {run_id}")
    if row["finished_at"] is None:
        raise SystemExit(f"segment_state_run {run_id} is incomplete")
    return dict(row)


def fetch_items(conn: sqlite3.Connection, run_id: int) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in SEGMENT_IDS)
    rows = conn.execute(
        f"""
        SELECT
            i.segment_id,
            i.final_state,
            i.state_group,
            i.needs_output_apply,
            i.confirmed_matches_output,
            i.relative_path,
            i.source_key,
            o.portuguese_text AS output_text,
            c.confirmed_text,
            c.confirmation_level,
            c.confirmation_source,
            c.confirmation_label,
            c.locked
        FROM segment_state_items i
        LEFT JOIN output_segments o ON o.segment_id = i.segment_id
        LEFT JOIN segment_confirmations c ON c.segment_id = i.segment_id
        WHERE i.run_id = ?
          AND i.segment_id IN ({placeholders})
        ORDER BY i.segment_id
        """,
        (run_id, *SEGMENT_IDS),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def top_counter(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common()]


def segment_kind(segment_id: int) -> str:
    if segment_id in ALREADY_OK_SEGMENT_IDS:
        return "already_ok_lifecycle_released"
    if segment_id in BLOCKED_CORRECTION_SEGMENT_IDS:
        return "blocked_token_change_correction"
    if segment_id in HELD_SEGMENT_IDS:
        return "hold_context"
    return "unknown"


def main() -> None:
    with connect_readonly() as conn:
        from_run = fetch_run(conn, FROM_RUN_ID)
        to_run = fetch_run(conn, TO_RUN_ID)
        before = fetch_items(conn, FROM_RUN_ID)
        after = fetch_items(conn, TO_RUN_ID)
    missing = sorted((set(SEGMENT_IDS) - set(before)) | (set(SEGMENT_IDS) - set(after)))
    if missing:
        raise SystemExit(f"missing segment state items: {missing}")

    rows: list[dict[str, Any]] = []
    for segment_id in SEGMENT_IDS:
        old = before[segment_id]
        new = after[segment_id]
        rows.append(
            {
                "segment_id": segment_id,
                "segment_kind": segment_kind(segment_id),
                "relative_path": new.get("relative_path"),
                "source_key": new.get("source_key"),
                "from_state_group": old.get("state_group"),
                "to_state_group": new.get("state_group"),
                "from_final_state": old.get("final_state"),
                "to_final_state": new.get("final_state"),
                "from_needs_output_apply": int(old.get("needs_output_apply") or 0),
                "to_needs_output_apply": int(new.get("needs_output_apply") or 0),
                "from_confirmed_matches_output": int(old.get("confirmed_matches_output") or 0),
                "to_confirmed_matches_output": int(new.get("confirmed_matches_output") or 0),
                "confirmed_matches_output_now": bool((new.get("confirmed_text") or "") == (new.get("output_text") or "")),
                "confirmation_level": new.get("confirmation_level"),
                "confirmation_source": new.get("confirmation_source"),
                "confirmation_label": new.get("confirmation_label"),
                "locked": int(new.get("locked") or 0),
            }
        )

    by_kind = Counter(row["segment_kind"] for row in rows)
    transition_counts = Counter(f"{row['from_state_group']} -> {row['to_state_group']}" for row in rows)
    final_state_counts = Counter(str(row["to_final_state"]) for row in rows)
    already_ok_rows = [row for row in rows if row["segment_kind"] == "already_ok_lifecycle_released"]
    blocked_rows = [row for row in rows if row["segment_kind"] == "blocked_token_change_correction"]
    held_rows = [row for row in rows if row["segment_kind"] == "hold_context"]

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_segment_state_delta",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "from_run_id": FROM_RUN_ID,
        "to_run_id": TO_RUN_ID,
        "from_run_finished_at": from_run["finished_at"],
        "to_run_finished_at": to_run["finished_at"],
        "global_before": {
            "closed_count": int(from_run["closed_count"] or 0),
            "pending_count": int(from_run["pending_count"] or 0),
            "reopen_count": int(from_run["reopen_count"] or 0),
            "output_apply_pending_count": int(from_run["output_apply_pending_count"] or 0),
        },
        "global_after": {
            "closed_count": int(to_run["closed_count"] or 0),
            "pending_count": int(to_run["pending_count"] or 0),
            "reopen_count": int(to_run["reopen_count"] or 0),
            "output_apply_pending_count": int(to_run["output_apply_pending_count"] or 0),
        },
        "global_delta": {
            "closed_count": int(to_run["closed_count"] or 0) - int(from_run["closed_count"] or 0),
            "pending_count": int(to_run["pending_count"] or 0) - int(from_run["pending_count"] or 0),
            "reopen_count": int(to_run["reopen_count"] or 0) - int(from_run["reopen_count"] or 0),
            "output_apply_pending_count": int(to_run["output_apply_pending_count"] or 0) - int(from_run["output_apply_pending_count"] or 0),
        },
        "segment_kind_counts": dict(by_kind),
        "transition_counts": top_counter(transition_counts),
        "final_state_counts": top_counter(final_state_counts),
        "already_ok_closed_count": sum(1 for row in already_ok_rows if row["to_state_group"] == "closed"),
        "already_ok_pending_count": sum(1 for row in already_ok_rows if row["to_state_group"] == "pending"),
        "blocked_correction_pending_count": sum(1 for row in blocked_rows if row["to_state_group"] == "pending"),
        "blocked_correction_needs_output_apply_count": sum(row["to_needs_output_apply"] for row in blocked_rows),
        "blocked_correction_confirmed_matches_output_now_count": sum(1 for row in blocked_rows if row["confirmed_matches_output_now"]),
        "held_pending_count": sum(1 for row in held_rows if row["to_state_group"] == "pending"),
        "apply_output_count": 0,
        "candidate_generation_count": 0,
        "lifecycle_policy_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_count": 0,
        "production_full_recommended_now": False,
        "records": rows,
        "output_files": {},
    }

    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_religion_faith_holy_site_effect_name_segment_state_delta"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, rows)
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "religion_faith holy-site effect-name segment-state delta",
        "",
        f"from_run_id: {FROM_RUN_ID}",
        f"to_run_id: {TO_RUN_ID}",
        f"global_closed_delta: {summary['global_delta']['closed_count']}",
        f"global_pending_delta: {summary['global_delta']['pending_count']}",
        f"global_output_apply_pending_delta: {summary['global_delta']['output_apply_pending_count']}",
        f"already_ok_closed_count: {summary['already_ok_closed_count']}",
        f"blocked_correction_pending_count: {summary['blocked_correction_pending_count']}",
        f"held_pending_count: {summary['held_pending_count']}",
        "",
        "final_state_counts:",
        *[f"- {item['count']} | {item['key']}" for item in summary["final_state_counts"]],
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
