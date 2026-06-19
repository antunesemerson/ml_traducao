from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "issue_review_short_label_route_shadow_v1"
POLICY_NAME = "short_label_route_shadow"
POLICY_STATUS = "shadow"
AGENT_KEY = "micro_short_label_style"
ROUTE_DECISIONS = {"needs_repair", "needs_new_microagent"}


def stable_hash(value: str | None) -> str:
    return hashlib.sha1((value or "").encode("utf-8")).hexdigest()


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_route_shadow"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_decision_run_id(conn, *, agent_key: str) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_review_decision_runs
        WHERE agent_key = ?
          AND finished_at IS NOT NULL
          AND accepted_count > 0
        ORDER BY id DESC
        LIMIT 1
        """,
        (agent_key,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No completed issue-review decision run found for {agent_key!r}.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_route_shadow_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL DEFAULT 'shadow',
            agent_key TEXT NOT NULL,
            decision_run_id INTEGER NOT NULL,
            queue_run_id INTEGER,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            route_ready_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            subpolicy_counts_json TEXT,
            action_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_route_shadow_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            decision_id INTEGER NOT NULL,
            decision_run_id INTEGER NOT NULL,
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
            subpolicy_name TEXT NOT NULL,
            route_status TEXT NOT NULL,
            route_action TEXT NOT NULL,
            route_allowed INTEGER NOT NULL,
            block_reason TEXT,
            current_confirmed_text_hash TEXT,
            queue_confirmed_text_hash TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_short_label_route_shadow_runs(id) ON DELETE CASCADE
        )
        """
    )


def fetch_decision_run(conn, *, decision_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM ml_issue_review_decision_runs WHERE id = ?",
        (decision_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Issue-review decision run not found: {decision_run_id}")
    return dict(row)


def fetch_rows(conn, *, decision_run_id: int, agent_key: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            d.id AS decision_id,
            d.run_id AS decision_run_id,
            d.queue_run_id,
            d.queue_item_id,
            d.ledger_run_id,
            d.ledger_item_id,
            d.segment_id,
            d.relative_path,
            d.source_key,
            d.source_line_number,
            d.agent_key,
            d.issue_family,
            d.issue_kind,
            d.queue_bucket,
            d.normalized_decision,
            d.evidence_label,
            d.corrected_text AS decision_corrected_text,
            d.notes AS decision_notes,
            d.valid AS decision_valid,
            d.validation_status AS decision_validation_status,
            q.review_status AS queue_review_status,
            q.reviewer_decision AS queue_reviewer_decision,
            q.corrected_text AS queue_corrected_text,
            q.confirmed_text AS queue_confirmed_text,
            l.issue_family AS ledger_issue_family,
            l.issue_kind AS ledger_issue_kind,
            c.confirmed_text AS current_confirmed_text,
            c.locked AS confirmation_locked
        FROM ml_issue_review_decisions d
        JOIN ml_issue_review_queue_items q ON q.id = d.queue_item_id
        JOIN ml_issue_ledger_items l ON l.id = d.ledger_item_id
        LEFT JOIN segment_confirmations c
          ON c.id = (
              SELECT c2.id
              FROM segment_confirmations c2
              WHERE c2.segment_id = d.segment_id
              ORDER BY c2.updated_at DESC, c2.id DESC
              LIMIT 1
          )
        WHERE d.run_id = ?
          AND d.agent_key = ?
          AND d.valid = 1
          AND d.normalized_decision IN ('needs_repair', 'needs_new_microagent')
        ORDER BY d.relative_path, d.source_line_number, d.source_key, d.id
        """,
        (decision_run_id, agent_key),
    ).fetchall()
    return [dict(row) for row in rows]


def classify_route(row: dict[str, Any]) -> tuple[str, str]:
    notes = str(row.get("decision_notes") or "").casefold()
    bucket = str(row.get("queue_bucket") or "")
    decision = str(row.get("normalized_decision") or "")

    if decision == "needs_new_microagent" and "gender_dynamic_token" in notes:
        return "short_label_delegate_gender_token", "would_delegate_to_gender_token_microagent_shadow"
    if decision == "needs_new_microagent" and "dynamic_ck3_expression" in notes:
        return "short_label_delegate_dynamic_expression", "would_delegate_to_dynamic_expression_microagent_shadow"
    if decision == "needs_repair" and "spanish_residual" in notes:
        if bucket in {"short_dynamic_spanish_literal", "short_dynamic_expression"}:
            return "short_label_delegate_dynamic_spanish_repair", "would_delegate_to_dynamic_literal_or_spanish_repair_shadow"
        if bucket == "short_spanish_residual_literal":
            return "short_label_delegate_spanish_residual_repair", "would_delegate_to_spanish_residual_repair_shadow"
        return "short_label_delegate_spanish_residual_repair", "would_delegate_to_spanish_residual_repair_shadow"
    return "short_label_route_unclassified", "hold_for_manual_route_review"


def evaluate_row(row: dict[str, Any], *, global_reasons: list[str]) -> dict[str, Any]:
    subpolicy_name, route_action = classify_route(row)
    blockers = list(global_reasons)
    queue_text = row.get("queue_confirmed_text") or ""
    current_text = row.get("current_confirmed_text") or ""

    if row.get("queue_review_status") != "reviewed":
        blockers.append("queue_item_not_reviewed")
    if row.get("queue_reviewer_decision") != row.get("normalized_decision"):
        blockers.append("queue_decision_mismatch")
    if row.get("decision_validation_status") != "accepted":
        blockers.append("decision_not_accepted")
    if row.get("ledger_issue_family") != "short_label_style_microagent":
        blockers.append("ledger_family_mismatch")
    if (row.get("decision_corrected_text") or "").strip() or (row.get("queue_corrected_text") or "").strip():
        blockers.append("corrected_text_present")
    if int(row.get("confirmation_locked") or 0):
        blockers.append("locked_confirmation")
    if not current_text:
        blockers.append("missing_current_confirmation")
    if current_text and queue_text and current_text != queue_text:
        blockers.append("stale_confirmation_text_changed")
    if subpolicy_name == "short_label_route_unclassified":
        blockers.append("unclassified_route")

    route_allowed = 0 if blockers else 1
    route_status = "shadow_ready_delegate" if route_allowed else "shadow_blocked"
    return {
        **row,
        "subpolicy_name": subpolicy_name,
        "route_status": route_status,
        "route_action": route_action if route_allowed else "hold_for_manual_route_review",
        "route_allowed": route_allowed,
        "block_reason": ",".join(blockers) if blockers else "",
        "current_confirmed_text_hash": stable_hash(current_text),
        "queue_confirmed_text_hash": stable_hash(queue_text),
    }


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    decision_run: dict[str, Any],
    rows: list[dict[str, Any]],
) -> None:
    fieldnames = [
        "route_item_id",
        "decision_id",
        "queue_run_id",
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "queue_bucket",
        "normalized_decision",
        "evidence_label",
        "subpolicy_name",
        "route_status",
        "route_action",
        "route_allowed",
        "block_reason",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {field: row.get(field) for field in fieldnames}
            payload["confirmed_preview"] = short(row.get("current_confirmed_text"))
            payload["notes"] = row.get("decision_notes")
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    counts = Counter(row["route_status"] for row in rows)
    subpolicy_counts = Counter(row["subpolicy_name"] for row in rows if row["route_allowed"])
    blocker_counts = Counter(row["block_reason"] or "none" for row in rows)
    lines = [
        "Issue-review short-label route shadow",
        f"Rule version: {RULE_VERSION}",
        f"Policy: {POLICY_NAME} ({POLICY_STATUS})",
        f"Run id: {run_id}",
        f"Decision run id: {decision_run['id']}",
        f"Queue run id: {decision_run.get('queue_run_id')}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Route ready: {sum(1 for row in rows if row['route_allowed']):,}",
        f"- Blocked: {sum(1 for row in rows if not row['route_allowed']):,}",
        "",
        "By status:",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        "",
        "Allowed subpolicies:",
        *[f"- {key}: {value:,}" for key, value in subpolicy_counts.most_common()],
        "",
        "Blockers:",
        *[f"- {key}: {value:,}" for key, value in blocker_counts.most_common()],
        "",
        "Ready samples:",
    ]
    for row in [item for item in rows if item["route_allowed"]][:30]:
        lines.append(
            f"- {row['subpolicy_name']} | {row['relative_path']}::{row['source_key']} "
            f"({row['route_action']})"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Shadow only: no source/output writes, no confirmations, no model promotion.",
            "- This covers only the short-label routing issue; delegated textual repair remains owned by target specialists.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, decision_run_id: int | None = None, agent_key: str = AGENT_KEY) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    txt_path, csv_path, jsonl_path = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_decision_run_id = decision_run_id or latest_decision_run_id(conn, agent_key=agent_key)
        decision_run = fetch_decision_run(conn, decision_run_id=selected_decision_run_id)
        source_rows = fetch_rows(conn, decision_run_id=selected_decision_run_id, agent_key=agent_key)
        global_reasons: list[str] = []
        if agent_key != AGENT_KEY or decision_run.get("agent_key") != AGENT_KEY:
            global_reasons.append("agent_mismatch")
        if not source_rows:
            global_reasons.append("no_route_candidate_rows")
        rows = [evaluate_row(row, global_reasons=global_reasons) for row in source_rows]
        ready_count = sum(1 for row in rows if row["route_allowed"])
        blocked_count = len(rows) - ready_count
        subpolicy_counts = Counter(row["subpolicy_name"] for row in rows if row["route_allowed"])
        action_counts = Counter(row["route_action"] for row in rows if row["route_allowed"])
        blocker_counts = Counter(row["block_reason"] or "none" for row in rows)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_short_label_route_shadow_runs (
                rule_version,
                policy_name,
                policy_status,
                agent_key,
                decision_run_id,
                queue_run_id,
                candidate_count,
                route_ready_count,
                blocked_count,
                subpolicy_counts_json,
                action_counts_json,
                blocker_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                POLICY_NAME,
                POLICY_STATUS,
                AGENT_KEY,
                selected_decision_run_id,
                decision_run.get("queue_run_id"),
                len(rows),
                ready_count,
                blocked_count,
                json.dumps(dict(subpolicy_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(action_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(blocker_counts), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
        for row in rows:
            item_cursor = conn.execute(
                """
                INSERT INTO ml_issue_short_label_route_shadow_items (
                    run_id,
                    decision_id,
                    decision_run_id,
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
                    subpolicy_name,
                    route_status,
                    route_action,
                    route_allowed,
                    block_reason,
                    current_confirmed_text_hash,
                    queue_confirmed_text_hash,
                    notes,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    row["decision_id"],
                    row["decision_run_id"],
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
                    row["normalized_decision"],
                    row["evidence_label"],
                    row["subpolicy_name"],
                    row["route_status"],
                    row["route_action"],
                    int(row["route_allowed"]),
                    row["block_reason"],
                    row["current_confirmed_text_hash"],
                    row["queue_confirmed_text_hash"],
                    row["decision_notes"],
                    now,
                ),
            )
            row["route_item_id"] = int(item_cursor.lastrowid)
        conn.commit()

    write_outputs(txt_path=txt_path, csv_path=csv_path, jsonl_path=jsonl_path, run_id=run_id, decision_run=decision_run, rows=rows)
    print("[issue_review_short_label_route_shadow] Shadow route generated")
    print(f"[issue_review_short_label_route_shadow] Run id: {run_id}")
    print(f"[issue_review_short_label_route_shadow] Decision run id: {selected_decision_run_id}")
    print(f"[issue_review_short_label_route_shadow] Candidates: {len(rows):,}")
    print(f"[issue_review_short_label_route_shadow] Route ready: {ready_count:,}")
    print(f"[issue_review_short_label_route_shadow] Blocked: {blocked_count:,}")
    print(f"[issue_review_short_label_route_shadow] Report: {txt_path}")
    return {
        "run_id": run_id,
        "decision_run_id": selected_decision_run_id,
        "candidate_count": len(rows),
        "route_ready_count": ready_count,
        "blocked_count": blocked_count,
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create shadow routing coverage for short-label decisions delegated to other specialists.")
    parser.add_argument("--decision-run-id", type=int, default=None)
    parser.add_argument("--agent-key", default=AGENT_KEY)
    args = parser.parse_args()
    main(decision_run_id=args.decision_run_id, agent_key=args.agent_key)
