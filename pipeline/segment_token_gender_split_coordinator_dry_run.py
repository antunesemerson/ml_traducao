from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "segment_token_gender_split_coordinator_dry_run_v1"
COORDINATOR_ROUTE = "gender_token_guarded_split_policy"


def latest_guarded_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_token_gender_split_guarded_policy_runs
        WHERE finished_at IS NOT NULL
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished segment_token_gender_split_guarded_policy_runs entry found.")
    return int(row["id"])


def fetch_guarded_run(conn, guarded_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM segment_token_gender_split_guarded_policy_runs
        WHERE id = ?
        """,
        (guarded_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Guarded policy run {guarded_run_id} was not found.")
    return dict(row)


def fetch_guarded_items(conn, guarded_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM segment_token_gender_split_guarded_policy_items
        WHERE run_id = ?
        ORDER BY
            CASE guarded_status
                WHEN 'ready_for_guarded_dry_run_release' THEN 0
                ELSE 1
            END,
            split_agent,
            relative_path,
            source_line_number,
            policy_item_id
        """,
        (guarded_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def coordinator_row(row: dict[str, Any]) -> dict[str, Any]:
    if int(row.get("apply_allowed") or 0):
        status = "blocked_apply_flag_unexpected"
        action = "do_not_route"
        priority = "hard_block"
    elif row["guarded_status"] == "ready_for_guarded_dry_run_release":
        status = "ready_for_coordinator_dry_run"
        action = "route_to_guarded_low_review"
        priority = "candidate_release"
    else:
        status = "blocked_by_guarded_policy"
        action = "keep_manual_review"
        priority = "blocked"
    return {
        **row,
        "coordinator_route": COORDINATOR_ROUTE,
        "coordinator_status": status,
        "coordinator_action": action,
        "coordinator_priority": priority,
        "coordinator_apply_allowed": 0,
    }


def write_outputs(
    settings: dict[str, Any],
    *,
    guarded_run: dict[str, Any],
    rows: list[dict[str, Any]],
    started_at: datetime,
) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{timestamp}_segment_token_gender_split_coordinator_dry_run"
    txt_path = base.with_suffix(".txt")
    csv_path = base.with_suffix(".csv")
    jsonl_path = base.with_suffix(".jsonl")

    fieldnames = [
        "policy_item_id",
        "segment_id",
        "relative_path",
        "source_line_number",
        "source_key",
        "split_agent",
        "guarded_status",
        "guarded_action",
        "target_policy_bucket",
        "target_risk_level",
        "coordinator_route",
        "coordinator_status",
        "coordinator_action",
        "coordinator_priority",
        "coordinator_apply_allowed",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    status_counts = Counter(row["coordinator_status"] for row in rows)
    agent_counts = Counter(row["split_agent"] for row in rows if row["coordinator_status"] == "ready_for_coordinator_dry_run")
    lines = [
        "Segment token gender split coordinator dry-run",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Coordinator route: {COORDINATOR_ROUTE}",
        f"Guarded policy run id: {guarded_run['id']}",
        f"Policy run id: {guarded_run['policy_run_id']}",
        f"Promotion audit run id: {guarded_run['audit_run_id']}",
        "",
        "Safety contract:",
        "- This is a coordinator integration dry-run only.",
        "- Coordinator apply allowed: 0",
        "- Source/output writes: no",
        "- Downstream production must still use its own gate before any output apply.",
        "",
        "Summary:",
        f"- Rows inspected: {len(rows)}",
        *[f"- {key}: {value}" for key, value in status_counts.most_common()],
        "",
        "Ready agents:",
        *[f"- {key}: {value}" for key, value in agent_counts.most_common()],
        "",
        "Coordinator contract:",
        "- ready_for_coordinator_dry_run rows may be routed as low-risk guarded review candidates.",
        "- blocked rows remain manual review.",
        "- This artifact does not create segment_token_policy_decisions and does not approve apply.",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path


def main(*, guarded_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_guarded_run_id = guarded_run_id or latest_guarded_run_id(conn)
        guarded_run = fetch_guarded_run(conn, selected_guarded_run_id)
        guarded_items = fetch_guarded_items(conn, selected_guarded_run_id)
    rows = [coordinator_row(row) for row in guarded_items]
    report_path, csv_path, jsonl_path = write_outputs(
        settings,
        guarded_run=guarded_run,
        rows=rows,
        started_at=started_at,
    )
    status_counts = Counter(row["coordinator_status"] for row in rows)
    print("[segment_token_gender_split_coordinator_dry_run] Coordinator dry-run generated")
    print(f"[segment_token_gender_split_coordinator_dry_run] Rule version: {RULE_VERSION}")
    print(f"[segment_token_gender_split_coordinator_dry_run] Guarded policy run id: {selected_guarded_run_id}")
    print(f"[segment_token_gender_split_coordinator_dry_run] Rows inspected: {len(rows)}")
    for key, value in status_counts.most_common():
        print(f"[segment_token_gender_split_coordinator_dry_run] {key}: {value}")
    print("[segment_token_gender_split_coordinator_dry_run] Apply allowed: 0")
    print(f"[segment_token_gender_split_coordinator_dry_run] Report: {report_path}")
    print(f"[segment_token_gender_split_coordinator_dry_run] CSV: {csv_path}")
    print(f"[segment_token_gender_split_coordinator_dry_run] JSONL: {jsonl_path}")
    return {
        "report_path": str(report_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "rows": len(rows),
        "status_counts": dict(status_counts),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build coordinator dry-run inputs for gender split guarded policies.")
    parser.add_argument("--guarded-run-id", type=int, default=None)
    args = parser.parse_args()
    main(guarded_run_id=args.guarded_run_id)
