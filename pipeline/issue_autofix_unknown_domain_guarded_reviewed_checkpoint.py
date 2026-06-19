from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_autofix_unknown_domain_guarded_reviewed_checkpoint_v1"
AGENT_KEY = "micro_autofix_unknown_router"
ISSUE_FAMILY = "autofix_unknown_microagent"
ALLOWED_DECISION = "false_positive_reopen"
ALLOWED_EVIDENCE = "false_positive_reopen"


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_autofix_unknown_domain_guarded_reviewed_checkpoint_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            source_decision_run_id INTEGER NOT NULL,
            source_queue_run_id INTEGER,
            ledger_run_id INTEGER,
            selected_count INTEGER NOT NULL DEFAULT 0,
            allowed_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            status_counts_json TEXT,
            subpolicy_counts_json TEXT,
            block_counts_json TEXT,
            report_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_autofix_unknown_domain_guarded_reviewed_checkpoint_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            decision_id INTEGER,
            queue_run_id INTEGER,
            ledger_run_id INTEGER,
            ledger_item_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT,
            source_key TEXT,
            source_line_number INTEGER,
            subpolicy_name TEXT,
            normalized_decision TEXT,
            checkpoint_status TEXT NOT NULL,
            block_reason TEXT,
            evidence_label TEXT,
            reviewer TEXT,
            notes TEXT,
            evidence_text TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_autofix_unknown_domain_guarded_reviewed_checkpoint_items_segment
        ON ml_issue_autofix_unknown_domain_guarded_reviewed_checkpoint_items(segment_id, checkpoint_status)
        """
    )


def latest_decision_run_id(conn, queue_run_id: int | None) -> int:
    where = "WHERE queue_run_id = ?" if queue_run_id is not None else ""
    params = (queue_run_id,) if queue_run_id is not None else ()
    row = conn.execute(
        f"""
        SELECT id
        FROM ml_issue_review_decision_runs
        {where}
        ORDER BY id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None:
        raise RuntimeError("No matching ml_issue_review_decision_runs found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_autofix_unknown_domain_guarded_reviewed_checkpoint"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl")


def load_decisions(conn, decision_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            decision.*,
            queue.queue_bucket AS queue_subpolicy_name,
            queue.agent_key AS queue_agent_key,
            queue.issue_family AS queue_issue_family,
            queue.evidence_text AS queue_evidence_text
        FROM ml_issue_review_decisions decision
        LEFT JOIN ml_issue_review_queue_items queue ON queue.id = decision.queue_item_id
        WHERE decision.run_id = ?
        ORDER BY decision.segment_id
        """,
        (decision_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def classify(row: dict[str, Any]) -> tuple[str, str | None]:
    if int(row.get("valid") or 0) != 1 or row.get("validation_status") != "accepted":
        return "blocked", "invalid_or_unaccepted_decision"
    if row.get("queue_agent_key") != AGENT_KEY and row.get("agent_key") != AGENT_KEY:
        return "blocked", "wrong_agent"
    if row.get("queue_issue_family") != ISSUE_FAMILY and row.get("issue_family") != ISSUE_FAMILY:
        return "blocked", "wrong_issue_family"
    if row.get("normalized_decision") != ALLOWED_DECISION:
        return "blocked", "decision_not_false_positive_reopen"
    if row.get("evidence_label") != ALLOWED_EVIDENCE:
        return "blocked", "evidence_not_false_positive_reopen"
    return "allowed", None


def build_items(rows: list[dict[str, Any]], run_id: int) -> list[dict[str, Any]]:
    created_at = db.utc_now()
    items: list[dict[str, Any]] = []
    for row in rows:
        status, block_reason = classify(row)
        items.append(
            {
                "run_id": run_id,
                "decision_id": row.get("id"),
                "queue_run_id": row.get("queue_run_id"),
                "ledger_run_id": row.get("ledger_run_id"),
                "ledger_item_id": row.get("ledger_item_id"),
                "segment_id": row.get("segment_id"),
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "source_line_number": row.get("source_line_number"),
                "subpolicy_name": row.get("queue_subpolicy_name") or row.get("queue_bucket"),
                "normalized_decision": row.get("normalized_decision"),
                "checkpoint_status": status,
                "block_reason": block_reason,
                "evidence_label": row.get("evidence_label"),
                "reviewer": row.get("reviewer"),
                "notes": row.get("notes"),
                "evidence_text": row.get("queue_evidence_text") or row.get("evidence_text"),
                "created_at": created_at,
            }
        )
    return items


def insert_rows(conn, table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    columns = list(rows[0].keys())
    placeholders = ", ".join("?" for _ in columns)
    conn.executemany(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
        [tuple(row[column] for column in columns) for row in rows],
    )


def write_outputs(txt_path: Path, jsonl_path: Path, *, run_id: int, decision_run_id: int, items: list[dict[str, Any]]) -> None:
    status_counts = Counter(item["checkpoint_status"] for item in items)
    subpolicy_counts = Counter(item["subpolicy_name"] for item in items if item["checkpoint_status"] == "allowed")
    block_counts = Counter(item["block_reason"] for item in items if item["block_reason"])

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Autofix unknown domain guarded reviewed checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Checkpoint run id: {run_id}",
        f"Decision run id: {decision_run_id}",
        "",
        "Summary:",
        f"- decisions: {len(items):,}",
        f"- allowed: {status_counts.get('allowed', 0):,}",
        f"- blocked: {status_counts.get('blocked', 0):,}",
        "- production_release_allowed: 0",
        "",
        "Allowed subpolicies:",
    ]
    lines.extend(f"- {name}: {count:,}" for name, count in subpolicy_counts.most_common()) if subpolicy_counts else lines.append("- none")
    lines.extend(["", "Block reasons:"])
    lines.extend(f"- {reason}: {count:,}" for reason, count in block_counts.most_common()) if block_counts else lines.append("- none")
    lines.extend(
        [
            "",
            "Safety:",
            "- Checkpoint only; no segment-state lifecycle closure.",
            "- No output writes.",
            "- Only accepted false_positive_reopen decisions can be allowed.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, decision_run_id: int | None, queue_run_id: int | None) -> dict[str, Any]:
    settings = db.load_settings()
    txt_path, jsonl_path = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_decision_run_id = decision_run_id or latest_decision_run_id(conn, queue_run_id)
        decisions = load_decisions(conn, selected_decision_run_id)
        source_queue_run_id = int(decisions[0]["queue_run_id"]) if decisions and decisions[0].get("queue_run_id") else queue_run_id
        ledger_run_id = int(decisions[0]["ledger_run_id"]) if decisions and decisions[0].get("ledger_run_id") else None
        now = db.utc_now()
        run_id = int(
            conn.execute(
                """
                INSERT INTO ml_issue_autofix_unknown_domain_guarded_reviewed_checkpoint_runs (
                    rule_version,
                    source_decision_run_id,
                    source_queue_run_id,
                    ledger_run_id,
                    selected_count,
                    allowed_count,
                    blocked_count,
                    production_release_allowed,
                    status_counts_json,
                    subpolicy_counts_json,
                    block_counts_json,
                    report_path,
                    jsonl_path,
                    started_at,
                    finished_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, 0, 0, 0, 0, '{}', '{}', '{}', ?, ?, ?, ?, ?)
                """,
                (
                    RULE_VERSION,
                    selected_decision_run_id,
                    source_queue_run_id,
                    ledger_run_id,
                    str(txt_path),
                    str(jsonl_path),
                    now,
                    now,
                    now,
                ),
            ).lastrowid
        )
        items = build_items(decisions, run_id)
        insert_rows(conn, "ml_issue_autofix_unknown_domain_guarded_reviewed_checkpoint_items", items)
        status_counts = Counter(item["checkpoint_status"] for item in items)
        subpolicy_counts = Counter(item["subpolicy_name"] for item in items if item["checkpoint_status"] == "allowed")
        block_counts = Counter(item["block_reason"] for item in items if item["block_reason"])
        conn.execute(
            """
            UPDATE ml_issue_autofix_unknown_domain_guarded_reviewed_checkpoint_runs
            SET selected_count = ?,
                allowed_count = ?,
                blocked_count = ?,
                status_counts_json = ?,
                subpolicy_counts_json = ?,
                block_counts_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                len(items),
                status_counts.get("allowed", 0),
                status_counts.get("blocked", 0),
                json.dumps(dict(status_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(subpolicy_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(block_counts), ensure_ascii=False, sort_keys=True),
                db.utc_now(),
                run_id,
            ),
        )
        write_outputs(txt_path, jsonl_path, run_id=run_id, decision_run_id=selected_decision_run_id, items=items)
        conn.commit()

    print("[issue_autofix_unknown_domain_guarded_reviewed_checkpoint] Checkpoint generated")
    print(f"[issue_autofix_unknown_domain_guarded_reviewed_checkpoint] Checkpoint run id: {run_id}")
    print(f"[issue_autofix_unknown_domain_guarded_reviewed_checkpoint] Decision run id: {selected_decision_run_id}")
    print(f"[issue_autofix_unknown_domain_guarded_reviewed_checkpoint] Allowed: {status_counts.get('allowed', 0):,}")
    print(f"[issue_autofix_unknown_domain_guarded_reviewed_checkpoint] Blocked: {status_counts.get('blocked', 0):,}")
    print(f"[issue_autofix_unknown_domain_guarded_reviewed_checkpoint] Report: {txt_path}")
    return {
        "run_id": run_id,
        "decision_run_id": selected_decision_run_id,
        "allowed": status_counts.get("allowed", 0),
        "blocked": status_counts.get("blocked", 0),
        "report_path": str(txt_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create reviewed checkpoint for autofix_unknown domain guarded evidence.")
    parser.add_argument("--decision-run-id", type=int)
    parser.add_argument("--queue-run-id", type=int, default=202)
    args = parser.parse_args()
    main(decision_run_id=args.decision_run_id, queue_run_id=args.queue_run_id)
