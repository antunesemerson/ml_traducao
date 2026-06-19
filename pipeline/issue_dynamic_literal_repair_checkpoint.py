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
from issue_dynamic_literal_repair_diagnostic import residual_hits


RULE_VERSION = "issue_dynamic_literal_repair_checkpoint_v1"
POLICY_NAME = "dynamic_literal_repair_shadow_v1"
AGENT_KEY = "micro_dynamic_ck3_expression"
SUBPOLICY_NAME = "dynamic_literal_repair"
CHECKPOINT_ACTION = "stage_dynamic_literal_repair_shadow"


def table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        LIMIT 1
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def latest_repair_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_dynamic_literal_repair_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No dynamic literal repair run found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_dynamic_literal_repair_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            repair_run_id INTEGER NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            allowed_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
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
        CREATE TABLE IF NOT EXISTS ml_issue_dynamic_literal_repair_checkpoint_items (
            id INTEGER PRIMARY KEY,
            checkpoint_run_id INTEGER NOT NULL,
            repair_run_id INTEGER NOT NULL,
            repair_item_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            checkpoint_action TEXT NOT NULL,
            block_reason TEXT,
            token_status TEXT NOT NULL,
            current_text TEXT NOT NULL,
            corrected_text TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_dynamic_literal_repair_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def fetch_rows(conn, *, repair_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.*,
            queue.ledger_item_id
        FROM ml_issue_dynamic_literal_repair_items item
        JOIN ml_issue_review_queue_items queue ON queue.id = item.queue_item_id
        WHERE item.run_id = ?
        ORDER BY item.id
        """,
        (repair_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def classify_checkpoint(row: dict[str, Any]) -> tuple[int, str]:
    current = row.get("current_text") or ""
    corrected = row.get("corrected_text") or ""
    if row.get("repair_status") != "ready_shadow":
        return 0, row.get("repair_status") or "not_ready_shadow"
    if row.get("token_status") != "same_structural_tokens":
        return 0, row.get("token_status") or "token_status_not_same_structural"
    if not corrected.strip():
        return 0, "missing_corrected_text"
    if current == corrected:
        return 0, "no_text_delta"
    if structural_tokens(current) != structural_tokens(corrected):
        return 0, "structural_tokens_changed"
    hits = residual_hits(corrected)
    if hits:
        return 0, "residual_after_repair:" + ",".join(hits[:6])
    return 1, ""


def report_paths(settings: dict[str, Any], repair_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_dynamic_literal_repair_checkpoint_run_{repair_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    repair_run_id: int,
    rows: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    fields = [
        "checkpoint_allowed",
        "block_reason",
        "repair_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "token_status",
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

    lines = [
        "Issue dynamic literal repair checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Repair run id: {repair_run_id}",
        f"Policy: {POLICY_NAME}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Allowed: {counts['allowed']:,}",
        f"- Blocked: {counts['blocked']:,}",
        "",
        "Blocks:",
        *[f"- {key}: {value:,}" for key, value in counts.items() if key.startswith("block:")],
        "",
        "Samples:",
    ]
    for row in rows[:80]:
        lines.extend(
            [
                (
                    f"- allowed={row['checkpoint_allowed']} block={row['block_reason'] or 'none'} "
                    f"segment={row['segment_id']} {row['relative_path']}::{row['source_key']}"
                ),
                f"  current: {short(row['current_text'], 220)}",
                f"  corrected: {short(row['corrected_text'], 220)}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This checkpoint records shadow-ready repair evidence only.",
            "- It does not write source/output and does not promote production apply by itself.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, repair_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        if not table_exists(conn, "ml_issue_dynamic_literal_repair_runs"):
            raise RuntimeError("Run issue_dynamic_literal_repair_diagnostic first.")
        ensure_tables(conn)
        selected_repair_run_id = repair_run_id or latest_repair_run_id(conn)
        source_rows = fetch_rows(conn, repair_run_id=selected_repair_run_id)
        classified: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        for source in source_rows:
            allowed, block_reason = classify_checkpoint(source)
            counts["allowed" if allowed else "blocked"] += 1
            if block_reason:
                counts[f"block:{block_reason}"] += 1
            classified.append(
                {
                    "repair_item_id": source["id"],
                    "ledger_item_id": source["ledger_item_id"],
                    "segment_id": source["segment_id"],
                    "relative_path": source["relative_path"],
                    "source_key": source["source_key"],
                    "agent_key": AGENT_KEY,
                    "subpolicy_name": SUBPOLICY_NAME,
                    "checkpoint_allowed": allowed,
                    "checkpoint_action": CHECKPOINT_ACTION,
                    "block_reason": block_reason,
                    "token_status": source["token_status"],
                    "current_text": source["current_text"],
                    "corrected_text": source.get("corrected_text") or "",
                    "reasons_json": source.get("reasons_json") or "[]",
                }
            )
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_repair_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_dynamic_literal_repair_checkpoint_runs (
                rule_version,
                repair_run_id,
                policy_name,
                policy_status,
                candidate_count,
                allowed_count,
                blocked_count,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_repair_run_id,
                POLICY_NAME,
                "shadow",
                len(classified),
                counts["allowed"],
                counts["blocked"],
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        checkpoint_run_id = int(cur.lastrowid)
        for row in classified:
            conn.execute(
                """
                INSERT INTO ml_issue_dynamic_literal_repair_checkpoint_items (
                    checkpoint_run_id,
                    repair_run_id,
                    repair_item_id,
                    ledger_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    agent_key,
                    subpolicy_name,
                    checkpoint_allowed,
                    checkpoint_action,
                    block_reason,
                    token_status,
                    current_text,
                    corrected_text,
                    reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_run_id,
                    selected_repair_run_id,
                    row["repair_item_id"],
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["agent_key"],
                    row["subpolicy_name"],
                    row["checkpoint_allowed"],
                    row["checkpoint_action"],
                    row["block_reason"],
                    row["token_status"],
                    row["current_text"],
                    row["corrected_text"],
                    row["reasons_json"],
                    now,
                ),
            )
        conn.commit()
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            checkpoint_run_id=checkpoint_run_id,
            repair_run_id=selected_repair_run_id,
            rows=classified,
            counts=counts,
        )

    print("[issue_dynamic_literal_repair_checkpoint] Checkpoint generated")
    print(f"[issue_dynamic_literal_repair_checkpoint] Rule version: {RULE_VERSION}")
    print(f"[issue_dynamic_literal_repair_checkpoint] Checkpoint run id: {checkpoint_run_id}")
    print(f"[issue_dynamic_literal_repair_checkpoint] Repair run id: {selected_repair_run_id}")
    print(f"[issue_dynamic_literal_repair_checkpoint] Candidates: {len(classified):,}")
    print(f"[issue_dynamic_literal_repair_checkpoint] Allowed: {counts['allowed']:,}")
    print(f"[issue_dynamic_literal_repair_checkpoint] Blocked: {counts['blocked']:,}")
    print(f"[issue_dynamic_literal_repair_checkpoint] Report: {txt_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "repair_run_id": selected_repair_run_id,
        "candidates": len(classified),
        "allowed": counts["allowed"],
        "blocked": counts["blocked"],
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Checkpoint dynamic literal repair shadow candidates.")
    parser.add_argument("--repair-run-id", type=int, default=None)
    args = parser.parse_args()
    main(repair_run_id=args.repair_run_id)
