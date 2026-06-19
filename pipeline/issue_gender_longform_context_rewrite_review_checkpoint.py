from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_gender_longform_context_rewrite_review_checkpoint_v1"
DECISION_RULE_VERSION = "issue_gender_longform_context_rewrite_review_decisions_v1"
CHECKPOINT_NAME = "gender_longform_context_rewrite_review_checkpoint_v1"
CHECKPOINT_STATUS = "ready_for_protected_apply"
PROMOTION_STATUS = "reviewed_context_rewrite_candidate"
APPROVED_SEGMENTS = {76454, 159267, 160285, 160412}


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def report_paths(settings: dict[str, Any], decision_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{now_stamp()}_issue_gender_longform_context_rewrite_review_checkpoint_decision_{decision_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_decision_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_gender_longform_context_rewrite_review_decision_runs
        WHERE rule_version = ?
          AND candidate_count = 4
          AND approved_count = 4
          AND blocked_count = 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (DECISION_RULE_VERSION,),
    ).fetchone()
    if row is None:
        raise RuntimeError("No approved 4-item gender longform context rewrite decision run found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_gender_longform_context_rewrite_review_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            decision_run_id INTEGER NOT NULL,
            queue_run_id INTEGER NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            promotion_status TEXT NOT NULL,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_review_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_blocked_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            status_counts_json TEXT,
            token_delta_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_gender_longform_context_rewrite_review_checkpoint_items (
            id INTEGER PRIMARY KEY,
            checkpoint_run_id INTEGER NOT NULL,
            decision_run_id INTEGER NOT NULL,
            decision_item_id INTEGER NOT NULL,
            queue_run_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            checkpoint_action TEXT NOT NULL,
            block_reason TEXT,
            decision_status TEXT NOT NULL,
            route_key TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            extraction_strategy TEXT NOT NULL,
            old_fragment TEXT NOT NULL,
            approved_fragment TEXT NOT NULL,
            current_text TEXT NOT NULL,
            corrected_text TEXT NOT NULL,
            token_delta_status TEXT NOT NULL,
            validator_issue_count INTEGER NOT NULL DEFAULT 0,
            validator_high_count INTEGER NOT NULL DEFAULT 0,
            reviewer_notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_gender_longform_context_rewrite_review_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def fetch_decision_run(conn, decision_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_gender_longform_context_rewrite_review_decision_runs
        WHERE id = ?
        """,
        (decision_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Decision run not found: {decision_run_id}")
    return dict(row)


def fetch_decision_items(conn, decision_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_gender_longform_context_rewrite_review_decision_items
        WHERE run_id = ?
        ORDER BY segment_id
        """,
        (decision_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def evaluate_item(row: dict[str, Any], *, global_reasons: list[str]) -> dict[str, Any]:
    reasons = list(global_reasons)
    segment_id = int(row["segment_id"])
    if segment_id not in APPROVED_SEGMENTS:
        reasons.append("segment_not_in_context_checkpoint_allowlist")
    if row.get("decision_status") != "approved_context_rewrite":
        reasons.append(f"decision_not_approved:{row.get('decision_status')}")
    if int(row.get("validator_high_count") or 0) != 0:
        reasons.append("validator_high_count_not_zero")
    if not row.get("old_fragment") or not row.get("approved_fragment") or not row.get("corrected_text"):
        reasons.append("missing_review_payload")
    if row.get("old_fragment") not in (row.get("current_text") or ""):
        reasons.append("old_fragment_not_found_in_current")
    allowed = 0 if reasons else 1
    return {
        **row,
        "checkpoint_allowed": allowed,
        "checkpoint_action": "stage_gender_longform_context_rewrite_reviewed_apply" if allowed else "preserve_for_manual_review",
        "checkpoint_block_reason": ";".join(reasons),
    }


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    decision_run: dict[str, Any],
    rows: list[dict[str, Any]],
    global_reasons: list[str],
) -> None:
    fields = [
        "checkpoint_allowed",
        "checkpoint_action",
        "checkpoint_block_reason",
        "decision_status",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "route_key",
        "subpolicy_name",
        "extraction_strategy",
        "token_delta_status",
        "validator_issue_count",
        "validator_high_count",
        "old_fragment",
        "approved_fragment",
        "corrected_text",
        "reviewer_notes",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    counts = Counter("checkpoint_allowed" if row["checkpoint_allowed"] else row["checkpoint_block_reason"] for row in rows)
    token_counts = Counter(row["token_delta_status"] for row in rows)
    allowed = [row for row in rows if row["checkpoint_allowed"]]
    blocked = [row for row in rows if not row["checkpoint_allowed"]]
    lines = [
        "Issue gender longform context rewrite reviewed checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint name: {CHECKPOINT_NAME}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Decision run id: {decision_run['id']}",
        f"Queue run id: {decision_run['queue_run_id']}",
        f"Checkpoint status: {CHECKPOINT_STATUS}",
        f"Promotion status: {PROMOTION_STATUS}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Allowed: {len(allowed):,}",
        f"- Preserved/review: {len(blocked):,}",
        "",
        "Global blockers:",
        *([f"- {reason}" for reason in global_reasons] or ["- none"]),
        "",
        "Checkpoint counts:",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        "",
        "Token delta:",
        *[f"- {key}: {value:,}" for key, value in token_counts.most_common()],
        "",
        "Allowed items:",
    ]
    for row in allowed:
        lines.extend(
            [
                f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']}",
                f"  action: {row['checkpoint_action']}",
                f"  token_delta: {row['token_delta_status']}",
                f"  old: {row['old_fragment']}",
                f"  approved: {row['approved_fragment']}",
            ]
        )
    lines.extend(["", "Preserved items:"])
    if blocked:
        for row in blocked:
            lines.append(
                f"- {row['checkpoint_block_reason']} | segment={row['segment_id']} "
                f"{row['relative_path']}::{row['source_key']}"
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Checkpoint only: no production run, no confirmations, no source/output writes.",
            "- Apply must validate the exact 4 allowed segments and live output before writing.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, decision_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = db.utc_now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_decision_run_id = decision_run_id or latest_decision_run_id(conn)
        decision_run = fetch_decision_run(conn, selected_decision_run_id)
        source_rows = fetch_decision_items(conn, selected_decision_run_id)
        global_reasons: list[str] = []
        if decision_run.get("rule_version") != DECISION_RULE_VERSION:
            global_reasons.append("decision_rule_version_mismatch")
        if int(decision_run.get("candidate_count") or 0) != 4:
            global_reasons.append("decision_candidate_count_not_4")
        if int(decision_run.get("approved_count") or 0) != 4:
            global_reasons.append("decision_approved_count_not_4")
        if int(decision_run.get("blocked_count") or 0) != 0:
            global_reasons.append("decision_blocked_count_not_0")
        if {int(row["segment_id"]) for row in source_rows} != APPROVED_SEGMENTS:
            global_reasons.append("decision_segment_set_mismatch")

        rows = [evaluate_item(row, global_reasons=global_reasons) for row in source_rows]
        status_counts = Counter(row["checkpoint_action"] for row in rows)
        token_counts = Counter(row["token_delta_status"] for row in rows)
        block_counts = Counter(row["checkpoint_block_reason"] for row in rows if row["checkpoint_block_reason"])
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_decision_run_id)
        now = db.utc_now()
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_gender_longform_context_rewrite_review_checkpoint_runs (
                rule_version,
                decision_run_id,
                queue_run_id,
                checkpoint_name,
                checkpoint_status,
                promotion_status,
                total_candidates,
                checkpoint_allowed_count,
                checkpoint_review_count,
                checkpoint_blocked_count,
                production_release_allowed,
                status_counts_json,
                token_delta_counts_json,
                block_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_decision_run_id,
                int(decision_run["queue_run_id"]),
                CHECKPOINT_NAME,
                CHECKPOINT_STATUS,
                PROMOTION_STATUS,
                len(rows),
                sum(1 for row in rows if row["checkpoint_allowed"]),
                sum(1 for row in rows if not row["checkpoint_allowed"]),
                0,
                json.dumps(dict(status_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(token_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(block_counts), ensure_ascii=False, sort_keys=True),
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
                INSERT INTO ml_issue_gender_longform_context_rewrite_review_checkpoint_items (
                    checkpoint_run_id,
                    decision_run_id,
                    decision_item_id,
                    queue_run_id,
                    queue_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    checkpoint_allowed,
                    checkpoint_action,
                    block_reason,
                    decision_status,
                    route_key,
                    subpolicy_name,
                    extraction_strategy,
                    old_fragment,
                    approved_fragment,
                    current_text,
                    corrected_text,
                    token_delta_status,
                    validator_issue_count,
                    validator_high_count,
                    reviewer_notes,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_run_id,
                    selected_decision_run_id,
                    row["id"],
                    int(decision_run["queue_run_id"]),
                    row["queue_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["checkpoint_allowed"],
                    row["checkpoint_action"],
                    row["checkpoint_block_reason"],
                    row["decision_status"],
                    row["route_key"],
                    row["subpolicy_name"],
                    row["extraction_strategy"],
                    row["old_fragment"],
                    row["approved_fragment"],
                    row["current_text"],
                    row["corrected_text"],
                    row["token_delta_status"],
                    row["validator_issue_count"],
                    row["validator_high_count"],
                    row["reviewer_notes"],
                    now,
                ),
            )
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            checkpoint_run_id=checkpoint_run_id,
            decision_run=decision_run,
            rows=rows,
            global_reasons=global_reasons,
        )
        conn.commit()

    allowed_count = sum(1 for row in rows if row["checkpoint_allowed"])
    blocked_count = len(rows) - allowed_count
    print(f"Gender longform context rewrite reviewed checkpoint: {checkpoint_run_id}")
    print(f"Decision run id: {selected_decision_run_id}")
    print(f"Candidates: {len(rows)}")
    print(f"Allowed: {allowed_count}")
    print(f"Blocked: {blocked_count}")
    print(f"Report: {txt_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "decision_run_id": selected_decision_run_id,
        "candidate_count": len(rows),
        "allowed_count": allowed_count,
        "blocked_count": blocked_count,
        "report_path": str(txt_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create checkpoint for reviewed gender longform context rewrites.")
    parser.add_argument("--decision-run-id", type=int, default=None)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    main(decision_run_id=args.decision_run_id)
