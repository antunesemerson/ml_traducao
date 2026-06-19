from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "issue_full_coverage_trust_checkpoint_v1"
CHECKPOINT_NAME = "full_coverage_trust_audit_checkpoint_v1"
CHECKPOINT_ACTION = "cover_full_coverage_trust_safe_pair"
POLICY_NAME = "full_coverage_trust_shadow"
AGENT_KEY = "coverage_trust_auditor"
QUEUE_STRATEGY = "full_coverage_false_ready_audit"
PAIR_FAMILIES = ("semantic_review_router", "short_label_style_microagent")
SAFE_DECISION = "safe_short_label"


def report_paths(settings: dict[str, Any], queue_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_full_coverage_trust_checkpoint_queue_{queue_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_full_coverage_trust_checkpoint_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            queue_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            decision_count INTEGER NOT NULL DEFAULT 0,
            safe_decision_count INTEGER NOT NULL DEFAULT 0,
            allowed_segment_count INTEGER NOT NULL DEFAULT 0,
            allowed_issue_item_count INTEGER NOT NULL DEFAULT 0,
            blocked_decision_count INTEGER NOT NULL DEFAULT 0,
            decision_counts_json TEXT,
            blocker_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_full_coverage_trust_checkpoint_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checkpoint_run_id INTEGER NOT NULL,
            queue_run_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            queue_bucket TEXT NOT NULL,
            issue_family TEXT NOT NULL,
            issue_kind TEXT NOT NULL,
            normalized_decision TEXT NOT NULL,
            evidence_label TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_full_coverage_trust_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_full_coverage_trust_checkpoint_items_ledger
        ON ml_issue_full_coverage_trust_checkpoint_items(ledger_item_id, checkpoint_allowed)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_full_coverage_trust_checkpoint_items_segment
        ON ml_issue_full_coverage_trust_checkpoint_items(segment_id, issue_family)
        """
    )


def latest_queue_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_review_queue_runs
        WHERE finished_at IS NOT NULL
          AND agent_key = ?
          AND queue_strategy = ?
          AND reviewed_count > 0
        ORDER BY id DESC
        LIMIT 1
        """,
        (AGENT_KEY, QUEUE_STRATEGY),
    ).fetchone()
    if row is None:
        raise RuntimeError("No reviewed full coverage trust queue found.")
    return int(row["id"])


def fetch_queue_run(conn, queue_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM ml_issue_review_queue_runs WHERE id = ?",
        (queue_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Queue run not found: {queue_run_id}")
    return dict(row)


def fetch_decisions(conn, queue_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            decision.*,
            item.evidence_text,
            item.evidence_json
        FROM ml_issue_review_decisions decision
        JOIN ml_issue_review_queue_items item ON item.id = decision.queue_item_id
        WHERE decision.queue_run_id = ?
          AND decision.valid = 1
          AND decision.validation_status = 'accepted'
        ORDER BY decision.segment_id, decision.queue_item_id
        """,
        (queue_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_segment_ledger_items(conn, ledger_run_id: int, segment_ids: set[int]) -> dict[int, list[dict[str, Any]]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in sorted(segment_ids))
    rows = conn.execute(
        f"""
        SELECT *
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        ORDER BY segment_id, issue_family, issue_kind, id
        """,
        (ledger_run_id, *sorted(segment_ids)),
    ).fetchall()
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["segment_id"])].append(dict(row))
    return grouped


def decision_block_reason(decision: dict[str, Any], ledger_items: list[dict[str, Any]], queue_run: dict[str, Any]) -> str:
    if queue_run.get("agent_key") != AGENT_KEY:
        return "wrong_queue_agent"
    if queue_run.get("queue_strategy") != QUEUE_STRATEGY:
        return "wrong_queue_strategy"
    normalized = str(decision.get("normalized_decision") or "")
    if normalized != SAFE_DECISION:
        return f"decision_not_safe:{normalized or 'empty'}"
    if str(decision.get("evidence_label") or "") != "positive_evidence":
        return "evidence_not_positive"
    if str(decision.get("corrected_text") or "").strip():
        return "safe_decision_has_corrected_text"
    families = tuple(str(item["issue_family"]) for item in ledger_items)
    if families != PAIR_FAMILIES:
        return "ledger_pair_mismatch:" + "|".join(families)
    if len(ledger_items) != 2:
        return "ledger_pair_item_count_mismatch"
    return ""


def build_checkpoint_rows(
    *,
    decisions: list[dict[str, Any]],
    ledger_by_segment: dict[int, list[dict[str, Any]]],
    queue_run: dict[str, Any],
) -> tuple[list[dict[str, Any]], Counter[str]]:
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for decision in decisions:
        segment_id = int(decision["segment_id"])
        ledger_items = ledger_by_segment.get(segment_id, [])
        block_reason = decision_block_reason(decision, ledger_items, queue_run)
        bucket = str(decision.get("queue_bucket") or "unknown_profile")
        normalized = str(decision.get("normalized_decision") or "unknown_decision")
        counts[f"decision:{normalized}"] += 1
        counts[f"profile:{bucket}"] += 1
        if block_reason:
            counts["blocked_decision"] += 1
            counts[f"block:{block_reason}"] += 1
            rows.append(
                {
                    "queue_run_id": int(decision["queue_run_id"]),
                    "queue_item_id": int(decision["queue_item_id"]),
                    "ledger_run_id": int(decision["ledger_run_id"]),
                    "ledger_item_id": int(decision.get("ledger_item_id") or 0),
                    "segment_id": segment_id,
                    "relative_path": decision["relative_path"],
                    "source_key": decision["source_key"],
                    "source_line_number": decision.get("source_line_number"),
                    "queue_bucket": bucket,
                    "issue_family": str(decision.get("issue_family") or "unknown_family"),
                    "issue_kind": str(decision.get("issue_kind") or "unknown_issue"),
                    "normalized_decision": normalized,
                    "evidence_label": str(decision.get("evidence_label") or ""),
                    "agent_key": AGENT_KEY,
                    "subpolicy_name": bucket,
                    "checkpoint_action": CHECKPOINT_ACTION,
                    "checkpoint_allowed": 0,
                    "block_reason": block_reason,
                }
            )
            continue

        counts["safe_decision"] += 1
        counts["allowed_segment"] += 1
        for ledger_item in ledger_items:
            counts["allowed_issue_item"] += 1
            rows.append(
                {
                    "queue_run_id": int(decision["queue_run_id"]),
                    "queue_item_id": int(decision["queue_item_id"]),
                    "ledger_run_id": int(decision["ledger_run_id"]),
                    "ledger_item_id": int(ledger_item["id"]),
                    "segment_id": segment_id,
                    "relative_path": decision["relative_path"],
                    "source_key": decision["source_key"],
                    "source_line_number": decision.get("source_line_number"),
                    "queue_bucket": bucket,
                    "issue_family": str(ledger_item["issue_family"]),
                    "issue_kind": str(ledger_item["issue_kind"] or "unknown_issue"),
                    "normalized_decision": normalized,
                    "evidence_label": str(decision.get("evidence_label") or ""),
                    "agent_key": AGENT_KEY,
                    "subpolicy_name": bucket,
                    "checkpoint_action": CHECKPOINT_ACTION,
                    "checkpoint_allowed": 1,
                    "block_reason": "",
                }
            )
    return rows, counts


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    queue_run: dict[str, Any],
    rows: list[dict[str, Any]],
    counts: Counter[str],
    started_at: datetime,
) -> None:
    allowed_rows = [row for row in rows if int(row["checkpoint_allowed"]) == 1]
    blocked_rows = [row for row in rows if int(row["checkpoint_allowed"]) == 0]
    allowed_segments = len({int(row["segment_id"]) for row in allowed_rows})
    lines = [
        "Full Coverage Trust Checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint name: {CHECKPOINT_NAME}",
        f"Checkpoint run id: {checkpoint_run_id}",
        "Production release allowed: 0",
        f"Queue run id: {queue_run['id']}",
        f"Ledger run id: {queue_run['ledger_run_id']}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Reviewed decisions: {counts['decision:safe_short_label'] + counts['decision:needs_repair'] + counts['decision:needs_domain_context']}",
        f"- Safe decisions: {counts['safe_decision']}",
        f"- Allowed segments: {allowed_segments}",
        f"- Allowed issue items: {len(allowed_rows)}",
        f"- Blocked decisions: {counts['blocked_decision']}",
        "",
        "Decision counts:",
    ]
    for key, value in sorted(counts.items()):
        if key.startswith("decision:"):
            lines.append(f"- {key.removeprefix('decision:')}: {value}")
    lines.extend(["", "Blocks:"])
    for key, value in sorted(counts.items()):
        if key.startswith("block:"):
            lines.append(f"- {key.removeprefix('block:')}: {value}")
    lines.extend(["", "Allowed samples:"])
    for row in allowed_rows[:30]:
        lines.append(
            f"- {row['issue_family']} | segment={row['segment_id']} "
            f"{row['relative_path']}::{row['source_key']}"
        )
    lines.extend(["", "Blocked samples:"])
    for row in blocked_rows[:30]:
        lines.append(
            f"- {row['block_reason']} | {row['normalized_decision']} | segment={row['segment_id']} "
            f"{row['relative_path']}::{row['source_key']}"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Checkpoint only: no source/output writes, no confirmations and no lifecycle/production closure.",
            "- This evidence is coverage-grade only for safe short-label semantic pairs from the trust audit queue.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    fieldnames = [
        "checkpoint_item_id",
        "checkpoint_run_id",
        "queue_run_id",
        "queue_item_id",
        "ledger_run_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "issue_family",
        "issue_kind",
        "normalized_decision",
        "evidence_label",
        "agent_key",
        "subpolicy_name",
        "checkpoint_action",
        "checkpoint_allowed",
        "block_reason",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps({key: row.get(key) for key in fieldnames}, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Checkpoint safe evidence from full-coverage trust audit queues.")
    parser.add_argument("--queue-run-id", type=int, default=None)
    args = parser.parse_args()

    settings = db.load_settings()
    started_at = datetime.now()
    started_at_db = started_at.isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        queue_run_id = args.queue_run_id or latest_queue_run_id(conn)
        queue_run = fetch_queue_run(conn, queue_run_id)
        decisions = fetch_decisions(conn, queue_run_id)
        if not decisions:
            raise RuntimeError(f"No accepted decisions found for queue {queue_run_id}")
        ledger_run_id = int(queue_run["ledger_run_id"])
        ledger_by_segment = fetch_segment_ledger_items(
            conn,
            ledger_run_id,
            {int(decision["segment_id"]) for decision in decisions},
        )
        rows, counts = build_checkpoint_rows(
            decisions=decisions,
            ledger_by_segment=ledger_by_segment,
            queue_run=queue_run,
        )
        checkpoint_status = "checkpoint_ready" if counts["safe_decision"] else "checkpoint_blocked"
        cur = conn.execute(
            """
            INSERT INTO ml_issue_full_coverage_trust_checkpoint_runs (
                rule_version,
                checkpoint_name,
                checkpoint_status,
                policy_name,
                policy_status,
                agent_key,
                queue_run_id,
                ledger_run_id,
                decision_count,
                safe_decision_count,
                allowed_segment_count,
                allowed_issue_item_count,
                blocked_decision_count,
                decision_counts_json,
                blocker_counts_json,
                started_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, 'shadow', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                CHECKPOINT_NAME,
                checkpoint_status,
                POLICY_NAME,
                AGENT_KEY,
                queue_run_id,
                ledger_run_id,
                len(decisions),
                counts["safe_decision"],
                counts["allowed_segment"],
                counts["allowed_issue_item"],
                counts["blocked_decision"],
                json.dumps({k: v for k, v in counts.items() if k.startswith("decision:")}, ensure_ascii=False, sort_keys=True),
                json.dumps({k: v for k, v in counts.items() if k.startswith("block:")}, ensure_ascii=False, sort_keys=True),
                started_at_db,
                started_at_db,
            ),
        )
        checkpoint_run_id = int(cur.lastrowid)
        now = db.utc_now()
        for row in rows:
            item_cur = conn.execute(
                """
                INSERT INTO ml_issue_full_coverage_trust_checkpoint_items (
                    checkpoint_run_id,
                    queue_run_id,
                    queue_item_id,
                    ledger_run_id,
                    ledger_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    queue_bucket,
                    issue_family,
                    issue_kind,
                    normalized_decision,
                    evidence_label,
                    agent_key,
                    subpolicy_name,
                    checkpoint_action,
                    checkpoint_allowed,
                    block_reason,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_run_id,
                    row["queue_run_id"],
                    row["queue_item_id"],
                    row["ledger_run_id"],
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    row["queue_bucket"],
                    row["issue_family"],
                    row["issue_kind"],
                    row["normalized_decision"],
                    row["evidence_label"],
                    row["agent_key"],
                    row["subpolicy_name"],
                    row["checkpoint_action"],
                    row["checkpoint_allowed"],
                    row["block_reason"],
                    now,
                ),
            )
            row["checkpoint_item_id"] = int(item_cur.lastrowid)
            row["checkpoint_run_id"] = checkpoint_run_id

        txt_path, csv_path, jsonl_path = report_paths(settings, queue_run_id)
        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            checkpoint_run_id=checkpoint_run_id,
            queue_run=queue_run,
            rows=rows,
            counts=counts,
            started_at=started_at,
        )
        finished_at = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            UPDATE ml_issue_full_coverage_trust_checkpoint_runs
            SET report_path = ?,
                csv_path = ?,
                jsonl_path = ?,
                finished_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (str(txt_path), str(csv_path), str(jsonl_path), finished_at, finished_at, checkpoint_run_id),
        )
        conn.commit()

    print("[issue_full_coverage_trust_checkpoint] Checkpoint generated")
    print(f"[issue_full_coverage_trust_checkpoint] Checkpoint run id: {checkpoint_run_id}")
    print(f"[issue_full_coverage_trust_checkpoint] Queue run id: {queue_run_id}")
    print(f"[issue_full_coverage_trust_checkpoint] Decisions: {len(decisions)}")
    print(f"[issue_full_coverage_trust_checkpoint] Safe decisions: {counts['safe_decision']}")
    print(f"[issue_full_coverage_trust_checkpoint] Allowed issue items: {counts['allowed_issue_item']}")
    print(f"[issue_full_coverage_trust_checkpoint] Blocked decisions: {counts['blocked_decision']}")
    print(f"[issue_full_coverage_trust_checkpoint] Report: {txt_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "queue_run_id": queue_run_id,
        "safe_decisions": counts["safe_decision"],
        "allowed_issue_items": counts["allowed_issue_item"],
        "blocked_decisions": counts["blocked_decision"],
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    main()
