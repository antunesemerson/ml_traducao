from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "actionable_pending_lifecycle_gap_policy_v1"
POLICY_NAME = "actionable_pending_reviewed_false_reopen_closure"
POLICY_ACTION = "close_reopen_actionable_pending_false_positive"
LABEL_FAMILY = "actionable_pending_lifecycle_gap_microagent"
REVIEWER = "codex_learning_front"


def latest_state_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL
          AND total_segments > 1000
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No segment-state run found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_actionable_pending_lifecycle_gap_policy"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_rows(conn, *, queue_run_id: int, state_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH issue_summary AS (
          SELECT
            segment_id,
            COUNT(*) AS issue_count,
            SUM(CASE WHEN lower(severity) IN ('high', 'error', 'critical') THEN 1 ELSE 0 END) AS high_issue_count
          FROM issues
          GROUP BY segment_id
        )
        SELECT
          decision.id AS decision_id,
          decision.queue_run_id,
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
          decision.normalized_decision,
          decision.evidence_label,
          decision.corrected_text,
          decision.reviewer,
          decision.valid,
          decision.created_at AS decision_created_at,
          state.id AS state_item_id,
          state.final_state,
          state.state_group,
          state.apply_state,
          state.confirmed_matches_output,
          state.output_state,
          state.review_state,
          state.lifecycle_policy_allowed,
          confirmation.confirmation_level,
          confirmation.confirmation_source,
          confirmation.confirmation_label,
          confirmation.locked,
          output.portuguese_text,
          COALESCE(issue_summary.issue_count, 0) AS issue_count,
          COALESCE(issue_summary.high_issue_count, 0) AS high_issue_count
        FROM ml_issue_review_decisions decision
        JOIN segment_state_items state
          ON state.segment_id = decision.segment_id
         AND state.run_id = ?
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.segment_id = decision.segment_id
        LEFT JOIN output_segments output
          ON output.segment_id = decision.segment_id
        LEFT JOIN issue_summary
          ON issue_summary.segment_id = decision.segment_id
        WHERE decision.queue_run_id = ?
          AND decision.valid = 1
          AND decision.normalized_decision = 'false_positive_reopen'
          AND decision.evidence_label = 'false_positive_reopen'
        ORDER BY decision.relative_path, decision.source_line_number, decision.segment_id
        """,
        (state_run_id, queue_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def humanish(row: dict[str, Any]) -> bool:
    review_state = str(row.get("review_state") or "").lower()
    confirmation_source = str(row.get("confirmation_source") or "").lower()
    confirmation_label = str(row.get("confirmation_label") or "").lower()
    if any(token in review_state for token in ("human", "manual")):
        return True
    return any(token in f"{confirmation_source} {confirmation_label}" for token in ("human", "manual", "gemini"))


def evaluate_row(row: dict[str, Any], *, state_run_id: int) -> tuple[int, str]:
    if int(row.get("valid") or 0) != 1:
        return 0, "decision_not_valid"
    if row.get("reviewer") != REVIEWER:
        return 0, "wrong_reviewer"
    if row.get("normalized_decision") != "false_positive_reopen":
        return 0, "not_false_positive_reopen"
    if row.get("evidence_label") != "false_positive_reopen":
        return 0, "wrong_evidence_label"
    if str(row.get("corrected_text") or "").strip():
        return 0, "corrected_text_not_empty"
    if row.get("final_state") != "reopen_auto_confirmed":
        return 0, "not_current_reopen_auto_confirmed"
    if row.get("state_group") != "pending" or row.get("apply_state") != "needs_review":
        return 0, "unexpected_segment_state"
    if int(row.get("confirmed_matches_output") or 0) != 1:
        return 0, "confirmation_output_mismatch"
    if int(row.get("lifecycle_policy_allowed") or 0) != 0:
        return 0, "already_lifecycle_allowed"
    if int(row.get("locked") or 0) == 1 or humanish(row):
        return 0, "human_or_locked_confirmation"
    if int(row.get("issue_count") or 0) != 0:
        return 0, "issue_signal_present"
    if int(row.get("high_issue_count") or 0) != 0:
        return 0, "high_issue_signal_present"
    if not str(row.get("portuguese_text") or "").strip():
        return 0, "missing_output_text"
    return 1, ""


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    policy_run_id: int,
    state_run_id: int,
    rows: list[dict[str, Any]],
) -> None:
    fieldnames = [
        "policy_item_id",
        "decision_id",
        "queue_run_id",
        "queue_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "final_state",
        "policy_action",
        "policy_allowed",
        "block_reason",
        "issue_count",
        "high_issue_count",
        "reviewer",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    counts = Counter("released" if row["policy_allowed"] else row["block_reason"] for row in rows)
    lines = [
        "Actionable pending lifecycle gap policy",
        f"Rule version: {RULE_VERSION}",
        f"Policy name: {POLICY_NAME}",
        f"Policy action: {POLICY_ACTION}",
        f"Policy run id: {policy_run_id}",
        f"Segment-state run id: {state_run_id}",
        "",
        "Summary:",
        f"- candidates: {len(rows):,}",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        "",
        "Released:",
    ]
    released = [row for row in rows if row["policy_allowed"]]
    if released:
        for row in released:
            lines.append(f"- segment={row['segment_id']} | {row['relative_path']}:{row['source_line_number']} | {row['source_key']}")
    else:
        lines.append("- none")
    blocked = [row for row in rows if not row["policy_allowed"]]
    lines.append("")
    lines.append("Blocked:")
    if blocked:
        for row in blocked:
            lines.append(f"- {row['block_reason']} | segment={row['segment_id']} | {row['relative_path']}:{row['source_line_number']} | {row['source_key']}")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- This policy only materializes reviewed false-positive reopen evidence for segment-state lifecycle closure.",
            "- It does not write output files and does not alter segment confirmations.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, queue_run_id: int = 40, state_run_id: int | None = None, policy_status: str = "active") -> dict[str, Any]:
    settings = db.load_settings()
    selected_state_run_id: int
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_state_run_id = state_run_id or latest_state_run_id(conn)
        rows = fetch_rows(conn, queue_run_id=queue_run_id, state_run_id=selected_state_run_id)
        for row in rows:
            allowed, block_reason = evaluate_row(row, state_run_id=selected_state_run_id)
            row["policy_action"] = POLICY_ACTION
            row["policy_allowed"] = allowed
            row["block_reason"] = block_reason
            row["label_family"] = LABEL_FAMILY
        txt_path, csv_path, jsonl_path = report_paths(settings)
        counts = Counter("released" if row["policy_allowed"] else row["block_reason"] for row in rows)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_lifecycle_policy_runs (
                rule_version,
                queue_run_id,
                audit_run_id,
                policy_name,
                label_family,
                policy_status,
                candidate_count,
                released_count,
                blocked_count,
                manual_boundary_count,
                invalid_count,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                None,
                POLICY_NAME,
                LABEL_FAMILY,
                policy_status,
                len(rows),
                counts["released"],
                len(rows) - counts["released"],
                len(rows) - counts["released"],
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        policy_run_id = int(cursor.lastrowid)
        for row in rows:
            item_cursor = conn.execute(
                """
                INSERT INTO auto_confirmation_reopen_lifecycle_policy_items (
                    run_id,
                    queue_run_id,
                    queue_item_id,
                    audit_run_id,
                    audit_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    label_family,
                    confirmation_label,
                    policy_action,
                    policy_allowed,
                    block_reason,
                    output_match_kind,
                    token_status,
                    issue_count,
                    high_issue_count,
                    model_safe_probability,
                    review_priority,
                    reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    policy_run_id,
                    None,
                    None,
                    None,
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    LABEL_FAMILY,
                    row.get("confirmation_label"),
                    POLICY_ACTION,
                    int(row["policy_allowed"]),
                    row["block_reason"],
                    "confirmed_output_match" if int(row.get("confirmed_matches_output") or 0) else "mismatch",
                    "not_applicable",
                    int(row.get("issue_count") or 0),
                    int(row.get("high_issue_count") or 0),
                    0.0,
                    json.dumps(
                        {
                            "decision_id": row.get("decision_id"),
                            "queue_run_id": row.get("queue_run_id"),
                            "queue_item_id": row.get("queue_item_id"),
                            "state_item_id": row.get("state_item_id"),
                            "reviewer": row.get("reviewer"),
                            "evidence_label": row.get("evidence_label"),
                            "normalized_decision": row.get("normalized_decision"),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            row["policy_item_id"] = int(item_cursor.lastrowid)
        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            policy_run_id=policy_run_id,
            state_run_id=selected_state_run_id,
            rows=rows,
        )
        conn.commit()
    print("[actionable_pending_lifecycle_gap_policy] Policy generated")
    print(f"[actionable_pending_lifecycle_gap_policy] Run id: {policy_run_id}")
    print(f"[actionable_pending_lifecycle_gap_policy] State run id: {selected_state_run_id}")
    print(f"[actionable_pending_lifecycle_gap_policy] Candidates: {len(rows)}")
    print(f"[actionable_pending_lifecycle_gap_policy] Released: {counts['released']}")
    print(f"[actionable_pending_lifecycle_gap_policy] Blocked: {len(rows) - counts['released']}")
    print(f"[actionable_pending_lifecycle_gap_policy] Report: {txt_path}")
    return {
        "run_id": policy_run_id,
        "state_run_id": selected_state_run_id,
        "released": counts["released"],
        "blocked": len(rows) - counts["released"],
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create lifecycle closure policy for reviewed actionable false reopens.")
    parser.add_argument("--queue-run-id", type=int, default=40)
    parser.add_argument("--state-run-id", type=int, default=None)
    parser.add_argument("--status", choices=["shadow", "active"], default="active")
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id, state_run_id=args.state_run_id, policy_status=args.status)
