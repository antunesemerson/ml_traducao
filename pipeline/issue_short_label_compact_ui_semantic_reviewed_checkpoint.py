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


RULE_VERSION = "issue_short_label_compact_ui_semantic_reviewed_checkpoint_v1"
CHECKPOINT_NAME = "short_label_compact_ui_semantic_reviewed_false_reopen_checkpoint_v1"
CHECKPOINT_ACTION = "cover_short_label_compact_ui_semantic_reviewed_false_reopen"
POLICY_NAME = "short_label_compact_ui_semantic_reviewed_false_reopen_shadow"
AGENT_KEY = "micro_short_label_style"
ISSUE_FAMILY = "short_label_style_microagent"
QUEUE_STRATEGY = "short_label_compact_ui_semantic_stratified_learning_queue"
SAFE_DECISION = "false_positive_reopen"
SUBPOLICY_NAME = "compact_ui_semantic_reviewed_false_positive_reopen"


def report_paths(settings: dict[str, Any], queue_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_compact_ui_semantic_reviewed_checkpoint_queue_{queue_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_compact_ui_semantic_reviewed_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            queue_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            source_segment_state_run_id INTEGER NOT NULL,
            decision_count INTEGER NOT NULL DEFAULT 0,
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

        CREATE TABLE IF NOT EXISTS ml_issue_short_label_compact_ui_semantic_reviewed_checkpoint_items (
            id INTEGER PRIMARY KEY,
            checkpoint_run_id INTEGER NOT NULL,
            queue_run_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            decision_id INTEGER NOT NULL,
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
            current_final_state TEXT,
            current_text TEXT,
            evidence_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_short_label_compact_ui_semantic_reviewed_checkpoint_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_short_label_compact_ui_semantic_reviewed_checkpoint_items_run
        ON ml_issue_short_label_compact_ui_semantic_reviewed_checkpoint_items(checkpoint_run_id, checkpoint_allowed, segment_id);

        CREATE INDEX IF NOT EXISTS idx_short_label_compact_ui_semantic_reviewed_checkpoint_items_ledger
        ON ml_issue_short_label_compact_ui_semantic_reviewed_checkpoint_items(ledger_item_id, checkpoint_allowed);
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
        raise RuntimeError("No reviewed compact UI semantic queue found.")
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


def fetch_queue_run(conn, queue_run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM ml_issue_review_queue_runs WHERE id = ?", (queue_run_id,)).fetchone()
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
            decision.id AS decision_id,
            decision.queue_item_id,
            decision.queue_run_id,
            decision.ledger_run_id,
            decision.ledger_item_id,
            decision.segment_id,
            decision.relative_path,
            decision.source_key,
            decision.source_line_number,
            decision.issue_family,
            decision.issue_kind,
            decision.agent_key,
            decision.queue_bucket,
            decision.normalized_decision,
            decision.evidence_label,
            decision.valid,
            decision.validation_status,
            item.evidence_text,
            item.evidence_json,
            item.confirmed_text,
            state.final_state,
            state.state_group,
            state.review_state AS segment_review_state,
            state.confirmed_matches_output,
            state.needs_output_apply,
            state.needs_reopen
        FROM ml_issue_review_decisions decision
        JOIN ml_issue_review_queue_items item ON item.id = decision.queue_item_id
        LEFT JOIN segment_state_items state
          ON state.run_id = ?
         AND state.segment_id = decision.segment_id
        WHERE decision.queue_run_id = ?
          AND decision.valid = 1
          AND decision.validation_status = 'accepted'
        ORDER BY decision.queue_bucket, decision.segment_id
        """,
        (segment_state_run_id, queue_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def block_reason(row: dict[str, Any], queue_run: dict[str, Any]) -> str:
    reasons: list[str] = []
    evidence = parse_json_dict(row.get("evidence_json"))
    if queue_run.get("agent_key") != AGENT_KEY:
        reasons.append("wrong_queue_agent")
    if queue_run.get("queue_strategy") != QUEUE_STRATEGY:
        reasons.append("wrong_queue_strategy")
    if row.get("agent_key") != AGENT_KEY:
        reasons.append("wrong_decision_agent")
    if row.get("issue_family") != ISSUE_FAMILY:
        reasons.append("wrong_issue_family")
    if row.get("issue_kind") != "short_or_compact_label_reopened":
        reasons.append("issue_kind_not_plain_short_label_reopen")
    if row.get("normalized_decision") != SAFE_DECISION:
        reasons.append("decision_not_false_positive_reopen")
    if row.get("evidence_label") != "false_positive_reopen":
        reasons.append("evidence_not_false_positive_reopen")
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
        "decision_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "normalized_decision",
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
        "Short-label compact UI semantic reviewed checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Queue run id: {queue_run['id']}",
        f"Ledger run id: {queue_run['ledger_run_id']}",
        f"Segment-state run id: {segment_state_run_id}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Decisions: {len(rows):,}",
        f"- Allowed issue items: {counts['allowed']:,}",
        f"- Blocked decisions: {counts['blocked']:,}",
        "",
        "Decision counts:",
    ]
    for key, value in counts.most_common():
        if key.startswith("decision:"):
            lines.append(f"- {key.removeprefix('decision:')}: {value:,}")
    lines.extend(["", "Blockers:"])
    for key, value in counts.most_common():
        if key.startswith("block:"):
            lines.append(f"- {key.removeprefix('block:')}: {value:,}")
    lines.extend(["", "Allowed samples:"])
    for row in [item for item in rows if item["checkpoint_allowed"]][:40]:
        lines.append(
            f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']} | {short(row['current_text'], 120)}"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This checkpoint marks reviewed issue-level coverage only.",
            "- It does not write source/output, create confirmations, or promote lifecycle policy.",
            "- Segment closure still requires full issue coverage and a separate lifecycle bridge.",
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
        queue_run = fetch_queue_run(conn, selected_queue_run_id)
        rows = fetch_rows(conn, queue_run_id=selected_queue_run_id, segment_state_run_id=selected_state_run_id)
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_queue_run_id)

        checkpoint_run_id = int(
            conn.execute(
                """
                INSERT INTO ml_issue_short_label_compact_ui_semantic_reviewed_checkpoint_runs (
                    rule_version, checkpoint_name, checkpoint_status, policy_name, policy_status,
                    agent_key, queue_run_id, ledger_run_id, source_segment_state_run_id,
                    decision_count, checkpoint_allowed_count, checkpoint_blocked_count,
                    production_release_allowed, reason_counts_json, bucket_counts_json,
                    report_path, csv_path, jsonl_path, started_at, finished_at, updated_at
                )
                VALUES (?, ?, 'shadow', ?, 'shadow', ?, ?, ?, ?, 0, 0, 0, 0, '{}', '{}', ?, ?, ?, ?, NULL, ?)
                """,
                (
                    RULE_VERSION,
                    CHECKPOINT_NAME,
                    POLICY_NAME,
                    AGENT_KEY,
                    selected_queue_run_id,
                    queue_run["ledger_run_id"],
                    selected_state_run_id,
                    str(txt_path),
                    str(csv_path),
                    str(jsonl_path),
                    started_at,
                    started_at,
                ),
            ).lastrowid
        )

        created_at = db.utc_now()
        output_rows: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        for row in rows:
            reason = block_reason(row, queue_run)
            allowed = 0 if reason else 1
            counts["allowed" if allowed else "blocked"] += 1
            counts[f"decision:{row.get('normalized_decision') or 'unknown'}"] += 1
            if reason:
                for part in reason.split(";"):
                    counts[f"block:{part}"] += 1
            output = {
                "checkpoint_run_id": checkpoint_run_id,
                "queue_run_id": selected_queue_run_id,
                "queue_item_id": row["queue_item_id"],
                "decision_id": row["decision_id"],
                "ledger_run_id": row["ledger_run_id"],
                "ledger_item_id": row["ledger_item_id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "queue_bucket": row["queue_bucket"],
                "issue_family": row["issue_family"],
                "issue_kind": row["issue_kind"],
                "normalized_decision": row["normalized_decision"],
                "evidence_label": row["evidence_label"],
                "agent_key": row["agent_key"],
                "subpolicy_name": SUBPOLICY_NAME,
                "checkpoint_action": CHECKPOINT_ACTION,
                "checkpoint_allowed": allowed,
                "block_reason": reason,
                "current_final_state": row.get("final_state"),
                "current_text": row.get("confirmed_text") or row.get("evidence_text"),
                "evidence_json": row.get("evidence_json"),
                "created_at": created_at,
            }
            output_rows.append(output)

        columns = [
            "checkpoint_run_id",
            "queue_run_id",
            "queue_item_id",
            "decision_id",
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
            "current_final_state",
            "current_text",
            "evidence_json",
            "created_at",
        ]
        if output_rows:
            placeholders = ", ".join("?" for _ in columns)
            conn.executemany(
                f"""
                INSERT INTO ml_issue_short_label_compact_ui_semantic_reviewed_checkpoint_items (
                    {", ".join(columns)}
                )
                VALUES ({placeholders})
                """,
                [tuple(row[column] for column in columns) for row in output_rows],
            )

        reason_counts = {k.removeprefix("block:"): v for k, v in counts.items() if k.startswith("block:")}
        bucket_counts = Counter(row["queue_bucket"] for row in output_rows if row["checkpoint_allowed"])
        finished_at = db.utc_now()
        conn.execute(
            """
            UPDATE ml_issue_short_label_compact_ui_semantic_reviewed_checkpoint_runs
            SET decision_count = ?,
                checkpoint_allowed_count = ?,
                checkpoint_blocked_count = ?,
                reason_counts_json = ?,
                bucket_counts_json = ?,
                finished_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                len(output_rows),
                counts["allowed"],
                counts["blocked"],
                json.dumps(reason_counts, ensure_ascii=False, sort_keys=True),
                json.dumps(dict(bucket_counts.most_common()), ensure_ascii=False, sort_keys=True),
                finished_at,
                finished_at,
                checkpoint_run_id,
            ),
        )
        conn.commit()

    write_reports(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        checkpoint_run_id=checkpoint_run_id,
        queue_run=queue_run,
        segment_state_run_id=selected_state_run_id,
        rows=output_rows,
        counts=counts,
    )
    print("[issue_short_label_compact_ui_semantic_reviewed_checkpoint] Checkpoint generated")
    print(f"[issue_short_label_compact_ui_semantic_reviewed_checkpoint] Run id: {checkpoint_run_id}")
    print(f"[issue_short_label_compact_ui_semantic_reviewed_checkpoint] Queue run id: {selected_queue_run_id}")
    print(f"[issue_short_label_compact_ui_semantic_reviewed_checkpoint] Decisions: {len(output_rows):,}")
    print(f"[issue_short_label_compact_ui_semantic_reviewed_checkpoint] Allowed: {counts['allowed']:,}")
    print(f"[issue_short_label_compact_ui_semantic_reviewed_checkpoint] Blocked: {counts['blocked']:,}")
    print(f"[issue_short_label_compact_ui_semantic_reviewed_checkpoint] Report: {txt_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "queue_run_id": selected_queue_run_id,
        "allowed": counts["allowed"],
        "blocked": counts["blocked"],
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Checkpoint reviewed compact UI semantic false-positive reopen decisions.")
    parser.add_argument("--queue-run-id", type=int)
    parser.add_argument("--segment-state-run-id", type=int)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id, segment_state_run_id=args.segment_state_run_id)
