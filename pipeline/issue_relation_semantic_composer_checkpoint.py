from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "issue_relation_semantic_composer_checkpoint_v1"
COMPOSER_RULE_VERSION = "issue_relation_semantic_composer_dry_run_v2"
CHECKPOINT_NAME = "relation_semantic_composer_reviewed_boundary_checkpoint_v1"
CHECKPOINT_STATUS = "ready_for_protected_apply"
PROMOTION_STATUS = "relation_semantic_composer_reviewed_repair"

POSSESSIVE_PAIR_RE = re.compile(r"\b(?:meu/minha|minha/meu|meu\(a\))\s+\[", re.IGNORECASE)
LEADING_MY_RE = re.compile(r"^\s*(?:Meu|Minha|meu|minha)\s+\[", re.IGNORECASE)
LEADING_THEIR_RE = re.compile(r"^\s*(?:Seu|Sua|seu|sua)\s+\[", re.IGNORECASE)


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def report_paths(settings: dict[str, Any], composer_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{now_stamp()}_issue_relation_semantic_composer_checkpoint_run_{composer_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_composer_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_relation_semantic_composer_runs
        WHERE rule_version = ?
          AND composed_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (COMPOSER_RULE_VERSION,),
    ).fetchone()
    if row is None:
        raise RuntimeError("No relation semantic composer run found.")
    return int(row["id"])


def fetch_composer_run(conn, composer_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_relation_semantic_composer_runs
        WHERE id = ?
        """,
        (composer_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Composer run not found: {composer_run_id}")
    return dict(row)


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_relation_semantic_composer_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            composer_run_id INTEGER NOT NULL,
            diagnostic_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            promotion_status TEXT NOT NULL,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_blocked_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            blocker_counts_json TEXT,
            route_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_relation_semantic_composer_checkpoint_items (
            id INTEGER PRIMARY KEY,
            checkpoint_run_id INTEGER NOT NULL,
            composer_run_id INTEGER NOT NULL,
            composer_item_id INTEGER NOT NULL,
            diagnostic_item_id INTEGER NOT NULL,
            diagnostic_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            route_key TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            checkpoint_action TEXT NOT NULL,
            block_reason TEXT,
            current_text TEXT NOT NULL,
            corrected_text TEXT,
            english_text TEXT,
            spanish_text TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_relation_semantic_composer_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def fetch_items(conn, composer_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_relation_semantic_composer_items
        WHERE run_id = ?
        ORDER BY relative_path, source_line_number, source_key, segment_id
        """,
        (composer_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def evaluate_item(item: dict[str, Any]) -> dict[str, Any]:
    current = item.get("current_text") or ""
    corrected = item.get("corrected_text") or ""
    reasons: list[str] = []

    if item.get("composer_status") != "composed_review_required":
        reasons.append(item.get("block_reason") or "composer_item_not_ready")
    if not corrected:
        reasons.append("missing_corrected_text")
    if current == corrected:
        reasons.append("no_text_delta")
    if "|U] ," in corrected:
        reasons.append("relation_token_comma_spacing_not_normalized")
    if LEADING_THEIR_RE.search(current):
        reasons.append("leading_seu_sua_requires_owner_review")
    if "[liege_to_challenge.Custom2(" in current:
        reasons.append("non_root_relation_owner_requires_context_review")
    if not (LEADING_MY_RE.search(current) or POSSESSIVE_PAIR_RE.search(current)):
        reasons.append("not_my_relation_surface")

    allowed = 0 if reasons else 1
    return {
        **item,
        "checkpoint_allowed": allowed,
        "checkpoint_action": "apply_relation_semantic_composer_reviewed_repair",
        "checkpoint_block_reason": ";".join(reasons),
    }


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    composer_run: dict[str, Any],
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
        "corrected_text",
        "english_text",
        "spanish_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    blocker_counts = Counter(
        "checkpoint_allowed" if row["checkpoint_allowed"] else row["checkpoint_block_reason"]
        for row in rows
    )
    route_counts = Counter(row["route_key"] for row in rows if row["checkpoint_allowed"])
    allowed_rows = [row for row in rows if row["checkpoint_allowed"]]
    blocked_rows = [row for row in rows if not row["checkpoint_allowed"]]

    lines = [
        "Issue relation semantic composer checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint name: {CHECKPOINT_NAME}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Composer run id: {composer_run['id']}",
        f"Diagnostic run id: {composer_run['diagnostic_run_id']}",
        f"Segment-state run id: {composer_run['segment_state_run_id']}",
        f"Checkpoint status: {CHECKPOINT_STATUS}",
        f"Promotion status: {PROMOTION_STATUS}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Allowed protected repairs: {len(allowed_rows):,}",
        f"- Blocked for review: {len(blocked_rows):,}",
        "",
        "Checkpoint counts:",
        *[f"- {key}: {value:,}" for key, value in blocker_counts.most_common()],
        "",
        "Allowed routes:",
        *[f"- {key}: {value:,}" for key, value in route_counts.most_common()],
        "",
        "Allowed samples:",
    ]
    for row in allowed_rows[:25]:
        lines.extend(
            [
                f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']}",
                f"  current: {short(row['current_text'], 220)}",
                f"  corrected: {short(row['corrected_text'], 220)}",
            ]
        )
    lines.extend(["", "Blocked samples:"])
    for row in blocked_rows[:25]:
        lines.extend(
            [
                f"- segment={row['segment_id']} reason={row['checkpoint_block_reason']} {row['relative_path']}::{row['source_key']}",
                f"  current: {short(row['current_text'], 220)}",
                f"  proposed: {short(row['corrected_text'], 220)}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Checkpoint only: no confirmations, no source/output writes, no production run.",
            "- Allowed rows are limited to my/meu/minha RelationToMe surfaces or explicit meu(a) pair forms.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--composer-run-id", type=int)
    args = parser.parse_args()

    settings = db.load_settings()
    conn = db.connect(settings)
    ensure_tables(conn)

    composer_run_id = args.composer_run_id or latest_composer_run_id(conn)
    composer_run = fetch_composer_run(conn, composer_run_id)
    if composer_run["rule_version"] != COMPOSER_RULE_VERSION:
        raise RuntimeError(
            f"Unexpected composer rule version: {composer_run['rule_version']} != {COMPOSER_RULE_VERSION}"
        )

    started = datetime.now().isoformat(timespec="seconds")
    txt_path, csv_path, jsonl_path = report_paths(settings, composer_run_id)
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_relation_semantic_composer_checkpoint_runs (
                rule_version,
                composer_run_id,
                diagnostic_run_id,
                segment_state_run_id,
                checkpoint_name,
                checkpoint_status,
                promotion_status,
                started_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                composer_run_id,
                composer_run["diagnostic_run_id"],
                composer_run["segment_state_run_id"],
                CHECKPOINT_NAME,
                CHECKPOINT_STATUS,
                PROMOTION_STATUS,
                started,
                started,
            ),
        )
        checkpoint_run_id = int(cursor.lastrowid)

    rows = [evaluate_item(row) for row in fetch_items(conn, composer_run_id)]
    allowed = sum(1 for row in rows if row["checkpoint_allowed"])
    blocked = len(rows) - allowed
    blocker_counts = Counter(
        "checkpoint_allowed" if row["checkpoint_allowed"] else row["checkpoint_block_reason"]
        for row in rows
    )
    route_counts = Counter(row["route_key"] for row in rows if row["checkpoint_allowed"])

    write_reports(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        checkpoint_run_id=checkpoint_run_id,
        composer_run=composer_run,
        rows=rows,
    )

    finished = datetime.now().isoformat(timespec="seconds")
    with conn:
        for row in rows:
            conn.execute(
                """
                INSERT INTO ml_issue_relation_semantic_composer_checkpoint_items (
                    checkpoint_run_id,
                    composer_run_id,
                    composer_item_id,
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
                    current_text,
                    corrected_text,
                    english_text,
                    spanish_text,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_run_id,
                    composer_run_id,
                    row["id"],
                    row["diagnostic_item_id"],
                    row["diagnostic_run_id"],
                    row["segment_state_run_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    row["route_key"],
                    row["checkpoint_allowed"],
                    row["checkpoint_action"],
                    row["checkpoint_block_reason"],
                    row["current_text"],
                    row["corrected_text"],
                    row.get("english_text"),
                    row.get("spanish_text"),
                    finished,
                ),
            )
        conn.execute(
            """
            UPDATE ml_issue_relation_semantic_composer_checkpoint_runs
            SET total_candidates = ?,
                checkpoint_allowed_count = ?,
                checkpoint_blocked_count = ?,
                blocker_counts_json = ?,
                route_counts_json = ?,
                report_path = ?,
                csv_path = ?,
                jsonl_path = ?,
                finished_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                len(rows),
                allowed,
                blocked,
                json.dumps(dict(blocker_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(route_counts), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                finished,
                finished,
                checkpoint_run_id,
            ),
        )

    print(f"Relation semantic composer checkpoint: {checkpoint_run_id}")
    print(f"Composer run id: {composer_run_id}")
    print(f"Candidates: {len(rows)}")
    print(f"Allowed protected repairs: {allowed}")
    print(f"Blocked for review: {blocked}")
    print(f"Report: {txt_path}")


if __name__ == "__main__":
    main()
