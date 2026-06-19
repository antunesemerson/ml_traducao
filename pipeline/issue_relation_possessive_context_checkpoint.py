from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short, structural_tokens


RULE_VERSION = "issue_relation_possessive_context_checkpoint_v1"
DIAGNOSTIC_RULE_VERSION = "issue_relation_possessive_context_diagnostic_v1"
CHECKPOINT_NAME = "relation_possessive_low_risk_boundary_checkpoint_v1"
CHECKPOINT_STATUS = "ready_for_partial_boundary_repair"
PROMOTION_STATUS = "partial_repair_candidate"
ALLOWED_ROUTES = {
    "relation_token_space_before_comma",
    "possessive_glued_to_relation_token",
}


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def report_paths(settings: dict[str, Any], diagnostic_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{now_stamp()}_issue_relation_possessive_context_checkpoint_diag_{diagnostic_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_diagnostic_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_relation_possessive_context_diagnostic_runs
        WHERE rule_version = ?
          AND candidate_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (DIAGNOSTIC_RULE_VERSION,),
    ).fetchone()
    if row is None:
        raise RuntimeError("No relation possessive context diagnostic run found.")
    return int(row["id"])


def fetch_diagnostic(conn, diagnostic_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM ml_issue_relation_possessive_context_diagnostic_runs WHERE id = ?",
        (diagnostic_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Diagnostic run not found: {diagnostic_run_id}")
    return dict(row)


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_relation_possessive_context_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            diagnostic_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            promotion_status TEXT NOT NULL,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_blocked_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            route_counts_json TEXT,
            block_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_relation_possessive_context_checkpoint_items (
            id INTEGER PRIMARY KEY,
            checkpoint_run_id INTEGER NOT NULL,
            diagnostic_run_id INTEGER NOT NULL,
            diagnostic_item_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            route_key TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            checkpoint_action TEXT NOT NULL,
            checkpoint_block_reason TEXT,
            current_text TEXT NOT NULL,
            corrected_text TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_relation_possessive_context_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def fetch_items(conn, diagnostic_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_relation_possessive_context_diagnostic_items
        WHERE run_id = ?
        ORDER BY repair_candidate DESC, route_key, relative_path, source_line_number, segment_id
        """,
        (diagnostic_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def evaluate_item(row: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    route_key = row.get("route_key") or ""
    current = row.get("current_text") or ""
    proposed = row.get("proposed_text") or ""
    if route_key not in ALLOWED_ROUTES:
        reasons.append("route_not_allowed_for_boundary_checkpoint")
    if int(row.get("repair_candidate") or 0) != 1:
        reasons.append("diagnostic_not_repair_candidate")
    if str(row.get("risk_level") or "") != "low":
        reasons.append("risk_not_low")
    if not proposed:
        reasons.append("missing_proposed_text")
    if proposed == current:
        reasons.append("no_text_delta")
    if structural_tokens(current) != structural_tokens(proposed):
        reasons.append("structural_tokens_changed")
    allowed = 0 if reasons else 1
    return {
        **row,
        "checkpoint_allowed": allowed,
        "checkpoint_action": (
            "apply_relation_possessive_boundary_microrepair"
            if allowed
            else "hold_relation_possessive_context"
        ),
        "checkpoint_block_reason": ";".join(reasons),
    }


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    diagnostic: dict[str, Any],
    rows: list[dict[str, Any]],
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
        "current_text",
        "proposed_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    route_counts = Counter(row["route_key"] for row in rows)
    block_counts = Counter(
        "checkpoint_allowed" if row["checkpoint_allowed"] else row["checkpoint_block_reason"]
        for row in rows
    )
    allowed = [row for row in rows if row["checkpoint_allowed"]]
    blocked = [row for row in rows if not row["checkpoint_allowed"]]
    lines = [
        "Issue relation possessive context checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint name: {CHECKPOINT_NAME}",
        f"Checkpoint run id: {run_id}",
        f"Diagnostic run id: {diagnostic['id']}",
        f"Segment-state run id: {diagnostic['segment_state_run_id']}",
        f"Checkpoint status: {CHECKPOINT_STATUS}",
        f"Promotion status: {PROMOTION_STATUS}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Allowed partial repairs: {len(allowed):,}",
        f"- Blocked/held: {len(blocked):,}",
        "",
        "Routes:",
        *[f"- {label}: {count:,}" for label, count in route_counts.most_common()],
        "",
        "Checkpoint counts:",
        *[f"- {label}: {count:,}" for label, count in block_counts.most_common()],
        "",
        "Allowed samples:",
    ]
    for row in allowed[:25]:
        lines.extend(
            [
                f"- {row['route_key']} | segment={row['segment_id']} {row['relative_path']}::{row['source_key']}",
                f"  current: {short(row['current_text'], 220)}",
                f"  corrected: {short(row['proposed_text'], 220)}",
            ]
        )
    lines.extend(["", "Held samples:"])
    for row in blocked[:25]:
        lines.append(
            f"- {row['route_key']} | {row['checkpoint_block_reason']} | "
            f"segment={row['segment_id']} {row['relative_path']}::{row['source_key']}"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Checkpoint only: no confirmations, no source/output writes, no production run.",
            "- Allowed rows are boundary micro-repairs, not semantic relation rewrites.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, diagnostic_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_diagnostic_run_id = diagnostic_run_id or latest_diagnostic_run_id(conn)
        diagnostic = fetch_diagnostic(conn, selected_diagnostic_run_id)
        rows = [evaluate_item(row) for row in fetch_items(conn, selected_diagnostic_run_id)]
        allowed_count = sum(row["checkpoint_allowed"] for row in rows)
        blocked_count = len(rows) - allowed_count
        route_counts = Counter(row["route_key"] for row in rows)
        block_counts = Counter(
            "checkpoint_allowed" if row["checkpoint_allowed"] else row["checkpoint_block_reason"]
            for row in rows
        )
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_diagnostic_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_relation_possessive_context_checkpoint_runs (
                rule_version,
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
                block_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_diagnostic_run_id,
                int(diagnostic["segment_state_run_id"]),
                CHECKPOINT_NAME,
                CHECKPOINT_STATUS,
                PROMOTION_STATUS,
                len(rows),
                allowed_count,
                blocked_count,
                json.dumps(route_counts, ensure_ascii=False, sort_keys=True),
                json.dumps(block_counts, ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
        for row in rows:
            conn.execute(
                """
                INSERT INTO ml_issue_relation_possessive_context_checkpoint_items (
                    checkpoint_run_id,
                    diagnostic_run_id,
                    diagnostic_item_id,
                    segment_state_run_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    route_key,
                    checkpoint_allowed,
                    checkpoint_action,
                    checkpoint_block_reason,
                    current_text,
                    corrected_text,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_diagnostic_run_id,
                    int(row["id"]),
                    int(row["segment_state_run_id"]),
                    int(row["segment_id"]),
                    row["relative_path"],
                    row["source_key"],
                    int(row.get("source_line_number") or 0),
                    row["route_key"],
                    row["checkpoint_allowed"],
                    row["checkpoint_action"],
                    row["checkpoint_block_reason"],
                    row["current_text"],
                    row["proposed_text"] if row["checkpoint_allowed"] else "",
                    now,
                ),
            )
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            diagnostic=diagnostic,
            rows=rows,
        )
        conn.commit()
    print(f"Relation possessive context checkpoint run: {run_id}")
    print(f"Diagnostic run id: {selected_diagnostic_run_id}")
    print(f"Allowed: {allowed_count:,}")
    print(f"Blocked/held: {blocked_count:,}")
    print(f"Report: {txt_path}")
    return {
        "run_id": run_id,
        "diagnostic_run_id": selected_diagnostic_run_id,
        "allowed_count": allowed_count,
        "blocked_count": blocked_count,
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Checkpoint low-risk relation possessive boundary repairs.")
    parser.add_argument("--diagnostic-run-id", type=int)
    args = parser.parse_args()
    main(diagnostic_run_id=args.diagnostic_run_id)
