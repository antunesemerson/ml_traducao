from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_select_cstring_preterite_ptbr_checkpoint_v1"
TARGET_AGENT = "select_cstring_local_player_preterite_verb_rewrite"
CHECKPOINT_ACTION = "checkpoint_select_cstring_preterite_ptbr_shadow_repair"
PRODUCTION_RELEASE_ALLOWED = 0


def latest_shadow_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_select_cstring_preterite_ptbr_shadow_repair_runs
        WHERE target_agent = ?
          AND production_release_allowed = 0
        ORDER BY id DESC
        LIMIT 1
        """,
        (TARGET_AGENT,),
    ).fetchone()
    if row is None:
        raise RuntimeError("No Select_CString preterite shadow repair run found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_preterite_ptbr_checkpoint_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            rule_version TEXT NOT NULL,
            target_agent TEXT NOT NULL,
            source_shadow_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL,
            checkpoint_allowed_count INTEGER NOT NULL,
            blocked_count INTEGER NOT NULL,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            block_counts_json TEXT NOT NULL,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_preterite_ptbr_checkpoint_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            source_shadow_run_id INTEGER NOT NULL,
            source_shadow_item_id INTEGER NOT NULL,
            segment_id INTEGER,
            target_agent TEXT NOT NULL,
            left_literal TEXT NOT NULL,
            right_literal TEXT,
            proposed_left_literal TEXT NOT NULL,
            proposed_right_literal TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            checkpoint_action TEXT NOT NULL,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_select_cstring_preterite_ptbr_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_select_cstring_preterite_ptbr_checkpoint_items_run
        ON ml_issue_select_cstring_preterite_ptbr_checkpoint_items(run_id)
        """
    )


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_select_cstring_preterite_ptbr_checkpoint"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def classify(row: dict[str, Any]) -> dict[str, Any]:
    left = str(row["left_literal"] or "").strip()
    right = str(row["right_literal"] or "").strip()
    proposed_left = str(row["proposed_left_literal"] or "").strip()
    proposed_right = str(row["proposed_right_literal"] or "").strip()
    block_reason = ""
    allowed = 0
    if int(row["shadow_ready"] or 0) != 1:
        block_reason = "shadow_not_ready"
    elif int(row["production_release_allowed"] or 0) != 0:
        block_reason = "shadow_has_production_authority"
    elif not left or not right:
        block_reason = "missing_source_literal_pair"
    elif not proposed_left or not proposed_right:
        block_reason = "missing_ptbr_proposal"
    elif proposed_left != proposed_right:
        block_reason = "non_neutral_branch_proposal_requires_composition"
    else:
        allowed = 1
    return {
        "source_shadow_item_id": int(row["id"]),
        "segment_id": row["segment_id"],
        "target_agent": str(row["target_agent"] or TARGET_AGENT),
        "left_literal": left,
        "right_literal": right,
        "proposed_left_literal": proposed_left,
        "proposed_right_literal": proposed_right,
        "checkpoint_allowed": allowed,
        "block_reason": block_reason,
        "checkpoint_action": CHECKPOINT_ACTION,
        "production_release_allowed": PRODUCTION_RELEASE_ALLOWED,
    }


def build_checkpoint(*, shadow_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    now = datetime.now().isoformat(timespec="seconds")
    report_path, csv_path, jsonl_path = report_paths(settings)
    with db.connect(settings) as conn:
        ensure_tables(conn)
        source_run_id = shadow_run_id or latest_shadow_run_id(conn)
        source_rows = conn.execute(
            """
            SELECT *
            FROM ml_issue_select_cstring_preterite_ptbr_shadow_repair_items
            WHERE run_id = ?
              AND target_agent = ?
            ORDER BY id
            """,
            (source_run_id, TARGET_AGENT),
        ).fetchall()
        items = [classify(dict(row)) for row in source_rows]
        allowed_count = sum(1 for item in items if item["checkpoint_allowed"])
        blocked_count = len(items) - allowed_count
        block_counts = Counter(item["block_reason"] or "allowed" for item in items)
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_select_cstring_preterite_ptbr_checkpoint_runs (
                started_at, finished_at, rule_version, target_agent, source_shadow_run_id,
                candidate_count, checkpoint_allowed_count, blocked_count,
                production_release_allowed, block_counts_json, report_path, csv_path, jsonl_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            """,
            (
                now,
                now,
                RULE_VERSION,
                TARGET_AGENT,
                source_run_id,
                len(items),
                allowed_count,
                blocked_count,
                json.dumps(dict(block_counts), ensure_ascii=False, sort_keys=True),
                str(report_path),
                str(csv_path),
                str(jsonl_path),
            ),
        )
        run_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO ml_issue_select_cstring_preterite_ptbr_checkpoint_items (
                run_id, source_shadow_run_id, source_shadow_item_id, segment_id,
                target_agent, left_literal, right_literal, proposed_left_literal,
                proposed_right_literal, checkpoint_allowed, block_reason,
                checkpoint_action, production_release_allowed, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            [
                (
                    run_id,
                    source_run_id,
                    item["source_shadow_item_id"],
                    item["segment_id"],
                    item["target_agent"],
                    item["left_literal"],
                    item["right_literal"],
                    item["proposed_left_literal"],
                    item["proposed_right_literal"],
                    item["checkpoint_allowed"],
                    item["block_reason"],
                    item["checkpoint_action"],
                    now,
                )
                for item in items
            ],
        )
        conn.commit()

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write(json.dumps({"run_id": run_id, "source_shadow_run_id": source_run_id, **item}, ensure_ascii=False, sort_keys=True) + "\n")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = [
            "run_id",
            "source_shadow_run_id",
            "segment_id",
            "left_literal",
            "right_literal",
            "proposed_left_literal",
            "proposed_right_literal",
            "checkpoint_allowed",
            "block_reason",
            "production_release_allowed",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in items:
            row = {"run_id": run_id, "source_shadow_run_id": source_run_id, **item}
            writer.writerow({key: row.get(key) for key in fieldnames})
    lines = [
        "Select_CString preterite PT-BR checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Source shadow run id: {source_run_id}",
        "",
        "Summary:",
        f"- Candidates: {len(items):,}",
        f"- Checkpoint allowed: {allowed_count:,}",
        f"- Blocked: {blocked_count:,}",
        "- Production release allowed: 0",
        "- Output writes: 0",
        "",
        "Block counts:",
        *[f"- {key}: {value:,}" for key, value in block_counts.most_common()],
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("[issue_select_cstring_preterite_ptbr_checkpoint] Checkpoint complete")
    print(f"[issue_select_cstring_preterite_ptbr_checkpoint] Run id: {run_id}")
    print(f"[issue_select_cstring_preterite_ptbr_checkpoint] Allowed: {allowed_count:,}")
    print(f"[issue_select_cstring_preterite_ptbr_checkpoint] Blocked: {blocked_count:,}")
    print(f"[issue_select_cstring_preterite_ptbr_checkpoint] Report: {report_path}")
    return {
        "run_id": run_id,
        "source_shadow_run_id": source_run_id,
        "candidate_count": len(items),
        "checkpoint_allowed_count": allowed_count,
        "blocked_count": blocked_count,
        "report_path": str(report_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a governed learning checkpoint for Select_CString preterite PT-BR shadow repairs.")
    parser.add_argument("--shadow-run-id", type=int, default=None)
    args = parser.parse_args()
    build_checkpoint(shadow_run_id=args.shadow_run_id)
