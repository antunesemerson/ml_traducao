from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_autofix_unknown_surface_checkpoint_v1"
AGENT_KEY = "micro_autofix_unknown_router"
ISSUE_FAMILY = "autofix_unknown_microagent"
SAFE_DECISIONS = {"safe_short_label", "safe_surface"}
ALLOWED_CLUSTERS = {
    "ui_warning_or_blocker_text",
    "list_value_or_counter_text",
    "confirmation_or_question_text",
    "ui_tooltip_or_markup_text",
}


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_autofix_unknown_surface_checkpoint_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            source_decision_run_id INTEGER,
            source_queue_run_id INTEGER,
            ledger_run_id INTEGER,
            selected_count INTEGER NOT NULL DEFAULT 0,
            allowed_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            cluster_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_autofix_unknown_surface_checkpoint_items (
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
            cluster TEXT,
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


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_autofix_unknown_surface_checkpoint"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl")


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
    if not row:
        raise RuntimeError("No matching ml_issue_review_decision_runs found.")
    return int(row["id"])


def load_decisions(conn, decision_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            decision.*,
            item.evidence_text AS queue_evidence_text,
            item.agent_key AS queue_agent_key,
            item.issue_family AS queue_issue_family
        FROM ml_issue_review_decisions decision
        LEFT JOIN ml_issue_review_queue_items item ON item.id = decision.queue_item_id
        WHERE decision.run_id = ?
        ORDER BY decision.segment_id
        """,
        (decision_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def classify(row: dict[str, Any]) -> tuple[str, str | None]:
    if int(row.get("valid") or 0) != 1:
        return "blocked", "invalid_decision"
    if row.get("queue_agent_key") != AGENT_KEY and row.get("agent_key") != AGENT_KEY:
        return "blocked", "wrong_agent"
    if row.get("queue_issue_family") != ISSUE_FAMILY and row.get("issue_family") != ISSUE_FAMILY:
        return "blocked", "wrong_issue_family"
    if str(row.get("queue_bucket") or "") not in ALLOWED_CLUSTERS:
        return "blocked", "cluster_not_allowed"
    if str(row.get("normalized_decision") or "") not in SAFE_DECISIONS:
        return "blocked", "decision_not_safe_surface"
    if str(row.get("evidence_label") or "") != "positive_evidence":
        return "blocked", "evidence_not_positive"
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
                "cluster": row.get("queue_bucket"),
                "normalized_decision": row.get("normalized_decision"),
                "checkpoint_status": status,
                "block_reason": block_reason,
                "evidence_label": row.get("evidence_label"),
                "reviewer": row.get("reviewer"),
                "notes": row.get("notes"),
                "evidence_text": row.get("queue_evidence_text"),
                "created_at": created_at,
            }
        )
    return items


def insert_items(conn, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    columns = list(items[0].keys())
    placeholders = ", ".join("?" for _ in columns)
    conn.executemany(
        f"""
        INSERT INTO ml_issue_autofix_unknown_surface_checkpoint_items (
            {", ".join(columns)}
        )
        VALUES ({placeholders})
        """,
        [tuple(item[column] for column in columns) for item in items],
    )


def write_outputs(txt_path: Path, jsonl_path: Path, run_id: int, items: list[dict[str, Any]], decision_run_id: int, queue_run_id: int | None) -> None:
    status_counts = Counter(item["checkpoint_status"] for item in items)
    cluster_counts = Counter(item["cluster"] for item in items if item["checkpoint_status"] == "allowed")
    block_counts = Counter(item["block_reason"] for item in items if item["block_reason"])

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Autofix unknown surface checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Checkpoint run id: {run_id}",
        f"Source decision run id: {decision_run_id}",
        f"Source queue run id: {queue_run_id if queue_run_id is not None else 'latest'}",
        "",
        "Summary:",
        f"- selected decisions: {len(items):,}",
        f"- allowed: {status_counts.get('allowed', 0):,}",
        f"- blocked: {status_counts.get('blocked', 0):,}",
        "- production_release_allowed: 0",
        "",
        "Allowed clusters:",
    ]
    if cluster_counts:
        lines.extend(f"- {cluster}: {count:,}" for cluster, count in cluster_counts.most_common())
    else:
        lines.append("- none")
    lines.extend(["", "Block reasons:"])
    if block_counts:
        lines.extend(f"- {reason}: {count:,}" for reason, count in block_counts.most_common())
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety:",
            "- Checkpoint only; no segment-state lifecycle closure.",
            "- No output writes.",
            "- Positive evidence must still pass a future bridge before it can reduce pending counts.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Checkpoint safe autofix_unknown surface evidence.")
    parser.add_argument("--decision-run-id", type=int)
    parser.add_argument("--queue-run-id", type=int, default=108)
    args = parser.parse_args()

    settings = db.load_settings()
    txt_path, jsonl_path = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        decision_run_id = args.decision_run_id or latest_decision_run_id(conn, args.queue_run_id)
        rows = load_decisions(conn, decision_run_id)
        now = db.utc_now()
        source_queue_run_id = int(rows[0]["queue_run_id"]) if rows else args.queue_run_id
        ledger_run_id = int(rows[0]["ledger_run_id"]) if rows and rows[0].get("ledger_run_id") else None
        run_id = conn.execute(
            """
            INSERT INTO ml_issue_autofix_unknown_surface_checkpoint_runs (
                rule_version,
                source_decision_run_id,
                source_queue_run_id,
                ledger_run_id,
                selected_count,
                allowed_count,
                blocked_count,
                production_release_allowed,
                cluster_counts_json,
                block_counts_json,
                report_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, 0, 0, 0, 0, '{}', '{}', ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                decision_run_id,
                source_queue_run_id,
                ledger_run_id,
                str(txt_path),
                str(jsonl_path),
                now,
                now,
                now,
            ),
        ).lastrowid
        run_id = int(run_id)
        items = build_items(rows, run_id)
        insert_items(conn, items)
        status_counts = Counter(item["checkpoint_status"] for item in items)
        cluster_counts = Counter(item["cluster"] for item in items if item["checkpoint_status"] == "allowed")
        block_counts = Counter(item["block_reason"] for item in items if item["block_reason"])
        conn.execute(
            """
            UPDATE ml_issue_autofix_unknown_surface_checkpoint_runs
            SET selected_count = ?,
                allowed_count = ?,
                blocked_count = ?,
                cluster_counts_json = ?,
                block_counts_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                len(items),
                status_counts.get("allowed", 0),
                status_counts.get("blocked", 0),
                json.dumps(dict(cluster_counts.most_common()), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(block_counts.most_common()), ensure_ascii=False, sort_keys=True),
                db.utc_now(),
                run_id,
            ),
        )
        conn.commit()

    write_outputs(txt_path, jsonl_path, run_id, items, decision_run_id, args.queue_run_id)
    print("[issue_autofix_unknown_surface_checkpoint] Checkpoint generated")
    print(f"[issue_autofix_unknown_surface_checkpoint] Run id: {run_id}")
    print(f"[issue_autofix_unknown_surface_checkpoint] Source decision run id: {decision_run_id}")
    print(f"[issue_autofix_unknown_surface_checkpoint] Decisions: {len(items):,}")
    print(f"[issue_autofix_unknown_surface_checkpoint] Allowed: {sum(1 for item in items if item['checkpoint_status'] == 'allowed'):,}")
    print(f"[issue_autofix_unknown_surface_checkpoint] Blocked: {sum(1 for item in items if item['checkpoint_status'] == 'blocked'):,}")
    print(f"[issue_autofix_unknown_surface_checkpoint] Report: {txt_path}")


if __name__ == "__main__":
    main()
