from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "issue_gender_longform_false_reopen_checkpoint_v1"
ROUTE_RULE_VERSION = "issue_gender_longform_context_route_diagnostic_v1"
CHECKPOINT_NAME = "gender_longform_false_reopen_context_checkpoint_v1"
CHECKPOINT_STATUS = "ready_for_shadow_lifecycle_bridge"
PROMOTION_STATUS = "false_reopen_candidate"
ALLOWED_ROUTES = {
    "single_combat_gender_surface_false_reopen_candidate",
    "tutorial_help_gender_surface_false_reopen_candidate",
    "generic_longform_gender_surface_false_reopen_candidate",
}


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def report_paths(settings: dict[str, Any], route_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{now_stamp()}_issue_gender_longform_false_reopen_checkpoint_route_run_{route_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_route_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_gender_longform_context_route_runs
        WHERE rule_version = ?
          AND potential_false_reopen_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (ROUTE_RULE_VERSION,),
    ).fetchone()
    if row is None:
        raise RuntimeError("No gender longform route diagnostic with false-reopen candidates found.")
    return int(row["id"])


def fetch_route_run(conn, route_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_gender_longform_context_route_runs
        WHERE id = ?
        """,
        (route_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Route diagnostic run not found: {route_run_id}")
    return dict(row)


def fetch_items(conn, route_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_gender_longform_context_route_items
        WHERE run_id = ?
        ORDER BY candidate_close_ready DESC, route_key, relative_path, source_line_number, source_key
        """,
        (route_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_gender_longform_false_reopen_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            route_run_id INTEGER NOT NULL,
            diagnostic_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            promotion_status TEXT NOT NULL,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_blocked_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            route_counts_json TEXT,
            blocker_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_gender_longform_false_reopen_checkpoint_items (
            id INTEGER PRIMARY KEY,
            checkpoint_run_id INTEGER NOT NULL,
            route_run_id INTEGER NOT NULL,
            route_item_id INTEGER NOT NULL,
            diagnostic_item_id INTEGER NOT NULL,
            diagnostic_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER NOT NULL DEFAULT 0,
            route_key TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            checkpoint_action TEXT NOT NULL,
            block_reason TEXT,
            final_state TEXT,
            confirmed_matches_output INTEGER NOT NULL DEFAULT 0,
            needs_output_apply INTEGER NOT NULL DEFAULT 0,
            validation_issue_codes TEXT,
            current_text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_gender_longform_false_reopen_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def evaluate_item(item: dict[str, Any], *, global_reasons: list[str]) -> dict[str, Any]:
    reasons = list(global_reasons)
    route_key = item.get("route_key") or ""
    if route_key not in ALLOWED_ROUTES:
        reasons.append("route_not_allowed_for_false_reopen_checkpoint")
    if not int(item.get("candidate_close_ready") or 0):
        reasons.append(item.get("block_reason") or "route_item_not_close_ready")
    if item.get("validation_issue_codes"):
        reasons.append("validation_issues_present")
    if not str(item.get("final_state") or "").startswith("reopen_auto_confirmed"):
        reasons.append("not_auto_confirmed_reopen")
    if not int(item.get("confirmed_matches_output") or 0):
        reasons.append("confirmation_output_mismatch")
    if int(item.get("needs_output_apply") or 0):
        reasons.append("needs_output_apply")

    allowed = 0 if reasons else 1
    return {
        **item,
        "checkpoint_allowed": allowed,
        "checkpoint_action": "stage_gender_longform_false_reopen_shadow_lifecycle",
        "checkpoint_block_reason": ";".join(reasons),
    }


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    route_run: dict[str, Any],
    rows: list[dict[str, Any]],
    global_reasons: list[str],
) -> None:
    fields = [
        "checkpoint_allowed",
        "checkpoint_action",
        "checkpoint_block_reason",
        "route_key",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "final_state",
        "confirmed_matches_output",
        "needs_output_apply",
        "validation_issue_codes",
        "current_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    counts = Counter("checkpoint_allowed" if row["checkpoint_allowed"] else row["checkpoint_block_reason"] for row in rows)
    route_counts = Counter(row["route_key"] for row in rows if row["checkpoint_allowed"])
    blocked = [row for row in rows if not row["checkpoint_allowed"]]
    allowed = [row for row in rows if row["checkpoint_allowed"]]

    lines = [
        "Issue gender longform false-reopen checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint name: {CHECKPOINT_NAME}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Route run id: {route_run['id']}",
        f"Diagnostic run id: {route_run['diagnostic_run_id']}",
        f"Segment-state run id: {route_run['segment_state_run_id']}",
        f"Checkpoint status: {CHECKPOINT_STATUS}",
        f"Promotion status: {PROMOTION_STATUS}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Allowed false-reopen candidates: {len(allowed):,}",
        f"- Blocked/preserved: {len(blocked):,}",
        "",
        "Global blockers:",
        *([f"- {reason}" for reason in global_reasons] or ["- none"]),
        "",
        "Checkpoint counts:",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        "",
        "Allowed routes:",
        *[f"- {key}: {value:,}" for key, value in route_counts.most_common()],
        "",
        "Allowed samples:",
    ]
    for row in allowed[:25]:
        lines.extend(
            [
                f"- {row['route_key']} | segment={row['segment_id']} {row['relative_path']}::{row['source_key']}",
                f"  text: {short(row['current_text'], 220)}",
            ]
        )
    lines.extend(["", "Blocked samples:"])
    if blocked:
        for row in blocked[:30]:
            lines.extend(
                [
                    f"- {row['checkpoint_block_reason']} | {row['route_key']} | "
                    f"segment={row['segment_id']} {row['relative_path']}::{row['source_key']}",
                    f"  validation: {row['validation_issue_codes'] or 'none'}",
                ]
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Checkpoint only: no production run, no confirmations, no source/output writes.",
            "- Lifecycle closure must be integrated separately by the production/system front.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, route_run_id: int | None = None, max_blocked_allowed: int = 10) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_route_run_id = route_run_id or latest_route_run_id(conn)
        route_run = fetch_route_run(conn, selected_route_run_id)
        source_rows = fetch_items(conn, selected_route_run_id)
        global_reasons: list[str] = []
        if route_run.get("rule_version") != ROUTE_RULE_VERSION:
            global_reasons.append("wrong_route_rule_version")
        if int(route_run.get("potential_false_reopen_count") or 0) <= 0:
            global_reasons.append("route_run_has_no_false_reopen_candidates")
        if int(route_run.get("candidate_count") or 0) - int(route_run.get("potential_false_reopen_count") or 0) > max_blocked_allowed:
            global_reasons.append("too_many_preserved_non_ready_items")
        if not source_rows:
            global_reasons.append("no_checkpoint_items")

        rows = [evaluate_item(row, global_reasons=global_reasons) for row in source_rows]
        allowed_count = sum(1 for row in rows if row["checkpoint_allowed"])
        blocked_count = len(rows) - allowed_count
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_route_run_id)
        route_counts = Counter(row["route_key"] for row in rows if row["checkpoint_allowed"])
        blocker_counts = Counter("checkpoint_allowed" if row["checkpoint_allowed"] else row["checkpoint_block_reason"] for row in rows)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_gender_longform_false_reopen_checkpoint_runs (
                rule_version,
                route_run_id,
                diagnostic_run_id,
                segment_state_run_id,
                checkpoint_name,
                checkpoint_status,
                promotion_status,
                total_candidates,
                checkpoint_allowed_count,
                checkpoint_blocked_count,
                production_release_allowed,
                route_counts_json,
                blocker_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_route_run_id,
                route_run["diagnostic_run_id"],
                route_run["segment_state_run_id"],
                CHECKPOINT_NAME,
                CHECKPOINT_STATUS if allowed_count else "blocked_by_checkpoint_guard",
                PROMOTION_STATUS if allowed_count else "blocked",
                len(rows),
                allowed_count,
                blocked_count,
                0,
                json.dumps(dict(route_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(blocker_counts), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        checkpoint_run_id = int(cursor.lastrowid)
        for row in rows:
            conn.execute(
                """
                INSERT INTO ml_issue_gender_longform_false_reopen_checkpoint_items (
                    checkpoint_run_id,
                    route_run_id,
                    route_item_id,
                    diagnostic_item_id,
                    diagnostic_run_id,
                    segment_state_run_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    route_key,
                    checkpoint_allowed,
                    checkpoint_action,
                    block_reason,
                    final_state,
                    confirmed_matches_output,
                    needs_output_apply,
                    validation_issue_codes,
                    current_text,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_run_id,
                    selected_route_run_id,
                    row["id"],
                    row["diagnostic_item_id"],
                    row["diagnostic_run_id"],
                    row["segment_state_run_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["route_key"],
                    row["checkpoint_allowed"],
                    row["checkpoint_action"],
                    row["checkpoint_block_reason"],
                    row["final_state"],
                    row["confirmed_matches_output"],
                    row["needs_output_apply"],
                    row["validation_issue_codes"],
                    row["current_text"],
                    now,
                ),
            )
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            checkpoint_run_id=checkpoint_run_id,
            route_run=route_run,
            rows=rows,
            global_reasons=global_reasons,
        )
        conn.commit()

    print(f"Gender longform false-reopen checkpoint: {checkpoint_run_id}")
    print(f"Route run id: {selected_route_run_id}")
    print(f"Allowed: {allowed_count}")
    print(f"Blocked: {blocked_count}")
    print(f"Report: {txt_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "route_run_id": selected_route_run_id,
        "allowed_count": allowed_count,
        "blocked_count": blocked_count,
        "report_path": str(txt_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Checkpoint longform gender false-reopen candidates.")
    parser.add_argument("--route-run-id", type=int, default=None)
    parser.add_argument("--max-blocked-allowed", type=int, default=10)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    main(route_run_id=args.route_run_id, max_blocked_allowed=args.max_blocked_allowed)
