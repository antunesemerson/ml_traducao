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


RULE_VERSION = "issue_gender_es_oa_final_vowel_trim_checkpoint_v1"
DRY_RUN_RULE_VERSION = "issue_gender_es_oa_final_vowel_trim_dry_run_v1"
CHECKPOINT_NAME = "gender_es_oa_final_vowel_trim_partial_repair_checkpoint_v1"
CHECKPOINT_STATUS = "ready_for_partial_repair_observation"
PROMOTION_STATUS = "partial_repair_candidate"
READY_STATUS = "ready_partial_repair"


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def report_paths(settings: dict[str, Any], dry_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{now_stamp()}_issue_gender_es_oa_final_vowel_trim_checkpoint_dryrun_{dry_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_dry_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_gender_es_oa_final_vowel_trim_runs
        WHERE rule_version = ?
          AND ready_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (DRY_RUN_RULE_VERSION,),
    ).fetchone()
    if row is None:
        raise RuntimeError("No ES_OA final-vowel trim dry-run found.")
    return int(row["id"])


def fetch_dry_run(conn, dry_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_gender_es_oa_final_vowel_trim_runs
        WHERE id = ?
        """,
        (dry_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Dry-run not found: {dry_run_id}")
    return dict(row)


def fetch_items(conn, dry_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_gender_es_oa_final_vowel_trim_items
        WHERE run_id = ?
        ORDER BY repair_status DESC, relative_path, source_line_number, source_key, segment_id
        """,
        (dry_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_gender_es_oa_final_vowel_trim_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            dry_run_id INTEGER NOT NULL,
            diagnostic_run_id INTEGER NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            promotion_status TEXT NOT NULL,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_blocked_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            blocker_counts_json TEXT,
            surface_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_gender_es_oa_final_vowel_trim_checkpoint_items (
            id INTEGER PRIMARY KEY,
            checkpoint_run_id INTEGER NOT NULL,
            dry_run_id INTEGER NOT NULL,
            dry_run_item_id INTEGER NOT NULL,
            diagnostic_item_id INTEGER NOT NULL,
            diagnostic_run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            checkpoint_action TEXT NOT NULL,
            block_reason TEXT,
            replacement_count INTEGER NOT NULL DEFAULT 0,
            replaced_surfaces TEXT,
            current_text TEXT NOT NULL,
            corrected_text TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_gender_es_oa_final_vowel_trim_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def evaluate_item(item: dict[str, Any], *, global_reasons: list[str]) -> dict[str, Any]:
    reasons = list(global_reasons)
    if item.get("repair_status") != READY_STATUS:
        reasons.append(item.get("block_reason") or "dry_run_item_not_ready")
    if not item.get("corrected_text"):
        reasons.append("missing_corrected_text")
    if int(item.get("replacement_count") or 0) <= 0:
        reasons.append("missing_replacement")
    allowed = 0 if reasons else 1
    return {
        **item,
        "checkpoint_allowed": allowed,
        "checkpoint_action": "observe_es_oa_final_vowel_trim_partial_repair",
        "checkpoint_block_reason": ";".join(reasons),
    }


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    dry_run: dict[str, Any],
    rows: list[dict[str, Any]],
    global_reasons: list[str],
) -> None:
    fields = [
        "checkpoint_allowed",
        "checkpoint_action",
        "checkpoint_block_reason",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "replacement_count",
        "replaced_surfaces",
        "current_text",
        "corrected_text",
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
    surface_counts = Counter()
    for row in rows:
        if not row["checkpoint_allowed"]:
            continue
        for surface in (row.get("replaced_surfaces") or "").split(","):
            if surface:
                surface_counts[surface] += 1

    lines = [
        "Issue gender ES_OA final-vowel trim checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint name: {CHECKPOINT_NAME}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Dry-run id: {dry_run['id']}",
        f"Diagnostic run id: {dry_run['diagnostic_run_id']}",
        f"Checkpoint status: {CHECKPOINT_STATUS}",
        f"Promotion status: {PROMOTION_STATUS}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Allowed partial repairs: {sum(1 for row in rows if row['checkpoint_allowed']):,}",
        f"- Blocked: {sum(1 for row in rows if not row['checkpoint_allowed']):,}",
        "",
        "Global blockers:",
        *([f"- {reason}" for reason in global_reasons] or ["- none"]),
        "",
        "Checkpoint counts:",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        "",
        "Top allowed surfaces:",
        *[f"- {key}: {value:,}" for key, value in surface_counts.most_common(20)],
        "",
        "Allowed samples:",
    ]
    for row in [item for item in rows if item["checkpoint_allowed"]][:25]:
        lines.extend(
            [
                f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']}",
                f"  current: {short(row['current_text'], 200)}",
                f"  corrected: {short(row['corrected_text'], 200)}",
            ]
        )
    blocked = [item for item in rows if not item["checkpoint_allowed"]]
    lines.extend(["", "Blocked samples:"])
    if blocked:
        for row in blocked[:25]:
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
            "- Production release remains disabled; this only records partial-repair knowledge.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, dry_run_id: int | None = None, max_blocked_allowed: int = 5) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_dry_run_id = dry_run_id or latest_dry_run_id(conn)
        dry_run = fetch_dry_run(conn, selected_dry_run_id)
        source_rows = fetch_items(conn, selected_dry_run_id)
        global_reasons: list[str] = []
        if dry_run.get("rule_version") != DRY_RUN_RULE_VERSION:
            global_reasons.append("wrong_dry_run_rule_version")
        if int(dry_run.get("ready_count") or 0) <= 0:
            global_reasons.append("dry_run_has_no_ready_items")
        if int(dry_run.get("blocked_count") or 0) > max_blocked_allowed:
            global_reasons.append("too_many_blocked_items")
        if not source_rows:
            global_reasons.append("no_checkpoint_items")
        rows = [evaluate_item(row, global_reasons=global_reasons) for row in source_rows]
        allowed_count = sum(1 for row in rows if row["checkpoint_allowed"])
        blocked_count = len(rows) - allowed_count
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_dry_run_id)
        blocker_counts = Counter("checkpoint_allowed" if row["checkpoint_allowed"] else row["checkpoint_block_reason"] for row in rows)
        surface_counts = Counter()
        for row in rows:
            if not row["checkpoint_allowed"]:
                continue
            for surface in (row.get("replaced_surfaces") or "").split(","):
                if surface:
                    surface_counts[surface] += 1
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_gender_es_oa_final_vowel_trim_checkpoint_runs (
                rule_version,
                dry_run_id,
                diagnostic_run_id,
                checkpoint_name,
                checkpoint_status,
                promotion_status,
                total_candidates,
                checkpoint_allowed_count,
                checkpoint_blocked_count,
                production_release_allowed,
                blocker_counts_json,
                surface_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_dry_run_id,
                dry_run["diagnostic_run_id"],
                CHECKPOINT_NAME,
                CHECKPOINT_STATUS if allowed_count else "blocked_by_checkpoint_guard",
                PROMOTION_STATUS if allowed_count else "blocked",
                len(rows),
                allowed_count,
                blocked_count,
                0,
                json.dumps(dict(blocker_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(surface_counts), ensure_ascii=False, sort_keys=True),
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
                INSERT INTO ml_issue_gender_es_oa_final_vowel_trim_checkpoint_items (
                    checkpoint_run_id,
                    dry_run_id,
                    dry_run_item_id,
                    diagnostic_item_id,
                    diagnostic_run_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    checkpoint_allowed,
                    checkpoint_action,
                    block_reason,
                    replacement_count,
                    replaced_surfaces,
                    current_text,
                    corrected_text,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_run_id,
                    selected_dry_run_id,
                    row["id"],
                    row["diagnostic_item_id"],
                    row["diagnostic_run_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["checkpoint_allowed"],
                    row["checkpoint_action"],
                    row["checkpoint_block_reason"],
                    row["replacement_count"],
                    row["replaced_surfaces"],
                    row["current_text"],
                    row["corrected_text"],
                    now,
                ),
            )
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            checkpoint_run_id=checkpoint_run_id,
            dry_run=dry_run,
            rows=rows,
            global_reasons=global_reasons,
        )
        conn.commit()

    print(f"Gender ES_OA final-vowel trim checkpoint: {checkpoint_run_id}")
    print(f"Dry-run id: {selected_dry_run_id}")
    print(f"Allowed: {allowed_count}")
    print(f"Blocked: {blocked_count}")
    print(f"Report: {txt_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "dry_run_id": selected_dry_run_id,
        "allowed_count": allowed_count,
        "blocked_count": blocked_count,
        "report_path": str(txt_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Checkpoint ES_OA final-vowel trim partial repair candidates.")
    parser.add_argument("--dry-run-id", type=int, default=None)
    parser.add_argument("--max-blocked-allowed", type=int, default=5)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    main(dry_run_id=args.dry_run_id, max_blocked_allowed=args.max_blocked_allowed)
