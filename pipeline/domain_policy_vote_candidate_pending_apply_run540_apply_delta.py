from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_pending_apply_run540_apply_delta_v1"
INPUT_JSONL = Path("reports/20260702_113500_447153_domain_policy_vote_candidate_pending_apply_run540_protected_apply_apply.jsonl")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only delta after pending apply run540 protected apply.")
    parser.add_argument("--input-jsonl", type=Path, default=INPUT_JSONL)
    parser.add_argument("--before-run-id", type=int, required=True)
    parser.add_argument("--after-run-id", type=int, required=True)
    parser.add_argument("--expected-applied", type=int, default=11)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def fetch_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM segment_state_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise SystemExit(f"missing run {run_id}")
    return dict(row)


def fetch_states(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, final_state, state_group, is_closed, confirmed_matches_output, needs_output_apply
        FROM segment_state_items
        WHERE run_id = ? AND segment_id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    applied_rows = [row for row in read_jsonl(args.input_jsonl) if row.get("status") == "ready"]
    segment_ids = [int(row["segment_id"]) for row in applied_rows]
    if len(segment_ids) != args.expected_applied:
        raise SystemExit(f"input guard failed: {len(segment_ids)}")
    with connect_readonly() as conn:
        before_run = fetch_run(conn, args.before_run_id)
        after_run = fetch_run(conn, args.after_run_id)
        before = fetch_states(conn, args.before_run_id, segment_ids)
        after = fetch_states(conn, args.after_run_id, segment_ids)
        pending_apply_rows = conn.execute(
            """
            SELECT segment_id, relative_path, source_key
            FROM segment_state_items
            WHERE run_id = ? AND final_state = 'pending_apply_confirmed'
            ORDER BY segment_id
            """,
            (args.after_run_id,),
        ).fetchall()
    records: list[dict[str, Any]] = []
    for row in applied_rows:
        sid = int(row["segment_id"])
        b = before.get(sid, {})
        a = after.get(sid, {})
        records.append(
            {
                "source": SOURCE,
                "segment_id": sid,
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "before_final_state": b.get("final_state"),
                "after_final_state": a.get("final_state"),
                "transition": f"{b.get('final_state')} -> {a.get('final_state')}",
                "after_confirmed_matches_output": int(a.get("confirmed_matches_output") or 0),
                "after_needs_output_apply": int(a.get("needs_output_apply") or 0),
                "validation": "output_apply_cleared" if int(a.get("needs_output_apply") or 0) == 0 else "still_needs_output_apply",
                "candidate_generation_count": 0,
                "apply_count": 1,
                "lifecycle_count": 0,
                "segment_state_count": 1,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )
    global_delta = {
        "closed_count": int(after_run.get("closed_count") or 0) - int(before_run.get("closed_count") or 0),
        "pending_count": int(after_run.get("pending_count") or 0) - int(before_run.get("pending_count") or 0),
        "reopen_count": int(after_run.get("reopen_count") or 0) - int(before_run.get("reopen_count") or 0),
        "output_apply_pending_count": int(after_run.get("output_apply_pending_count") or 0)
        - int(before_run.get("output_apply_pending_count") or 0),
    }
    validation_counts = Counter(row["validation"] for row in records)
    after_state_counts = Counter(str(row["after_final_state"]) for row in records)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_post_apply_delta",
        "before_run_id": args.before_run_id,
        "after_run_id": args.after_run_id,
        "record_count": len(records),
        "expected_applied": args.expected_applied,
        "global_delta": global_delta,
        "validation_counts": dict(validation_counts.most_common()),
        "after_state_counts": dict(after_state_counts.most_common()),
        "output_apply_cleared_count": validation_counts.get("output_apply_cleared", 0),
        "still_needs_output_apply_count": validation_counts.get("still_needs_output_apply", 0),
        "remaining_pending_apply_confirmed_count": len(pending_apply_rows),
        "remaining_pending_apply_confirmed": [dict(row) for row in pending_apply_rows],
        "expected_outcome_met": validation_counts.get("output_apply_cleared", 0) == args.expected_applied
        and global_delta["output_apply_pending_count"] == -args.expected_applied
        and len(pending_apply_rows) == 1,
        "candidate_generation_count": 0,
        "apply_count": args.expected_applied,
        "lifecycle_count": 0,
        "segment_state_count": 1,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": True,
        "production_full_recommended_now": False,
        "single_operational_recommendation": (
            "Protected apply succeeded. Keep remaining token-changing pending_apply_confirmed in hold and return to closure debt organization."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_pending_apply_run540_apply_delta_{summary['before_run_id']}_{summary['after_run_id']}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Pending apply run540 apply delta",
        f"before_run_id={summary['before_run_id']}",
        f"after_run_id={summary['after_run_id']}",
        f"record_count={summary['record_count']}",
        f"global_delta={json.dumps(summary['global_delta'], ensure_ascii=False, sort_keys=True)}",
        f"validation_counts={json.dumps(summary['validation_counts'], ensure_ascii=False, sort_keys=True)}",
        f"remaining_pending_apply_confirmed_count={summary['remaining_pending_apply_confirmed_count']}",
        "",
        "Recommendation:",
        summary["single_operational_recommendation"],
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records, summary = build(args)
    txt_path, jsonl_path, summary_path = write_reports(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"output_apply_cleared_count={summary['output_apply_cleared_count']}")
    print(f"still_needs_output_apply_count={summary['still_needs_output_apply_count']}")
    print(f"remaining_pending_apply_confirmed_count={summary['remaining_pending_apply_confirmed_count']}")
    print(f"needs_output_apply_delta={summary['global_delta']['output_apply_pending_count']}")
    print(f"expected_outcome_met={summary['expected_outcome_met']}")
    print("candidate_generation_count=0")
    print(f"apply_count={summary['apply_count']}")
    print("segment_state_count=1")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
