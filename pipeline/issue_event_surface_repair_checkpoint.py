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
from local_quality_validator import validate_text


RULE_VERSION = "issue_event_surface_repair_checkpoint_v1"
POLICY_NAME = "event_surface_repair_shadow_v1"
POLICY_STATUS = "shadow"
CHECKPOINT_ACTION = "stage_event_surface_repair_shadow"
SUBPOLICY_NAME = "event_surface_corrected_text_repair"


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


def latest_decision_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT run.id
        FROM ml_issue_review_decision_runs run
        JOIN ml_issue_review_decisions decision ON decision.run_id = run.id
        WHERE run.finished_at IS NOT NULL
          AND decision.normalized_decision = 'needs_repair'
          AND TRIM(COALESCE(decision.corrected_text, '')) <> ''
        ORDER BY run.id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished event surface repair decision run found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_event_surface_repair_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            decision_run_id INTEGER NOT NULL,
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
        CREATE TABLE IF NOT EXISTS ml_issue_event_surface_repair_checkpoint_items (
            id INTEGER PRIMARY KEY,
            checkpoint_run_id INTEGER NOT NULL,
            decision_run_id INTEGER NOT NULL,
            decision_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            agent_key TEXT NOT NULL,
            issue_family TEXT NOT NULL,
            issue_kind TEXT NOT NULL,
            queue_bucket TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            checkpoint_action TEXT NOT NULL,
            block_reason TEXT,
            token_status TEXT NOT NULL,
            current_text TEXT NOT NULL,
            corrected_text TEXT NOT NULL,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_event_surface_repair_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def fetch_rows(conn, *, decision_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            decision.id AS decision_id,
            decision.run_id AS decision_run_id,
            decision.queue_item_id,
            decision.ledger_run_id,
            decision.ledger_item_id,
            decision.segment_id,
            decision.relative_path,
            decision.source_key,
            decision.source_line_number,
            decision.agent_key,
            decision.issue_family,
            decision.issue_kind,
            decision.queue_bucket,
            decision.corrected_text,
            decision.notes,
            queue.evidence_text AS current_text
        FROM ml_issue_review_decisions decision
        JOIN ml_issue_review_queue_items queue ON queue.id = decision.queue_item_id
        WHERE decision.run_id = ?
          AND decision.valid = 1
          AND decision.validation_status = 'accepted'
          AND decision.normalized_decision = 'needs_repair'
          AND TRIM(COALESCE(decision.corrected_text, '')) <> ''
        ORDER BY decision.id
        """,
        (decision_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def validator_block_reason(text: str) -> str:
    result = validate_text(text)
    issues = result.get("issues") or []
    blockers = [
        f"{issue.get('code') or 'quality_issue'}:{issue.get('severity') or 'unknown'}"
        for issue in issues
        if str(issue.get("severity") or "").lower() in {"high", "critical"}
    ]
    return ",".join(blockers[:4])


def classify_checkpoint(row: dict[str, Any]) -> tuple[int, str, str]:
    current = str(row.get("current_text") or "")
    corrected = str(row.get("corrected_text") or "")
    if not corrected.strip():
        return 0, "missing_corrected_text", "missing_corrected_text"
    if current == corrected:
        return 0, "no_text_delta", "same_text"

    current_tokens = structural_tokens(current)
    corrected_tokens = structural_tokens(corrected)
    if current_tokens != corrected_tokens:
        return 0, "structural_tokens_changed", "structural_tokens_changed"

    validator_reason = validator_block_reason(corrected)
    if validator_reason:
        return 0, f"validator_block:{validator_reason}", "same_structural_tokens"

    return 1, "", "same_structural_tokens"


def report_paths(settings: dict[str, Any], decision_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_event_surface_repair_checkpoint_decision_run_{decision_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    decision_run_id: int,
    rows: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    fields = [
        "checkpoint_allowed",
        "block_reason",
        "decision_id",
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "agent_key",
        "subpolicy_name",
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
            handle.write(json.dumps({field: row.get(field) for field in fields}, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Issue event surface repair checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Policy: {POLICY_NAME}",
        f"Policy status: {POLICY_STATUS}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Decision run id: {decision_run_id}",
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
                f"  current: {short(row['current_text'], 180)}",
                f"  corrected: {short(row['corrected_text'], 180)}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Shadow checkpoint only: no source/output reads, no confirmations and no production writes.",
            "- Allowed items mean the issue has a token-safe corrected_text proposal, not that the whole segment is closed.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, decision_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        if not table_exists(conn, "ml_issue_review_decisions"):
            raise RuntimeError("Run issue_review_ingest first.")
        ensure_tables(conn)
        selected_decision_run_id = decision_run_id or latest_decision_run_id(conn)
        source_rows = fetch_rows(conn, decision_run_id=selected_decision_run_id)
        classified: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        now = db.utc_now()

        for source in source_rows:
            allowed, block_reason, token_status = classify_checkpoint(source)
            counts["allowed" if allowed else "blocked"] += 1
            if block_reason:
                counts[f"block:{block_reason}"] += 1
            classified.append(
                {
                    **source,
                    "subpolicy_name": SUBPOLICY_NAME,
                    "checkpoint_allowed": allowed,
                    "checkpoint_action": CHECKPOINT_ACTION,
                    "block_reason": block_reason,
                    "token_status": token_status,
                    "created_at": now,
                }
            )

        txt_path, csv_path, jsonl_path = report_paths(settings, selected_decision_run_id)
        finished_at = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_event_surface_repair_checkpoint_runs (
                rule_version,
                policy_name,
                policy_status,
                decision_run_id,
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
                POLICY_NAME,
                POLICY_STATUS,
                selected_decision_run_id,
                len(classified),
                counts["allowed"],
                counts["blocked"],
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                finished_at,
                finished_at,
            ),
        )
        checkpoint_run_id = int(cur.lastrowid)

        for row in classified:
            conn.execute(
                """
                INSERT INTO ml_issue_event_surface_repair_checkpoint_items (
                    checkpoint_run_id,
                    decision_run_id,
                    decision_id,
                    queue_item_id,
                    ledger_run_id,
                    ledger_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    agent_key,
                    issue_family,
                    issue_kind,
                    queue_bucket,
                    subpolicy_name,
                    checkpoint_allowed,
                    checkpoint_action,
                    block_reason,
                    token_status,
                    current_text,
                    corrected_text,
                    notes,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_run_id,
                    selected_decision_run_id,
                    row["decision_id"],
                    row["queue_item_id"],
                    row["ledger_run_id"],
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    row["agent_key"],
                    row["issue_family"],
                    row["issue_kind"],
                    row["queue_bucket"],
                    row["subpolicy_name"],
                    row["checkpoint_allowed"],
                    row["checkpoint_action"],
                    row["block_reason"],
                    row["token_status"],
                    row["current_text"],
                    row["corrected_text"],
                    row.get("notes") or "",
                    row["created_at"],
                ),
            )
        conn.commit()
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            checkpoint_run_id=checkpoint_run_id,
            decision_run_id=selected_decision_run_id,
            rows=classified,
            counts=counts,
        )

    print("[issue_event_surface_repair_checkpoint] Checkpoint generated")
    print(f"[issue_event_surface_repair_checkpoint] Rule version: {RULE_VERSION}")
    print(f"[issue_event_surface_repair_checkpoint] Checkpoint run id: {checkpoint_run_id}")
    print(f"[issue_event_surface_repair_checkpoint] Decision run id: {selected_decision_run_id}")
    print(f"[issue_event_surface_repair_checkpoint] Candidates: {len(classified):,}")
    print(f"[issue_event_surface_repair_checkpoint] Allowed: {counts['allowed']:,}")
    print(f"[issue_event_surface_repair_checkpoint] Blocked: {counts['blocked']:,}")
    print(f"[issue_event_surface_repair_checkpoint] Report: {txt_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "decision_run_id": selected_decision_run_id,
        "candidates": len(classified),
        "allowed": counts["allowed"],
        "blocked": counts["blocked"],
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Checkpoint event surface corrected_text repair evidence.")
    parser.add_argument("--decision-run-id", type=int, default=None)
    args = parser.parse_args()
    main(decision_run_id=args.decision_run_id)
