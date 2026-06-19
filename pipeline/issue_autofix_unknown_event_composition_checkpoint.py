from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_autofix_unknown_event_composition_checkpoint_v1"
AGENT_KEY = "micro_autofix_unknown_router"
ISSUE_FAMILY = "autofix_unknown_microagent"
QUEUE_BUCKET = "event_sentence_or_description"
POSITIVE_DECISION = "composition_ready"


def latest_finished_run_id(conn, table: str) -> int:
    row = conn.execute(
        f"""
        SELECT id
        FROM {table}
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise RuntimeError(f"No finished run found in {table}.")
    return int(row["id"])


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


def report_paths(settings: dict[str, Any], stem: str) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_{stem}"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_autofix_unknown_event_composition_checkpoint_runs (
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
        CREATE TABLE IF NOT EXISTS ml_issue_autofix_unknown_event_composition_checkpoint_items (
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
        CREATE TABLE IF NOT EXISTS ml_issue_autofix_unknown_event_composition_bridge_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            source_checkpoint_run_id INTEGER NOT NULL,
            source_segment_state_run_id INTEGER NOT NULL,
            source_ledger_run_id INTEGER NOT NULL,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            ready_count INTEGER NOT NULL DEFAULT 0,
            partial_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            estimated_closed_gain INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            status_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_autofix_unknown_event_composition_bridge_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            checkpoint_item_id INTEGER NOT NULL,
            checkpoint_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT,
            source_key TEXT,
            bridge_status TEXT NOT NULL,
            block_reason TEXT,
            segment_open_issue_count INTEGER NOT NULL DEFAULT 0,
            covered_issue_count INTEGER NOT NULL DEFAULT 0,
            final_state TEXT,
            evidence_text TEXT,
            created_at TEXT NOT NULL
        )
        """
    )


def load_decisions(conn, decision_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            decision.*,
            queue.queue_bucket,
            queue.agent_key AS queue_agent_key,
            queue.issue_family AS queue_issue_family,
            queue.evidence_text AS queue_evidence_text
        FROM ml_issue_review_decisions decision
        JOIN ml_issue_review_queue_items queue ON queue.id = decision.queue_item_id
        WHERE decision.run_id = ?
        ORDER BY decision.segment_id
        """,
        (decision_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def classify_checkpoint(row: dict[str, Any]) -> tuple[str, str | None]:
    if int(row.get("valid") or 0) != 1 or row.get("validation_status") != "accepted":
        return "blocked", "invalid_or_unaccepted_decision"
    if row.get("queue_agent_key") != AGENT_KEY and row.get("agent_key") != AGENT_KEY:
        return "blocked", "wrong_agent"
    if row.get("queue_issue_family") != ISSUE_FAMILY and row.get("issue_family") != ISSUE_FAMILY:
        return "blocked", "wrong_issue_family"
    if row.get("queue_bucket") != QUEUE_BUCKET:
        return "blocked", "wrong_queue_bucket"
    if row.get("normalized_decision") != POSITIVE_DECISION:
        return "blocked", "decision_not_composition_ready"
    if row.get("evidence_label") != POSITIVE_DECISION:
        return "blocked", "evidence_not_composition_ready"
    return "allowed", None


def build_checkpoint_items(rows: list[dict[str, Any]], run_id: int) -> list[dict[str, Any]]:
    created_at = db.utc_now()
    items: list[dict[str, Any]] = []
    for row in rows:
        status, block_reason = classify_checkpoint(row)
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


def insert_rows(conn, table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    columns = list(rows[0].keys())
    placeholders = ", ".join("?" for _ in columns)
    conn.executemany(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
        [tuple(row[column] for column in columns) for row in rows],
    )


def write_checkpoint_report(path: Path, jsonl_path: Path, run_id: int, decision_run_id: int, items: list[dict[str, Any]]) -> None:
    status_counts = Counter(item["checkpoint_status"] for item in items)
    block_counts = Counter(item["block_reason"] for item in items if item["block_reason"])
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    lines = [
        "Autofix unknown event composition checkpoint",
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
        "Block reasons:",
    ]
    lines.extend(f"- {reason}: {count:,}" for reason, count in block_counts.most_common()) if block_counts else lines.append("- none")
    lines.extend(
        [
            "",
            "Safety:",
            "- Checkpoint only; no segment-state lifecycle closure.",
            "- No source/output writes.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def fetch_bridge_candidates(conn, checkpoint_run_id: int, state_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH issue_counts AS (
            SELECT
                segment_id,
                COUNT(*) AS open_issue_count,
                SUM(CASE WHEN issue_family = ? THEN 1 ELSE 0 END) AS autofix_unknown_count
            FROM ml_issue_ledger_items
            WHERE run_id = ?
              AND status = 'open'
            GROUP BY segment_id
        )
        SELECT
            checkpoint.id AS checkpoint_item_id,
            checkpoint.run_id AS checkpoint_run_id,
            checkpoint.ledger_run_id,
            checkpoint.segment_id,
            checkpoint.relative_path,
            checkpoint.source_key,
            checkpoint.evidence_text,
            state.final_state,
            state.state_group,
            COALESCE(issue_counts.open_issue_count, 0) AS segment_open_issue_count,
            COALESCE(issue_counts.autofix_unknown_count, 0) AS autofix_unknown_count
        FROM ml_issue_autofix_unknown_event_composition_checkpoint_items checkpoint
        LEFT JOIN segment_state_items state
            ON state.run_id = ?
           AND state.segment_id = checkpoint.segment_id
        LEFT JOIN issue_counts ON issue_counts.segment_id = checkpoint.segment_id
        WHERE checkpoint.run_id = ?
          AND checkpoint.checkpoint_status = 'allowed'
        ORDER BY checkpoint.segment_id
        """,
        (ISSUE_FAMILY, ledger_run_id, state_run_id, checkpoint_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def classify_bridge(row: dict[str, Any]) -> tuple[str, str | None, int]:
    if row.get("state_group") != "pending":
        return "blocked", "not_pending_or_already_closed", 0
    if int(row.get("segment_open_issue_count") or 0) == 1 and int(row.get("autofix_unknown_count") or 0) == 1:
        return "ready_for_lifecycle_bridge", None, 1
    if int(row.get("autofix_unknown_count") or 0) >= 1:
        return "partial_coverage", "other_open_issues_remain", 1
    return "blocked", "checkpoint_issue_not_in_current_ledger", 0


def build_bridge_items(rows: list[dict[str, Any]], run_id: int, state_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    created_at = db.utc_now()
    items: list[dict[str, Any]] = []
    for row in rows:
        status, block_reason, covered = classify_bridge(row)
        items.append(
            {
                "run_id": run_id,
                "checkpoint_item_id": row["checkpoint_item_id"],
                "checkpoint_run_id": row["checkpoint_run_id"],
                "ledger_run_id": ledger_run_id,
                "segment_state_run_id": state_run_id,
                "segment_id": row["segment_id"],
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "bridge_status": status,
                "block_reason": block_reason,
                "segment_open_issue_count": int(row.get("segment_open_issue_count") or 0),
                "covered_issue_count": covered,
                "final_state": row.get("final_state"),
                "evidence_text": row.get("evidence_text"),
                "created_at": created_at,
            }
        )
    return items


def write_bridge_report(path: Path, jsonl_path: Path, run_id: int, checkpoint_run_id: int, state_run_id: int, ledger_run_id: int, items: list[dict[str, Any]]) -> None:
    status_counts = Counter(item["bridge_status"] for item in items)
    block_counts = Counter(item["block_reason"] for item in items if item["block_reason"])
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    lines = [
        "Autofix unknown event composition bridge proposal",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Bridge run id: {run_id}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Segment-state run id: {state_run_id}",
        f"Ledger run id: {ledger_run_id}",
        "",
        "Summary:",
        f"- candidates: {len(items):,}",
        f"- ready_for_lifecycle_bridge: {status_counts.get('ready_for_lifecycle_bridge', 0):,}",
        f"- partial_coverage: {status_counts.get('partial_coverage', 0):,}",
        f"- blocked: {status_counts.get('blocked', 0):,}",
        f"- estimated_closed_gain: {status_counts.get('ready_for_lifecycle_bridge', 0):,}",
        "- production_release_allowed: 0",
        "",
        "Block reasons:",
    ]
    lines.extend(f"- {reason}: {count:,}" for reason, count in block_counts.most_common()) if block_counts else lines.append("- none")
    lines.extend(
        [
            "",
            "Ready sample:",
        ]
    )
    ready = [item for item in items if item["bridge_status"] == "ready_for_lifecycle_bridge"]
    if ready:
        for item in ready[:20]:
            lines.append(f"- segment={item['segment_id']} | {item['relative_path']}::{item['source_key']} | {item['evidence_text']}")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety:",
            "- Proposal only.",
            "- Does not modify segment_state.",
            "- Does not write output.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Checkpoint and bridge-propose autofix_unknown event composition evidence.")
    parser.add_argument("--decision-run-id", type=int)
    parser.add_argument("--queue-run-id", type=int)
    parser.add_argument("--segment-state-run-id", type=int)
    parser.add_argument("--ledger-run-id", type=int)
    args = parser.parse_args()

    settings = db.load_settings()
    checkpoint_txt, checkpoint_jsonl = report_paths(settings, "issue_autofix_unknown_event_composition_checkpoint")
    bridge_txt, bridge_jsonl = report_paths(settings, "issue_autofix_unknown_event_composition_bridge_proposal")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        decision_run_id = args.decision_run_id or latest_decision_run_id(conn, args.queue_run_id)
        decisions = load_decisions(conn, decision_run_id)
        source_queue_run_id = int(decisions[0]["queue_run_id"]) if decisions else args.queue_run_id
        ledger_run_id = args.ledger_run_id or (int(decisions[0]["ledger_run_id"]) if decisions and decisions[0].get("ledger_run_id") else latest_finished_run_id(conn, "ml_issue_ledger_runs"))
        now = db.utc_now()

        checkpoint_run_id = int(
            conn.execute(
                """
                INSERT INTO ml_issue_autofix_unknown_event_composition_checkpoint_runs (
                    rule_version, source_decision_run_id, source_queue_run_id, ledger_run_id,
                    selected_count, allowed_count, blocked_count, production_release_allowed,
                    status_counts_json, block_counts_json, report_path, jsonl_path,
                    started_at, finished_at, updated_at
                )
                VALUES (?, ?, ?, ?, 0, 0, 0, 0, '{}', '{}', ?, ?, ?, ?, ?)
                """,
                (
                    RULE_VERSION,
                    decision_run_id,
                    source_queue_run_id,
                    ledger_run_id,
                    str(checkpoint_txt),
                    str(checkpoint_jsonl),
                    now,
                    now,
                    now,
                ),
            ).lastrowid
        )
        checkpoint_items = build_checkpoint_items(decisions, checkpoint_run_id)
        insert_rows(conn, "ml_issue_autofix_unknown_event_composition_checkpoint_items", checkpoint_items)
        checkpoint_status = Counter(item["checkpoint_status"] for item in checkpoint_items)
        checkpoint_blocks = Counter(item["block_reason"] for item in checkpoint_items if item["block_reason"])
        conn.execute(
            """
            UPDATE ml_issue_autofix_unknown_event_composition_checkpoint_runs
            SET selected_count = ?, allowed_count = ?, blocked_count = ?,
                status_counts_json = ?, block_counts_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                len(checkpoint_items),
                checkpoint_status.get("allowed", 0),
                checkpoint_status.get("blocked", 0),
                json.dumps(dict(checkpoint_status), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(checkpoint_blocks), ensure_ascii=False, sort_keys=True),
                db.utc_now(),
                checkpoint_run_id,
            ),
        )

        state_run_id = args.segment_state_run_id or latest_finished_run_id(conn, "segment_state_runs")
        bridge_rows = fetch_bridge_candidates(conn, checkpoint_run_id, state_run_id, ledger_run_id)
        bridge_run_id = int(
            conn.execute(
                """
                INSERT INTO ml_issue_autofix_unknown_event_composition_bridge_runs (
                    rule_version, source_checkpoint_run_id, source_segment_state_run_id,
                    source_ledger_run_id, total_candidates, ready_count, partial_count,
                    blocked_count, estimated_closed_gain, production_release_allowed,
                    status_counts_json, block_counts_json, report_path, jsonl_path,
                    started_at, finished_at, updated_at
                )
                VALUES (?, ?, ?, ?, 0, 0, 0, 0, 0, 0, '{}', '{}', ?, ?, ?, ?, ?)
                """,
                (
                    RULE_VERSION,
                    checkpoint_run_id,
                    state_run_id,
                    ledger_run_id,
                    str(bridge_txt),
                    str(bridge_jsonl),
                    now,
                    now,
                    now,
                ),
            ).lastrowid
        )
        bridge_items = build_bridge_items(bridge_rows, bridge_run_id, state_run_id, ledger_run_id)
        insert_rows(conn, "ml_issue_autofix_unknown_event_composition_bridge_items", bridge_items)
        bridge_status = Counter(item["bridge_status"] for item in bridge_items)
        bridge_blocks = Counter(item["block_reason"] for item in bridge_items if item["block_reason"])
        conn.execute(
            """
            UPDATE ml_issue_autofix_unknown_event_composition_bridge_runs
            SET total_candidates = ?, ready_count = ?, partial_count = ?, blocked_count = ?,
                estimated_closed_gain = ?, status_counts_json = ?, block_counts_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                len(bridge_items),
                bridge_status.get("ready_for_lifecycle_bridge", 0),
                bridge_status.get("partial_coverage", 0),
                bridge_status.get("blocked", 0),
                bridge_status.get("ready_for_lifecycle_bridge", 0),
                json.dumps(dict(bridge_status), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(bridge_blocks), ensure_ascii=False, sort_keys=True),
                db.utc_now(),
                bridge_run_id,
            ),
        )
        conn.commit()

    write_checkpoint_report(checkpoint_txt, checkpoint_jsonl, checkpoint_run_id, decision_run_id, checkpoint_items)
    write_bridge_report(bridge_txt, bridge_jsonl, bridge_run_id, checkpoint_run_id, state_run_id, ledger_run_id, bridge_items)

    print("[issue_autofix_unknown_event_composition_checkpoint] Checkpoint and proposal generated")
    print(f"[issue_autofix_unknown_event_composition_checkpoint] Decision run id: {decision_run_id}")
    print(f"[issue_autofix_unknown_event_composition_checkpoint] Checkpoint run id: {checkpoint_run_id}")
    print(f"[issue_autofix_unknown_event_composition_checkpoint] Allowed: {checkpoint_status.get('allowed', 0):,}")
    print(f"[issue_autofix_unknown_event_composition_checkpoint] Blocked: {checkpoint_status.get('blocked', 0):,}")
    print(f"[issue_autofix_unknown_event_composition_checkpoint] Bridge run id: {bridge_run_id}")
    print(f"[issue_autofix_unknown_event_composition_checkpoint] Ready: {bridge_status.get('ready_for_lifecycle_bridge', 0):,}")
    print(f"[issue_autofix_unknown_event_composition_checkpoint] Partial: {bridge_status.get('partial_coverage', 0):,}")
    print(f"[issue_autofix_unknown_event_composition_checkpoint] Bridge report: {bridge_txt}")


if __name__ == "__main__":
    main()
