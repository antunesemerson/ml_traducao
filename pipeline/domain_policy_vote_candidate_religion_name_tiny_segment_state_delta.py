from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_name_tiny_segment_state_delta"
FROM_RUN_ID = 504
TO_RUN_ID = 506
SEGMENT_IDS = (127339, 239280, 239285, 240291, 63072)


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


def write_txt(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "domain_policy_vote_candidate religion_name tiny segment-state delta",
        "",
        f"from_run_id: {summary['from_run_id']}",
        f"to_run_id: {summary['to_run_id']}",
        f"global_closed_delta: {summary['global_delta']['closed_count']}",
        f"global_pending_delta: {summary['global_delta']['pending_count']}",
        f"global_reopen_delta: {summary['global_delta']['reopen_count']}",
        f"global_needs_output_apply_delta: {summary['global_delta']['output_apply_pending_count']}",
        "",
        "batch_transition_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["batch_transition_counts"])
    lines.extend(["", "batch_final_state_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["batch_final_state_counts"])
    lines.extend(["", "items:"])
    for row in rows:
        lines.extend(
            [
                "",
                f"## segment_id {row['segment_id']} | {row['from_state_group']} -> {row['to_state_group']}",
                f"- from_final_state: {row['from_final_state']}",
                f"- to_final_state: {row['to_final_state']}",
                f"- needs_output_apply: {row['to_needs_output_apply']}",
                f"- source_key: {row['source_key']}",
            ]
        )
    lines.extend(["", "gates:", "- candidate_generation: not_run", "- apply_output: not_run", "- lifecycle: not_run", "- reindex: not_run", "- full_production: not_run"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    with connect_readonly() as conn:
        from_run = fetch_run(conn, FROM_RUN_ID)
        to_run = fetch_run(conn, TO_RUN_ID)
        if to_run.get("finished_at") is None:
            raise SystemExit(f"to_run {TO_RUN_ID} is incomplete")
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
                "relative_path": new.get("relative_path"),
                "source_key": new.get("source_key"),
                "from_state_group": old.get("state_group"),
                "to_state_group": new.get("state_group"),
                "from_final_state": old.get("final_state"),
                "to_final_state": new.get("final_state"),
                "from_needs_output_apply": int(old.get("needs_output_apply") or 0),
                "to_needs_output_apply": int(new.get("needs_output_apply") or 0),
                "confirmed_matches_output": bool((new.get("confirmed_text") or "") == (new.get("output_text") or "")),
                "confirmation_level": new.get("confirmation_level"),
                "confirmation_source": new.get("confirmation_source"),
                "confirmation_label": new.get("confirmation_label"),
                "locked": int(new.get("locked") or 0),
            }
        )

    transition_counts = Counter(f"{row['from_state_group']} -> {row['to_state_group']}" for row in rows)
    final_state_counts = Counter(str(row["to_final_state"]) for row in rows)
    needs_output_apply_count = sum(int(row["to_needs_output_apply"]) for row in rows)
    closed_batch_count = sum(1 for row in rows if row["to_state_group"] == "closed")
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_segment_state_delta",
        "from_run_id": FROM_RUN_ID,
        "to_run_id": TO_RUN_ID,
        "from_run_finished_at": from_run.get("finished_at"),
        "to_run_finished_at": to_run.get("finished_at"),
        "global_before": {
            "closed_count": int(from_run["closed_count"]),
            "pending_count": int(from_run["pending_count"]),
            "reopen_count": int(from_run["reopen_count"]),
            "output_apply_pending_count": int(from_run["output_apply_pending_count"]),
        },
        "global_after": {
            "closed_count": int(to_run["closed_count"]),
            "pending_count": int(to_run["pending_count"]),
            "reopen_count": int(to_run["reopen_count"]),
            "output_apply_pending_count": int(to_run["output_apply_pending_count"]),
        },
        "global_delta": {
            "closed_count": int(to_run["closed_count"]) - int(from_run["closed_count"]),
            "pending_count": int(to_run["pending_count"]) - int(from_run["pending_count"]),
            "reopen_count": int(to_run["reopen_count"]) - int(from_run["reopen_count"]),
            "output_apply_pending_count": int(to_run["output_apply_pending_count"]) - int(from_run["output_apply_pending_count"]),
        },
        "batch_count": len(rows),
        "batch_closed_count": closed_batch_count,
        "batch_pending_count": len(rows) - closed_batch_count,
        "batch_needs_output_apply_count": needs_output_apply_count,
        "batch_all_confirmed_match_output": all(row["confirmed_matches_output"] for row in rows),
        "batch_transition_counts": top_counter(transition_counts),
        "batch_final_state_counts": top_counter(final_state_counts),
        "candidate_generation_count": 0,
        "apply_output_count": 0,
        "lifecycle_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "output_files": {},
    }
    base = reports_dir() / f"{stamp()}_{SOURCE}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, rows)
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary, rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
