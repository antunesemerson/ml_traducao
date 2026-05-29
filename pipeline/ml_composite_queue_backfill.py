from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "ml_composite_queue_backfill_v1"


def resolve_path(path_value: str | None) -> Path | None:
    if not path_value:
        return None
    path = Path(path_value)
    if not path.is_absolute():
        path = db.project_path(str(path))
    return path


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def fetch_queue_runs(conn, *, only_missing: bool) -> list[dict[str, Any]]:
    where = [
        "source_mode = 'active_composite_gate'",
        "jsonl_path IS NOT NULL",
    ]
    if only_missing:
        where.append(
            """
            NOT EXISTS (
                SELECT 1
                FROM ml_composite_gate_queue_items qi
                WHERE qi.queue_run_id = ml_composite_gate_queue_runs.id
            )
            """
        )
    rows = conn.execute(
        f"""
        SELECT *
        FROM ml_composite_gate_queue_runs
        WHERE {" AND ".join(where)}
        ORDER BY id ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def insert_items(conn, *, queue_run: dict[str, Any], rows: list[dict[str, Any]], now: str) -> int:
    inserted = 0
    for row in rows:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO ml_composite_gate_queue_items (
                queue_run_id,
                gate_key,
                checkpoint_id,
                overlay_run_id,
                source_policy_run_id,
                policy_item_id,
                segment_id,
                suggested_route,
                overlay_policy_bucket,
                overlay_risk_level,
                relative_path,
                source_key,
                source_line_number,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                queue_run["id"],
                queue_run["gate_key"],
                queue_run["checkpoint_id"],
                queue_run["overlay_run_id"],
                queue_run["source_policy_run_id"],
                row["policy_item_id"],
                row["segment_id"],
                row["suggested_route"],
                row["overlay_policy_bucket"],
                row["overlay_risk_level"],
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                now,
            ),
        )
        inserted += cursor.rowcount
    return inserted


def write_report(settings: dict[str, Any], *, summaries: list[dict[str, Any]], started_at: datetime) -> Path:
    lines = [
        "ML composite queue item backfill",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        "",
        "Summary:",
        f"- Queue runs inspected: {len(summaries)}",
        f"- Rows loaded: {sum(item['rows_loaded'] for item in summaries)}",
        f"- Items inserted: {sum(item['inserted'] for item in summaries)}",
        f"- Missing files: {sum(1 for item in summaries if item['status'] == 'missing_jsonl')}",
        f"- Failed files: {sum(1 for item in summaries if item['status'] == 'failed')}",
        "",
        "Runs:",
    ]
    for item in summaries:
        lines.append(
            "- run {run_id} | {route} | status={status} | loaded={rows_loaded} | inserted={inserted} | {path}".format(
                run_id=item["queue_run_id"],
                route=item.get("route_filter_csv") or "all",
                status=item["status"],
                rows_loaded=item["rows_loaded"],
                inserted=item["inserted"],
                path=item.get("jsonl_path") or "none",
            )
        )
    return db.write_report(settings, "ml_composite_queue_backfill", lines)


def main(*, only_missing: bool = True) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    now = started_at.isoformat(timespec="seconds")
    summaries: list[dict[str, Any]] = []
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        queue_runs = fetch_queue_runs(conn, only_missing=only_missing)
        for queue_run in queue_runs:
            summary = {
                "queue_run_id": queue_run["id"],
                "route_filter_csv": queue_run.get("route_filter_csv"),
                "jsonl_path": queue_run.get("jsonl_path"),
                "rows_loaded": 0,
                "inserted": 0,
                "status": "ok",
            }
            path = resolve_path(queue_run.get("jsonl_path"))
            if path is None or not path.exists():
                summary["status"] = "missing_jsonl"
                summaries.append(summary)
                continue
            try:
                rows = load_jsonl(path)
                summary["rows_loaded"] = len(rows)
                summary["inserted"] = insert_items(conn, queue_run=queue_run, rows=rows, now=now)
            except Exception as exc:  # noqa: BLE001 - keep backfill resilient and auditable.
                summary["status"] = "failed"
                summary["error"] = f"{type(exc).__name__}: {exc}"
            summaries.append(summary)
        conn.commit()
    report_path = write_report(settings, summaries=summaries, started_at=started_at)
    print("[ml_composite_queue_backfill] Backfill complete")
    print(f"[ml_composite_queue_backfill] Rule version: {RULE_VERSION}")
    print(f"[ml_composite_queue_backfill] Queue runs inspected: {len(summaries)}")
    print(f"[ml_composite_queue_backfill] Rows loaded: {sum(item['rows_loaded'] for item in summaries)}")
    print(f"[ml_composite_queue_backfill] Items inserted: {sum(item['inserted'] for item in summaries)}")
    print(f"[ml_composite_queue_backfill] Report: {report_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill item-level records for active composite gate queues.")
    parser.add_argument("--all", action="store_true", help="Inspect all active gate queue runs, not just missing item rows.")
    args = parser.parse_args()
    main(only_missing=not args.all)
