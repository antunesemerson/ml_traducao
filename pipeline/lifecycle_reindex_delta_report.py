from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


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
    return conn


def run_row(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM segment_state_runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        raise SystemExit(f"missing segment_state_run_id={run_id}")
    return dict(row)


def counts(conn: sqlite3.Connection, run_id: int, column: str) -> dict[str, int]:
    return {
        str(row[column]): int(row["count"])
        for row in conn.execute(
            f"""
            SELECT {column}, COUNT(*) AS count
            FROM segment_state_items
            WHERE run_id = ?
            GROUP BY {column}
            """,
            (run_id,),
        )
    }


def top_final_deltas(conn: sqlite3.Connection, before_run_id: int, after_run_id: int) -> list[dict[str, Any]]:
    before = counts(conn, before_run_id, "final_state")
    after = counts(conn, after_run_id, "final_state")
    keys = sorted(set(before) | set(after))
    rows = [
        {"final_state": key, "before": before.get(key, 0), "after": after.get(key, 0), "delta": after.get(key, 0) - before.get(key, 0)}
        for key in keys
    ]
    return sorted(rows, key=lambda row: abs(row["delta"]), reverse=True)[:30]


def changed_segments(conn: sqlite3.Connection, before_run_id: int, after_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            before.segment_id,
            before.relative_path,
            before.source_key,
            before.final_state AS before_final_state,
            after.final_state AS after_final_state,
            before.state_group AS before_state_group,
            after.state_group AS after_state_group
        FROM segment_state_items before
        JOIN segment_state_items after ON after.segment_id = before.segment_id
        WHERE before.run_id = ?
          AND after.run_id = ?
          AND (
            before.final_state != after.final_state
            OR before.state_group != after.state_group
            OR before.needs_output_apply != after.needs_output_apply
            OR before.confirmed_matches_output != after.confirmed_matches_output
          )
        ORDER BY before.segment_id
        """,
        (before_run_id, after_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def segment_lookup(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> list[dict[str, Any]]:
    if not segment_ids:
        return []
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, relative_path, source_key, final_state, state_group, needs_human,
               needs_output_apply, confirmed_matches_output, review_state, apply_state
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        ORDER BY segment_id
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return [dict(row) for row in rows]


def build_summary(conn: sqlite3.Connection, before_run_id: int, after_run_id: int, tracked_ids: list[int]) -> dict[str, Any]:
    before = run_row(conn, before_run_id)
    after = run_row(conn, after_run_id)
    changed = changed_segments(conn, before_run_id, after_run_id)
    transition_counts = Counter(
        f"{row['before_state_group']}:{row['before_final_state']} -> {row['after_state_group']}:{row['after_final_state']}"
        for row in changed
    )
    return {
        "schema_version": 1,
        "source": "lifecycle_reindex_delta_report_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "before_run_id": before_run_id,
        "after_run_id": after_run_id,
        "before": before,
        "after": after,
        "delta": {
            "total_segments": int(after["total_segments"] or 0) - int(before["total_segments"] or 0),
            "closed_count": int(after["closed_count"] or 0) - int(before["closed_count"] or 0),
            "pending_count": int(after["pending_count"] or 0) - int(before["pending_count"] or 0),
            "output_apply_pending_count": int(after["output_apply_pending_count"] or 0)
            - int(before["output_apply_pending_count"] or 0),
            "reopen_count": int(after["reopen_count"] or 0) - int(before["reopen_count"] or 0),
        },
        "changed_segment_count": len(changed),
        "transition_counts": dict(transition_counts.most_common(20)),
        "top_final_state_deltas": top_final_deltas(conn, before_run_id, after_run_id),
        "tracked_segments_before": segment_lookup(conn, before_run_id, tracked_ids),
        "tracked_segments_after": segment_lookup(conn, after_run_id, tracked_ids),
        "production_full_recommended_now": False,
        "next_action": "retarget_readonly_on_latest_segment_state_or_review_delta",
    }


def write_outputs(summary: dict[str, Any]) -> tuple[Path, Path]:
    base = reports_dir() / f"{stamp()}_lifecycle_reindex_delta_report"
    txt_path = base.with_suffix(".txt")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Lifecycle/reindex delta report",
        f"before_run_id={summary['before_run_id']}",
        f"after_run_id={summary['after_run_id']}",
        "",
        "Delta:",
        *[f"- {key}: {value}" for key, value in summary["delta"].items()],
        f"- changed_segment_count: {summary['changed_segment_count']}",
        "",
        "After stats:",
        f"- total_segments: {summary['after']['total_segments']}",
        f"- closed_count: {summary['after']['closed_count']}",
        f"- pending_count: {summary['after']['pending_count']}",
        f"- output_apply_pending_count: {summary['after']['output_apply_pending_count']}",
        f"- reopen_count: {summary['after']['reopen_count']}",
        "",
        "Tracked segments after:",
    ]
    for row in summary["tracked_segments_after"]:
        lines.append(
            f"- {row['segment_id']}: {row['state_group']} / {row['final_state']} / needs_output_apply={row['needs_output_apply']}"
        )
    lines.extend(["", "Top transitions:"])
    lines.extend(f"- {key}: {value}" for key, value in summary["transition_counts"].items())
    lines.append("")
    lines.append("production_full_recommended_now=false")
    lines.append(f"next_action={summary['next_action']}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before-run-id", type=int, required=True)
    parser.add_argument("--after-run-id", type=int, required=True)
    parser.add_argument("--tracked-segment-id", type=int, action="append", default=[])
    args = parser.parse_args()
    with connect_readonly() as conn:
        summary = build_summary(conn, args.before_run_id, args.after_run_id, args.tracked_segment_id)
    txt_path, summary_path = write_outputs(summary)
    print(f"txt={txt_path}")
    print(f"summary={summary_path}")
    print("delta=" + json.dumps(summary["delta"], ensure_ascii=False, sort_keys=True))
    print(f"changed_segment_count={summary['changed_segment_count']}")
    print("tracked_segments_after=" + json.dumps(summary["tracked_segments_after"], ensure_ascii=False, sort_keys=True))
    print("production_full_recommended_now=false")


if __name__ == "__main__":
    main()
