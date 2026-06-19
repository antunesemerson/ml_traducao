from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short
from segment_token_policy_decisions import load_decision_rows


RULE_VERSION = "auto_confirmation_reopen_text_review_ingest_v1"

SKIP_LABELS = {"", "pending", "skip", "skipped", "not_reviewed"}
POSITIVE_LABELS = {
    "approve",
    "approved",
    "approve_current_confirmed",
    "correct",
    "ok",
    "safe",
    "valid",
    "valid_confirmation",
    "no_change",
}
NEGATIVE_LABELS = {
    "fix",
    "fix_confirmed_text",
    "minor_fix",
    "major_fix",
    "residual_spanish",
    "semantic_error",
    "structure_error",
    "structural_error",
    "token_mismatch",
    "reject",
    "rejected",
    "reject_current_confirmed",
    "needs_fix",
    "likely_needs_fix",
}
CONTEXT_LABELS = {
    "needs_more_context",
    "context_required",
    "dynamic_context_review",
    "manual_boundary",
    "manual_boundary_review",
}
MANUAL_EXCEPTION_LABELS = {
    "manual_exception",
    "manual_exception_only",
    "contextual_exception",
    "keep_manual_exception_only",
}


def sha256_text(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_token(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


def extract_queue_item_id(row: dict[str, Any]) -> int | None:
    for key in ("queue_item_id", "text_queue_item_id", "item_id", "id"):
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def extract_raw_label(row: dict[str, Any]) -> str:
    for key in ("decision", "human_label", "review_label", "label", "evidence_label"):
        value = normalize_token(row.get(key))
        if value and value != "pending":
            return value
    if (row.get("corrected_text") or "").strip():
        return "fix_confirmed_text"
    return normalize_token(row.get("decision"))


def normalize_decision(row: dict[str, Any]) -> tuple[str, str] | None:
    label = extract_raw_label(row)
    if label in SKIP_LABELS:
        return None
    if label in POSITIVE_LABELS:
        return "approve_current_confirmed", "positive_evidence"
    if label in NEGATIVE_LABELS:
        return "fix_or_reject_current_confirmed", "negative_boundary"
    if label in CONTEXT_LABELS:
        return "needs_more_context", "needs_more_context"
    if label in MANUAL_EXCEPTION_LABELS:
        return "manual_exception_only", "manual_exception_only"
    return "unmapped_review_label", "needs_more_context"


def fetch_queue_item(conn, *, queue_item_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
            item.*,
            run.agent_key,
            run.diagnostic_run_id,
            source.english_text,
            source.spanish_text,
            output.portuguese_text,
            confirmation.confirmed_text
        FROM auto_confirmation_reopen_text_review_queue_items item
        JOIN auto_confirmation_reopen_text_review_queue_runs run ON run.id = item.run_id
        JOIN source_segments source ON source.id = item.segment_id
        LEFT JOIN output_segments output ON output.segment_id = item.segment_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.id = (
              SELECT c.id
              FROM segment_confirmations c
              WHERE c.segment_id = item.segment_id
              ORDER BY c.updated_at DESC, c.id DESC
              LIMIT 1
          )
        WHERE item.id = ?
        """,
        (queue_item_id,),
    ).fetchone()
    return dict(row) if row else None


def insert_run(
    conn,
    *,
    queue_run_id: int | None,
    agent_key: str | None,
    decisions_path: Path,
    started_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO auto_confirmation_reopen_text_review_decision_runs (
            rule_version,
            queue_run_id,
            agent_key,
            decisions_path,
            started_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (RULE_VERSION, queue_run_id, agent_key, str(decisions_path), started_at, started_at),
    )
    return int(cur.lastrowid)


def upsert_decision(
    conn,
    *,
    run_id: int,
    queue_item: dict[str, Any],
    decision: str,
    evidence_label: str,
    decision_row: dict[str, Any],
    reviewer: str | None,
    now: str,
) -> None:
    corrected_text = (decision_row.get("corrected_text") or decision_row.get("proposed_text") or "").strip()
    notes = decision_row.get("notes") or decision_row.get("reason") or ""
    row_reviewer = decision_row.get("reviewer") or reviewer or ""
    reasons = {
        "rule_version": RULE_VERSION,
        "raw_label": extract_raw_label(decision_row),
        "suggested_review_label": queue_item.get("suggested_review_label"),
        "select_cstring_count": queue_item.get("select_cstring_count"),
        "spanish_literal_hint_count": queue_item.get("spanish_literal_hint_count"),
        "issue_count": queue_item.get("issue_count"),
    }
    conn.execute(
        """
        INSERT INTO auto_confirmation_reopen_text_review_decisions (
            run_id,
            queue_run_id,
            queue_item_id,
            diagnostic_run_id,
            diagnostic_item_id,
            segment_id,
            relative_path,
            source_key,
            source_line_number,
            agent_key,
            text_subfamily,
            risk_level,
            decision,
            evidence_label,
            corrected_text,
            notes,
            reviewer,
            current_confirmed_text_hash,
            corrected_text_hash,
            reasons_json,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(queue_item_id) DO UPDATE SET
            run_id = excluded.run_id,
            decision = excluded.decision,
            evidence_label = excluded.evidence_label,
            corrected_text = excluded.corrected_text,
            notes = excluded.notes,
            reviewer = excluded.reviewer,
            current_confirmed_text_hash = excluded.current_confirmed_text_hash,
            corrected_text_hash = excluded.corrected_text_hash,
            reasons_json = excluded.reasons_json,
            updated_at = excluded.updated_at
        """,
        (
            run_id,
            int(queue_item["run_id"]),
            int(queue_item["id"]),
            queue_item.get("diagnostic_run_id"),
            queue_item.get("diagnostic_item_id"),
            int(queue_item["segment_id"]),
            queue_item["relative_path"],
            queue_item["source_key"],
            queue_item.get("source_line_number"),
            queue_item["agent_key"],
            queue_item["text_subfamily"],
            queue_item["risk_level"],
            decision,
            evidence_label,
            corrected_text,
            notes,
            row_reviewer,
            sha256_text(queue_item.get("confirmed_text")),
            sha256_text(corrected_text) if corrected_text else None,
            json.dumps(reasons, ensure_ascii=False, sort_keys=True),
            now,
            now,
        ),
    )


def report_path(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return reports_dir / f"{stamp}_auto_confirmation_reopen_text_review_ingest.txt"


def update_run(
    conn,
    *,
    run_id: int,
    counts: Counter,
    report: Path,
    finished_at: str,
) -> None:
    conn.execute(
        """
        UPDATE auto_confirmation_reopen_text_review_decision_runs
        SET
            total_rows = ?,
            accepted_count = ?,
            positive_count = ?,
            negative_count = ?,
            needs_more_context_count = ?,
            manual_exception_count = ?,
            skipped_count = ?,
            report_path = ?,
            finished_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            counts["total_rows"],
            counts["accepted"],
            counts["positive_evidence"],
            counts["negative_boundary"],
            counts["needs_more_context"],
            counts["manual_exception_only"],
            counts["skipped"],
            str(report),
            finished_at,
            finished_at,
            run_id,
        ),
    )


def main(
    *,
    decisions_path: str,
    queue_run_id: int | None = None,
    reviewer: str | None = None,
) -> dict[str, Any]:
    settings = db.load_settings()
    decisions_file = db.project_path(decisions_path)
    if not decisions_file.exists():
        raise FileNotFoundError(decisions_file)
    rows = load_decision_rows(decisions_file)
    started_at = datetime.now()
    now = started_at.isoformat(timespec="seconds")
    counts: Counter = Counter()
    preview_lines: list[str] = []
    selected_agent: str | None = None

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        run_id = insert_run(
            conn,
            queue_run_id=queue_run_id,
            agent_key=None,
            decisions_path=decisions_file,
            started_at=now,
        )
        for index, decision_row in enumerate(rows, start=1):
            counts["total_rows"] += 1
            queue_item_id = extract_queue_item_id(decision_row)
            normalized = normalize_decision(decision_row)
            if queue_item_id is None:
                counts["skipped"] += 1
                preview_lines.append(f"- row {index}: skipped_missing_queue_item_id")
                continue
            if normalized is None:
                counts["skipped"] += 1
                continue
            queue_item = fetch_queue_item(conn, queue_item_id=queue_item_id)
            if queue_item is None:
                counts["skipped"] += 1
                preview_lines.append(f"- queue_item {queue_item_id}: skipped_missing_queue_item")
                continue
            if queue_run_id is not None and int(queue_item["run_id"]) != int(queue_run_id):
                counts["skipped"] += 1
                preview_lines.append(
                    f"- queue_item {queue_item_id}: skipped_queue_run_mismatch={queue_item['run_id']} expected={queue_run_id}"
                )
                continue
            decision, evidence_label = normalized
            selected_agent = selected_agent or queue_item["agent_key"]
            upsert_decision(
                conn,
                run_id=run_id,
                queue_item=queue_item,
                decision=decision,
                evidence_label=evidence_label,
                decision_row=decision_row,
                reviewer=reviewer,
                now=now,
            )
            counts["accepted"] += 1
            counts[evidence_label] += 1
            preview_lines.append(
                (
                    f"- {evidence_label}: {queue_item['relative_path']}:"
                    f"{queue_item['source_line_number']}:{queue_item['source_key']} "
                    f"({short(queue_item.get('confirmed_text'), 90)})"
                )
            )

        report = report_path(settings)
        lines = [
            "Auto-confirmation text review ingest",
            f"Rule version: {RULE_VERSION}",
            f"Decision run id: {run_id}",
            f"Decisions path: {decisions_file}",
            f"Started at: {started_at.isoformat(timespec='seconds')}",
            "",
            "Summary:",
            f"- Rows read: {counts['total_rows']:,}",
            f"- Accepted: {counts['accepted']:,}",
            f"- Positive: {counts['positive_evidence']:,}",
            f"- Negative/boundary: {counts['negative_boundary']:,}",
            f"- Needs more context: {counts['needs_more_context']:,}",
            f"- Manual exception only: {counts['manual_exception_only']:,}",
            f"- Skipped: {counts['skipped']:,}",
            "",
            "Preview:",
            *preview_lines[:40],
            "",
            "Safety note:",
            "- This ingest only records review evidence for learning.",
            "- It does not update confirmations, train models, promote policies, or write output files.",
        ]
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        finished_at = datetime.now().isoformat(timespec="seconds")
        update_run(conn, run_id=run_id, counts=counts, report=report, finished_at=finished_at)
        if selected_agent:
            conn.execute(
                "UPDATE auto_confirmation_reopen_text_review_decision_runs SET agent_key = ? WHERE id = ?",
                (selected_agent, run_id),
            )
        conn.commit()

    print("[auto_confirmation_reopen_text_review_ingest] Decisions ingested")
    print(f"[auto_confirmation_reopen_text_review_ingest] Run id: {run_id}")
    print(f"[auto_confirmation_reopen_text_review_ingest] Rows read: {counts['total_rows']:,}")
    print(f"[auto_confirmation_reopen_text_review_ingest] Accepted: {counts['accepted']:,}")
    print(f"[auto_confirmation_reopen_text_review_ingest] Skipped: {counts['skipped']:,}")
    print(f"[auto_confirmation_reopen_text_review_ingest] Report: {report}")
    return {
        "run_id": run_id,
        "rows_read": counts["total_rows"],
        "accepted": counts["accepted"],
        "skipped": counts["skipped"],
        "report_path": str(report),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest human decisions for text-replacement review queues.")
    parser.add_argument("--decisions-path", required=True)
    parser.add_argument("--queue-run-id", type=int, default=None)
    parser.add_argument("--reviewer", default=None)
    args = parser.parse_args()
    main(decisions_path=args.decisions_path, queue_run_id=args.queue_run_id, reviewer=args.reviewer)
