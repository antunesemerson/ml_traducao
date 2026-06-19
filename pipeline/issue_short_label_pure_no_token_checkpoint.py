from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_short_label_pure_no_token_checkpoint_v1"
POLICY_NAME = "short_label_pure_no_token_nominal_shadow"
CHECKPOINT_NAME = "short_label_pure_no_token_nominal_checkpoint_v1"
CHECKPOINT_ACTION = "cover_short_label_pure_no_token_nominal"
AGENT_KEY = "micro_short_label_style"


def latest_shadow_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_pure_no_token_shadow_policy_runs
        WHERE finished_at IS NOT NULL
          AND policy_name = ?
          AND shadow_ready_count > 0
        ORDER BY id DESC
        LIMIT 1
        """,
        (POLICY_NAME,),
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished pure no-token shadow policy run found.")
    return int(row["id"])


def report_base(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    return reports_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_issue_short_label_pure_no_token_checkpoint"


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_pure_no_token_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            promotion_status TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            shadow_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_blocked_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            reason_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_short_label_pure_no_token_checkpoint_items (
            id INTEGER PRIMARY KEY,
            checkpoint_run_id INTEGER NOT NULL,
            shadow_run_id INTEGER NOT NULL,
            shadow_item_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            issue_family TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            source_decision TEXT,
            source_reason TEXT,
            evidence_text TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_short_label_pure_no_token_checkpoint_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_short_label_pure_checkpoint_items_run
        ON ml_issue_short_label_pure_no_token_checkpoint_items(checkpoint_run_id, checkpoint_allowed, segment_id);

        CREATE INDEX IF NOT EXISTS idx_short_label_pure_checkpoint_items_ledger
        ON ml_issue_short_label_pure_no_token_checkpoint_items(ledger_item_id, checkpoint_allowed);
        """
    )


def fetch_shadow_run(conn, *, shadow_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_pure_no_token_shadow_policy_runs
        WHERE id = ?
        """,
        (shadow_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Shadow run not found: {shadow_run_id}")
    return dict(row)


def fetch_rows(conn, *, shadow_run_id: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT item.*
            FROM ml_issue_short_label_pure_no_token_shadow_policy_items item
            WHERE item.run_id = ?
            ORDER BY item.relative_path, item.source_line_number, item.segment_id
            """,
            (shadow_run_id,),
        )
    ]


def row_block_reason(row: dict[str, Any]) -> str:
    if row.get("shadow_status") != "shadow_ready":
        return row.get("validation_issue") or row.get("reason") or "shadow_not_ready"
    if row.get("decision") != "safe_short_label":
        return "decision_not_safe_short_label"
    if row.get("confidence_label") != "positive":
        return "confidence_not_positive"
    if row.get("validation_issue"):
        return "validation_issue_present"
    if any(marker in (row.get("evidence_text") or "") for marker in ("[", "]", "$", "{", "}")):
        return "unexpected_token_marker"
    return ""


def main(*, shadow_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = db.utc_now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_shadow = shadow_run_id or latest_shadow_run_id(conn)
        shadow_run = fetch_shadow_run(conn, shadow_run_id=selected_shadow)
        rows = fetch_rows(conn, shadow_run_id=selected_shadow)

        global_reasons: list[str] = []
        if shadow_run.get("policy_name") != POLICY_NAME:
            global_reasons.append("wrong_policy_name")
        if shadow_run.get("agent_key") != AGENT_KEY:
            global_reasons.append("wrong_agent_key")
        if int(shadow_run.get("production_release_allowed") or 0) != 0:
            global_reasons.append("shadow_unexpectedly_has_production_authority")
        if not rows:
            global_reasons.append("no_shadow_items")

        items: list[dict[str, Any]] = []
        reason_counts: Counter[str] = Counter()
        for row in rows:
            reason = ";".join(global_reasons) if global_reasons else row_block_reason(row)
            allowed = 0 if reason else 1
            reason_counts[reason or "allowed"] += 1
            items.append(
                {
                    "shadow_item_id": row["id"],
                    "ledger_item_id": row["ledger_item_id"],
                    "segment_id": row["segment_id"],
                    "relative_path": row["relative_path"],
                    "source_key": row["source_key"],
                    "source_line_number": row["source_line_number"],
                    "issue_family": "short_label_style_microagent",
                    "agent_key": AGENT_KEY,
                    "checkpoint_action": CHECKPOINT_ACTION,
                    "checkpoint_allowed": allowed,
                    "block_reason": reason,
                    "source_decision": row["decision"],
                    "source_reason": row["reason"],
                    "evidence_text": row["evidence_text"],
                }
            )

        allowed_count = sum(item["checkpoint_allowed"] for item in items)
        blocked_count = len(items) - allowed_count
        checkpoint_status = "ready_for_lifecycle_policy" if allowed_count > 0 and not global_reasons else "blocked"
        promotion_status = "shadow_checkpoint_candidate" if checkpoint_status == "ready_for_lifecycle_policy" else "blocked"

        cursor = conn.execute(
            """
            INSERT INTO ml_issue_short_label_pure_no_token_checkpoint_runs (
                rule_version, checkpoint_name, checkpoint_status, promotion_status,
                policy_name, agent_key, shadow_run_id, candidate_count,
                checkpoint_allowed_count, checkpoint_blocked_count,
                production_release_allowed, reason_counts_json, started_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                CHECKPOINT_NAME,
                checkpoint_status,
                promotion_status,
                POLICY_NAME,
                AGENT_KEY,
                selected_shadow,
                len(items),
                allowed_count,
                blocked_count,
                json.dumps(dict(reason_counts), ensure_ascii=False, sort_keys=True),
                started_at,
                started_at,
            ),
        )
        checkpoint_run_id = int(cursor.lastrowid)
        created_at = db.utc_now()
        conn.executemany(
            """
            INSERT INTO ml_issue_short_label_pure_no_token_checkpoint_items (
                checkpoint_run_id, shadow_run_id, shadow_item_id, ledger_item_id,
                segment_id, relative_path, source_key, source_line_number,
                issue_family, agent_key, checkpoint_action, checkpoint_allowed,
                block_reason, source_decision, source_reason, evidence_text, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    checkpoint_run_id,
                    selected_shadow,
                    item["shadow_item_id"],
                    item["ledger_item_id"],
                    item["segment_id"],
                    item["relative_path"],
                    item["source_key"],
                    item["source_line_number"],
                    item["issue_family"],
                    item["agent_key"],
                    item["checkpoint_action"],
                    item["checkpoint_allowed"],
                    item["block_reason"],
                    item["source_decision"],
                    item["source_reason"],
                    item["evidence_text"],
                    created_at,
                )
                for item in items
            ],
        )

        base = report_base(settings)
        txt_path = base.with_suffix(".txt")
        csv_path = base.with_suffix(".csv")
        jsonl_path = base.with_suffix(".jsonl")
        conn.execute(
            """
            UPDATE ml_issue_short_label_pure_no_token_checkpoint_runs
            SET report_path = ?, csv_path = ?, jsonl_path = ?,
                finished_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (str(txt_path), str(csv_path), str(jsonl_path), db.utc_now(), db.utc_now(), checkpoint_run_id),
        )
        conn.commit()

    fields = [
        "checkpoint_run_id",
        "shadow_run_id",
        "shadow_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "issue_family",
        "agent_key",
        "checkpoint_action",
        "checkpoint_allowed",
        "block_reason",
        "source_decision",
        "source_reason",
        "evidence_text",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in items:
            writer.writerow({"checkpoint_run_id": checkpoint_run_id, "shadow_run_id": selected_shadow, **item})
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps({"checkpoint_run_id": checkpoint_run_id, "shadow_run_id": selected_shadow, **item}, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Short Label Pure No-Token Checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Shadow run id: {selected_shadow}",
        f"Status: {checkpoint_status}",
        f"Promotion status: {promotion_status}",
        f"Candidates: {len(items):,}",
        f"Allowed: {allowed_count:,}",
        f"Blocked: {blocked_count:,}",
        "- Production release allowed: 0",
        "",
        "Reason counts:",
        *[f"- {key}: {value:,}" for key, value in reason_counts.most_common()],
        "",
        "Interpretation:",
        "- This checkpoint covers issue-level short-label pure no-token nominal candidates.",
        "- It does not close segment-state and does not write output.",
        "- Segment closure still requires coverage composition and lifecycle integration.",
        "",
        f"CSV: {csv_path}",
        f"JSONL: {jsonl_path}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("[issue_short_label_pure_no_token_checkpoint] Checkpoint generated")
    print(f"[issue_short_label_pure_no_token_checkpoint] Checkpoint run id: {checkpoint_run_id}")
    print(f"[issue_short_label_pure_no_token_checkpoint] Shadow run id: {selected_shadow}")
    print(f"[issue_short_label_pure_no_token_checkpoint] Allowed: {allowed_count:,}")
    print(f"[issue_short_label_pure_no_token_checkpoint] Blocked: {blocked_count:,}")
    print(f"[issue_short_label_pure_no_token_checkpoint] Report: {txt_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "shadow_run_id": selected_shadow,
        "candidate_count": len(items),
        "allowed_count": allowed_count,
        "blocked_count": blocked_count,
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Checkpoint pure no-token short-label shadow policy.")
    parser.add_argument("--shadow-run-id", type=int, default=None)
    args = parser.parse_args()
    main(shadow_run_id=args.shadow_run_id)
