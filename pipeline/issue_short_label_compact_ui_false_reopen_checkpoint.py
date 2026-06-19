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


RULE_VERSION = "issue_short_label_compact_ui_false_reopen_checkpoint_v1"
CHECKPOINT_NAME = "short_label_compact_ui_false_reopen_checkpoint_v1"
POLICY_NAME = "short_label_compact_ui_semantic_false_reopen_shadow"
AGENT_KEY = "micro_short_label_style"
ISSUE_FAMILY = "short_label_style_microagent"
QUEUE_STRATEGY = "short_label_compact_ui_semantic_stratified_learning_queue"
CHECKPOINT_ACTION = "cover_short_label_compact_ui_false_reopen"
SUBPOLICY_NAME = "compact_ui_false_positive_reopen"
SAFE_DECISION = "false_positive_reopen"


def report_paths(settings: dict[str, Any], queue_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_compact_ui_false_reopen_checkpoint_queue_{queue_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_compact_ui_false_reopen_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            promotion_status TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            queue_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            source_segment_state_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_blocked_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            reason_counts_json TEXT,
            bucket_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_short_label_compact_ui_false_reopen_checkpoint_items (
            id INTEGER PRIMARY KEY,
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
            agent_key TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            current_final_state TEXT,
            current_text TEXT,
            evidence_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_short_label_compact_ui_false_reopen_checkpoint_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_short_label_compact_ui_false_reopen_checkpoint_items_run
        ON ml_issue_short_label_compact_ui_false_reopen_checkpoint_items(checkpoint_run_id, checkpoint_allowed, segment_id);

        CREATE INDEX IF NOT EXISTS idx_short_label_compact_ui_false_reopen_checkpoint_items_ledger
        ON ml_issue_short_label_compact_ui_false_reopen_checkpoint_items(ledger_item_id, checkpoint_allowed);
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
          AND selected_count > 0
        ORDER BY id DESC
        LIMIT 1
        """,
        (AGENT_KEY, QUEUE_STRATEGY),
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished compact UI semantic review queue found.")
    return int(row["id"])


def latest_segment_state_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished segment-state run found.")
    return int(row["id"])


def fetch_queue_run(conn, *, queue_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_review_queue_runs
        WHERE id = ?
        """,
        (queue_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Queue run not found: {queue_run_id}")
    return dict(row)


def parse_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def fetch_rows(conn, *, queue_run_id: int, segment_state_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.id AS queue_item_id,
            item.run_id AS queue_run_id,
            item.ledger_run_id,
            item.ledger_item_id,
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.source_line_number,
            item.issue_family,
            item.issue_kind,
            item.agent_key,
            item.queue_bucket,
            item.suggested_decision,
            item.evidence_text,
            item.evidence_json,
            item.confirmed_text,
            state.final_state,
            state.state_group,
            state.output_state,
            state.review_state AS segment_review_state,
            state.apply_state,
            state.confirmed_matches_output,
            state.needs_output_apply,
            state.needs_reopen,
            state.is_closed,
            state.reasons_json AS state_reasons_json
        FROM ml_issue_review_queue_items item
        LEFT JOIN segment_state_items state
          ON state.run_id = ?
         AND state.segment_id = item.segment_id
        WHERE item.run_id = ?
        ORDER BY item.queue_bucket, item.priority_score DESC, item.segment_id
        """,
        (segment_state_run_id, queue_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def row_block_reason(row: dict[str, Any], queue_run: dict[str, Any]) -> str:
    reasons: list[str] = []
    evidence = parse_json_dict(row.get("evidence_json"))
    if queue_run.get("agent_key") != AGENT_KEY:
        reasons.append("wrong_queue_agent")
    if queue_run.get("queue_strategy") != QUEUE_STRATEGY:
        reasons.append("wrong_queue_strategy")
    if row.get("agent_key") != AGENT_KEY:
        reasons.append("wrong_item_agent")
    if row.get("issue_family") != ISSUE_FAMILY:
        reasons.append("wrong_issue_family")
    if row.get("suggested_decision") != SAFE_DECISION:
        reasons.append("decision_not_false_positive_reopen")
    if row.get("issue_kind") != "short_or_compact_label_reopened":
        reasons.append("issue_kind_not_plain_short_label_reopen")
    if row.get("final_state") != "reopen_auto_confirmed_autofix":
        reasons.append("not_current_reopen_auto_confirmed_autofix")
    if row.get("state_group") != "pending":
        reasons.append("state_group_not_pending")
    if row.get("segment_review_state") != "auto_confirmed":
        reasons.append("segment_not_auto_confirmed")
    if int(row.get("confirmed_matches_output") or 0) != 1:
        reasons.append("confirmed_output_mismatch")
    if int(row.get("needs_output_apply") or 0) != 0:
        reasons.append("needs_output_apply")
    if int(row.get("needs_reopen") or 0) != 1:
        reasons.append("not_marked_as_reopen")
    if evidence.get("active_token_status") not in (None, "ok"):
        reasons.append("active_token_status_not_ok")
    if evidence.get("candidate_token_status") not in (None, "ok"):
        reasons.append("candidate_token_status_not_ok")
    if evidence.get("issue_codes"):
        reasons.append("quality_issue_codes_present")
    if not str(row.get("confirmed_text") or row.get("evidence_text") or "").strip():
        reasons.append("empty_text")
    return ";".join(reasons)


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    queue_run: dict[str, Any],
    segment_state_run_id: int,
    rows: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    fields = [
        "checkpoint_allowed",
        "block_reason",
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "issue_kind",
        "checkpoint_action",
        "current_final_state",
        "current_text",
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
        "Short-label Compact UI False-Reopen Checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Queue run id: {queue_run['id']}",
        f"Ledger run id: {queue_run['ledger_run_id']}",
        f"Segment-state run id: {segment_state_run_id}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Allowed issue items: {counts['allowed']:,}",
        f"- Blocked issue items: {counts['blocked']:,}",
        "",
        "Buckets:",
    ]
    for key, value in counts.most_common():
        if key.startswith("bucket:"):
            lines.append(f"- {key.removeprefix('bucket:')}: {value:,}")
    lines.extend(["", "Blockers:"])
    for key, value in counts.most_common():
        if key.startswith("block:"):
            lines.append(f"- {key.removeprefix('block:')}: {value:,}")
    lines.extend(["", "Allowed samples:"])
    for row in [item for item in rows if item["checkpoint_allowed"]][:60]:
        lines.append(
            f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']} | {short(row['current_text'], 150)}"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This checkpoint covers only the short-label false-reopen issue.",
            "- It does not write source/output, create confirmations, or promote lifecycle policy.",
            "- Multi-issue segments still require coverage from the other responsible neurons.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, queue_run_id: int | None = None, segment_state_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = db.utc_now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_queue_run_id = queue_run_id or latest_queue_run_id(conn)
        selected_state_run_id = segment_state_run_id or latest_segment_state_run_id(conn)
        queue_run = fetch_queue_run(conn, queue_run_id=selected_queue_run_id)
        source_rows = fetch_rows(conn, queue_run_id=selected_queue_run_id, segment_state_run_id=selected_state_run_id)

        rows: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        for row in source_rows:
            reason = row_block_reason(row, queue_run)
            allowed = 0 if reason else 1
            if allowed:
                counts["allowed"] += 1
                counts[f"bucket:{row.get('queue_bucket') or 'unknown'}"] += 1
            else:
                counts["blocked"] += 1
                counts[f"block:{reason}"] += 1
            rows.append(
                {
                    "queue_run_id": selected_queue_run_id,
                    "queue_item_id": row["queue_item_id"],
                    "ledger_run_id": row["ledger_run_id"],
                    "ledger_item_id": row["ledger_item_id"],
                    "segment_id": row["segment_id"],
                    "relative_path": row["relative_path"],
                    "source_key": row["source_key"],
                    "source_line_number": row["source_line_number"],
                    "queue_bucket": row["queue_bucket"],
                    "issue_family": row["issue_family"],
                    "issue_kind": row["issue_kind"],
                    "agent_key": AGENT_KEY,
                    "subpolicy_name": SUBPOLICY_NAME,
                    "checkpoint_action": CHECKPOINT_ACTION if allowed else "blocked",
                    "checkpoint_allowed": allowed,
                    "block_reason": reason,
                    "current_final_state": row.get("final_state") or "",
                    "current_text": row.get("confirmed_text") or row.get("evidence_text") or "",
                    "evidence_json": row.get("evidence_json") or "",
                    "created_at": db.utc_now(),
                }
            )

        allowed_count = counts["allowed"]
        blocked_count = counts["blocked"]
        checkpoint_status = "ready_for_lifecycle_policy" if allowed_count else "blocked"
        promotion_status = "shadow_checkpoint_candidate" if allowed_count else "blocked"
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_short_label_compact_ui_false_reopen_checkpoint_runs (
                rule_version, checkpoint_name, checkpoint_status, promotion_status,
                policy_name, policy_status, agent_key, queue_run_id, ledger_run_id,
                source_segment_state_run_id, candidate_count, checkpoint_allowed_count,
                checkpoint_blocked_count, production_release_allowed, reason_counts_json,
                bucket_counts_json, started_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'shadow', ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                CHECKPOINT_NAME,
                checkpoint_status,
                promotion_status,
                POLICY_NAME,
                AGENT_KEY,
                selected_queue_run_id,
                int(queue_run["ledger_run_id"]),
                selected_state_run_id,
                len(rows),
                allowed_count,
                blocked_count,
                json.dumps({k: v for k, v in counts.items() if not k.startswith("bucket:")}, ensure_ascii=False, sort_keys=True),
                json.dumps({k.removeprefix("bucket:"): v for k, v in counts.items() if k.startswith("bucket:")}, ensure_ascii=False, sort_keys=True),
                started_at,
                started_at,
            ),
        )
        checkpoint_run_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO ml_issue_short_label_compact_ui_false_reopen_checkpoint_items (
                checkpoint_run_id, queue_run_id, queue_item_id, ledger_run_id, ledger_item_id,
                segment_id, relative_path, source_key, source_line_number, queue_bucket,
                issue_family, issue_kind, agent_key, subpolicy_name, checkpoint_action,
                checkpoint_allowed, block_reason, current_final_state, current_text,
                evidence_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    checkpoint_run_id,
                    row["queue_run_id"],
                    row["queue_item_id"],
                    row["ledger_run_id"],
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["queue_bucket"],
                    row["issue_family"],
                    row["issue_kind"],
                    row["agent_key"],
                    row["subpolicy_name"],
                    row["checkpoint_action"],
                    row["checkpoint_allowed"],
                    row["block_reason"],
                    row["current_final_state"],
                    row["current_text"],
                    row["evidence_json"],
                    row["created_at"],
                )
                for row in rows
            ],
        )

        txt_path, csv_path, jsonl_path = report_paths(settings, selected_queue_run_id)
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            checkpoint_run_id=checkpoint_run_id,
            queue_run=queue_run,
            segment_state_run_id=selected_state_run_id,
            rows=rows,
            counts=counts,
        )
        conn.execute(
            """
            UPDATE ml_issue_short_label_compact_ui_false_reopen_checkpoint_runs
            SET report_path = ?,
                csv_path = ?,
                jsonl_path = ?,
                finished_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (str(txt_path), str(csv_path), str(jsonl_path), db.utc_now(), db.utc_now(), checkpoint_run_id),
        )
        conn.commit()

    result = {
        "checkpoint_run_id": checkpoint_run_id,
        "queue_run_id": selected_queue_run_id,
        "segment_state_run_id": selected_state_run_id,
        "candidates": len(rows),
        "allowed": allowed_count,
        "blocked": blocked_count,
        "report": str(txt_path),
        "csv": str(csv_path),
        "jsonl": str(jsonl_path),
    }
    print("[issue_short_label_compact_ui_false_reopen_checkpoint] Checkpoint generated")
    for key, value in result.items():
        print(f"[issue_short_label_compact_ui_false_reopen_checkpoint] {key}: {value}")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create compact UI short-label false-reopen coverage checkpoint.")
    parser.add_argument("--queue-run-id", type=int)
    parser.add_argument("--segment-state-run-id", type=int)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id, segment_state_run_id=args.segment_state_run_id)
