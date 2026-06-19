from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from segment_token_policy_decisions import load_decision_rows


RULE_VERSION = "issue_review_ingest_v1"

SKIP_LABELS = {"", "pending", "skip", "skipped", "not_reviewed", "todo"}
SAFE_LABELS = {
    "approve",
    "approved",
    "correct",
    "ok",
    "safe",
    "valid",
    "positive",
    "safe_short_label",
    "safe_surface",
    "current_ok",
    "keep_current",
}
FALSE_POSITIVE_LABELS = {
    "false_positive",
    "false_positive_reopen",
    "false_reopen",
    "reopen_false_positive",
    "safe_reopen",
    "active_safe_ok",
}
REPAIR_LABELS = {
    "fix",
    "repair",
    "needs_fix",
    "needs_repair",
    "minor_fix",
    "major_fix",
    "residual_spanish",
    "semantic_error",
    "structure_error",
    "structural_error",
    "token_mismatch",
    "corrected",
    "correction",
}
CONTEXT_LABELS = {
    "context",
    "needs_context",
    "needs_domain_context",
    "context_required",
    "needs_more_context",
    "dynamic_context_review",
    "manual_context",
    "unclear",
}
NEW_MICROAGENT_LABELS = {
    "new_microagent",
    "needs_new_microagent",
    "create_microagent",
    "split_microagent",
    "needs_subagent",
    "needs_subpolicy",
    "new_subpolicy",
}
MANUAL_EXCEPTION_LABELS = {
    "manual_exception",
    "manual_exception_only",
    "contextual_exception",
    "manual_override",
    "keep_manual_exception_only",
}
COMPOSITION_READY_LABELS = {
    "composition_ready",
    "ready_for_composition",
    "multiagent_ready",
    "multi_agent_ready",
}

DECISION_EVIDENCE = {
    "safe_short_label": "positive_evidence",
    "false_positive_reopen": "false_positive_reopen",
    "needs_repair": "repair_required",
    "needs_domain_context": "context_required",
    "needs_new_microagent": "new_microagent_required",
    "manual_exception": "manual_exception",
    "composition_ready": "composition_ready",
}


def normalize_token(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


def short(value: str | None, limit: int = 120) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def extract_int(row: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def extract_queue_item_id(row: dict[str, Any]) -> int | None:
    return extract_int(row, ("queue_item_id", "issue_queue_item_id", "item_id", "id"))


def extract_queue_run_id(row: dict[str, Any]) -> int | None:
    return extract_int(row, ("queue_run_id", "issue_queue_run_id", "run_id"))


def extract_ledger_item_id(row: dict[str, Any]) -> int | None:
    return extract_int(row, ("ledger_item_id", "issue_ledger_item_id"))


def extract_raw_label(row: dict[str, Any]) -> str:
    for key in ("decision", "reviewer_decision", "human_decision", "review_label", "label", "evidence_label"):
        label = normalize_token(row.get(key))
        if label and label != "pending":
            return label
    corrected_text = str(row.get("corrected_text") or row.get("proposed_text") or "").strip()
    if corrected_text:
        return "needs_repair"
    return ""


def normalize_decision(row: dict[str, Any]) -> tuple[str, str, str] | None:
    raw_label = extract_raw_label(row)
    if raw_label in SKIP_LABELS:
        return None
    if raw_label in SAFE_LABELS:
        return raw_label, "safe_short_label", DECISION_EVIDENCE["safe_short_label"]
    if raw_label in FALSE_POSITIVE_LABELS:
        return raw_label, "false_positive_reopen", DECISION_EVIDENCE["false_positive_reopen"]
    if raw_label in REPAIR_LABELS:
        return raw_label, "needs_repair", DECISION_EVIDENCE["needs_repair"]
    if raw_label in CONTEXT_LABELS:
        return raw_label, "needs_domain_context", DECISION_EVIDENCE["needs_domain_context"]
    if raw_label in NEW_MICROAGENT_LABELS:
        return raw_label, "needs_new_microagent", DECISION_EVIDENCE["needs_new_microagent"]
    if raw_label in MANUAL_EXCEPTION_LABELS:
        return raw_label, "manual_exception", DECISION_EVIDENCE["manual_exception"]
    if raw_label in COMPOSITION_READY_LABELS:
        return raw_label, "composition_ready", DECISION_EVIDENCE["composition_ready"]
    return raw_label, "invalid_unmapped_label", "invalid"


def first_queue_run_id(rows: list[dict[str, Any]], fallback: int | None) -> int | None:
    if fallback is not None:
        return fallback
    for row in rows:
        queue_run_id = extract_queue_run_id(row)
        if queue_run_id is not None:
            return queue_run_id
    return None


def fetch_queue_run(conn, queue_run_id: int | None) -> dict[str, Any] | None:
    if queue_run_id is None:
        return None
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_review_queue_runs
        WHERE id = ?
        LIMIT 1
        """,
        (queue_run_id,),
    ).fetchone()
    return dict(row) if row else None


def fetch_queue_item(
    conn,
    *,
    queue_item_id: int | None,
    queue_run_id: int | None,
    ledger_item_id: int | None,
) -> dict[str, Any] | None:
    if queue_item_id is not None:
        params: list[Any] = [queue_item_id]
        run_filter = ""
        if queue_run_id is not None:
            run_filter = "AND item.run_id = ?"
            params.append(queue_run_id)
        row = conn.execute(
            f"""
            SELECT
                item.*,
                run.rule_version AS queue_rule_version,
                run.agent_key AS queue_run_agent_key,
                run.issue_family AS queue_run_issue_family
            FROM ml_issue_review_queue_items item
            JOIN ml_issue_review_queue_runs run ON run.id = item.run_id
            WHERE item.id = ?
              {run_filter}
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()
        return dict(row) if row else None

    if queue_run_id is None or ledger_item_id is None:
        return None

    row = conn.execute(
        """
        SELECT
            item.*,
            run.rule_version AS queue_rule_version,
            run.agent_key AS queue_run_agent_key,
            run.issue_family AS queue_run_issue_family
        FROM ml_issue_review_queue_items item
        JOIN ml_issue_review_queue_runs run ON run.id = item.run_id
        WHERE item.run_id = ?
          AND item.ledger_item_id = ?
        LIMIT 1
        """,
        (queue_run_id, ledger_item_id),
    ).fetchone()
    return dict(row) if row else None


def insert_decision_run(
    conn,
    *,
    queue_run_id: int | None,
    agent_key: str | None,
    decisions_path: Path,
    source_report: str | None,
    reviewer: str | None,
    started_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO ml_issue_review_decision_runs (
            rule_version,
            queue_run_id,
            agent_key,
            decisions_path,
            source_report,
            reviewer,
            started_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (RULE_VERSION, queue_run_id, agent_key, str(decisions_path), source_report, reviewer, started_at, started_at),
    )
    return int(cur.lastrowid)


def upsert_decision(
    conn,
    *,
    run_id: int,
    queue_item: dict[str, Any],
    raw_label: str,
    normalized_decision: str,
    evidence_label: str,
    decision_row: dict[str, Any],
    reviewer: str | None,
    now: str,
) -> None:
    corrected_text = str(decision_row.get("corrected_text") or decision_row.get("proposed_text") or "").strip()
    notes = str(decision_row.get("notes") or decision_row.get("reason") or "").strip()
    row_reviewer = str(decision_row.get("reviewer") or reviewer or "").strip()
    reasons = {
        "rule_version": RULE_VERSION,
        "raw_label": raw_label,
        "suggested_decision": queue_item.get("suggested_decision"),
        "queue_bucket": queue_item.get("queue_bucket"),
        "issue_family": queue_item.get("issue_family"),
        "issue_kind": queue_item.get("issue_kind"),
        "source": "ml_issue_review_queue",
    }
    conn.execute(
        """
        INSERT INTO ml_issue_review_decisions (
            run_id,
            queue_run_id,
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
            suggested_decision,
            reviewer_decision,
            normalized_decision,
            evidence_label,
            corrected_text,
            notes,
            reviewer,
            valid,
            validation_status,
            reasons_json,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'accepted', ?, ?, ?)
        ON CONFLICT(queue_item_id) DO UPDATE SET
            run_id = excluded.run_id,
            reviewer_decision = excluded.reviewer_decision,
            normalized_decision = excluded.normalized_decision,
            evidence_label = excluded.evidence_label,
            corrected_text = excluded.corrected_text,
            notes = excluded.notes,
            reviewer = excluded.reviewer,
            valid = excluded.valid,
            validation_status = excluded.validation_status,
            reasons_json = excluded.reasons_json,
            updated_at = excluded.updated_at
        """,
        (
            run_id,
            int(queue_item["run_id"]),
            int(queue_item["id"]),
            int(queue_item["ledger_run_id"]),
            int(queue_item["ledger_item_id"]),
            int(queue_item["segment_id"]),
            queue_item["relative_path"],
            queue_item["source_key"],
            queue_item.get("source_line_number"),
            queue_item["agent_key"],
            queue_item["issue_family"],
            queue_item["issue_kind"],
            queue_item["queue_bucket"],
            queue_item.get("suggested_decision"),
            raw_label,
            normalized_decision,
            evidence_label,
            corrected_text,
            notes,
            row_reviewer,
            json.dumps(reasons, ensure_ascii=False, sort_keys=True),
            now,
            now,
        ),
    )
    conn.execute(
        """
        UPDATE ml_issue_review_queue_items
        SET
            review_status = 'reviewed',
            reviewer_decision = ?,
            reviewer_notes = ?,
            corrected_text = ?,
            reviewed_at = ?
        WHERE id = ?
        """,
        (normalized_decision, notes, corrected_text, now, int(queue_item["id"])),
    )
    conn.execute(
        """
        UPDATE ml_issue_ledger_items
        SET validation_status = ?
        WHERE id = ?
        """,
        (evidence_label, int(queue_item["ledger_item_id"])),
    )


def update_queue_run_counts(conn, queue_run_id: int, now: str) -> None:
    reviewed_row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM ml_issue_review_queue_items
        WHERE run_id = ?
          AND review_status = 'reviewed'
        """,
        (queue_run_id,),
    ).fetchone()
    selected_row = conn.execute(
        """
        SELECT selected_count
        FROM ml_issue_review_queue_runs
        WHERE id = ?
        """,
        (queue_run_id,),
    ).fetchone()
    reviewed_count = int(reviewed_row["count"] or 0)
    selected_count = int(selected_row["selected_count"] or 0) if selected_row else 0
    conn.execute(
        """
        UPDATE ml_issue_review_queue_runs
        SET
            reviewed_count = ?,
            open_count = MAX(0, ? - ?),
            updated_at = ?
        WHERE id = ?
        """,
        (reviewed_count, selected_count, reviewed_count, now, queue_run_id),
    )


def update_linked_queue_run_counts(conn, queue_run_id: int, now: str) -> None:
    linked_tables = (
        "ml_issue_long_text_composition_recheck_queue_runs",
        "ml_issue_custom_localization_composition_recheck_queue_runs",
    )
    counts = conn.execute(
        """
        SELECT
            COUNT(*) AS selected_count,
            SUM(CASE WHEN review_status = 'reviewed' THEN 1 ELSE 0 END) AS reviewed_count
        FROM ml_issue_review_queue_items
        WHERE run_id = ?
        """,
        (queue_run_id,),
    ).fetchone()
    selected_count = int(counts["selected_count"] or 0) if counts else 0
    reviewed_count = int(counts["reviewed_count"] or 0) if counts else 0
    open_count = max(0, selected_count - reviewed_count)

    for table_name in linked_tables:
        exists = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
              AND name = ?
            """,
            (table_name,),
        ).fetchone()
        if not exists:
            continue
        conn.execute(
            f"""
            UPDATE {table_name}
            SET
                selected_count = ?,
                open_count = ?,
                reviewed_count = ?,
                updated_at = ?
            WHERE review_queue_run_id = ?
            """,
            (selected_count, open_count, reviewed_count, now, queue_run_id),
        )


def update_decision_run(
    conn,
    *,
    run_id: int,
    queue_run_id: int | None,
    agent_key: str | None,
    counts: Counter,
    report_path: Path,
    finished_at: str,
) -> None:
    evidence_counts = {
        key: value
        for key, value in sorted(counts.items())
        if key.startswith("evidence_") or key.startswith("decision_")
    }
    conn.execute(
        """
        UPDATE ml_issue_review_decision_runs
        SET
            queue_run_id = ?,
            agent_key = ?,
            total_rows = ?,
            accepted_count = ?,
            skipped_count = ?,
            invalid_count = ?,
            safe_count = ?,
            false_positive_count = ?,
            repair_count = ?,
            context_count = ?,
            manual_exception_count = ?,
            new_microagent_count = ?,
            evidence_counts_json = ?,
            report_path = ?,
            finished_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            queue_run_id,
            agent_key,
            counts["total_rows"],
            counts["accepted"],
            counts["skipped"],
            counts["invalid"],
            counts["decision_safe_short_label"],
            counts["decision_false_positive_reopen"],
            counts["decision_needs_repair"],
            counts["decision_needs_domain_context"],
            counts["decision_manual_exception"],
            counts["decision_needs_new_microagent"],
            json.dumps(evidence_counts, ensure_ascii=False, sort_keys=True),
            str(report_path),
            finished_at,
            finished_at,
            run_id,
        ),
    )


def main(
    *,
    decisions_path: str,
    queue_run_id: int | None = None,
    source_report: str | None = None,
    reviewer: str | None = None,
) -> dict[str, Any]:
    settings = db.load_settings()
    path = db.project_path(decisions_path)
    if not path.exists():
        raise FileNotFoundError(path)

    rows = load_decision_rows(path)
    selected_queue_run_id = first_queue_run_id(rows, queue_run_id)
    started_at = datetime.now()
    started_at_db = started_at.isoformat(timespec="seconds")
    counts: Counter = Counter()
    preview_lines: list[str] = []
    invalid_lines: list[str] = []
    skipped_lines: list[str] = []
    touched_queue_runs: set[int] = set()
    selected_agent: str | None = None

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        queue_run = fetch_queue_run(conn, selected_queue_run_id)
        selected_agent = queue_run.get("agent_key") if queue_run else None
        run_id = insert_decision_run(
            conn,
            queue_run_id=selected_queue_run_id,
            agent_key=selected_agent,
            decisions_path=path,
            source_report=source_report,
            reviewer=reviewer,
            started_at=started_at_db,
        )

        for index, decision_row in enumerate(rows, start=1):
            counts["total_rows"] += 1
            normalized = normalize_decision(decision_row)
            if normalized is None:
                counts["skipped"] += 1
                if len(skipped_lines) < 30:
                    skipped_lines.append(f"- row {index}: skipped_empty_decision")
                continue

            raw_label, normalized_decision, evidence_label = normalized
            if evidence_label == "invalid":
                counts["invalid"] += 1
                if len(invalid_lines) < 30:
                    invalid_lines.append(f"- row {index}: invalid_unmapped_label={raw_label}")
                continue

            row_queue_run_id = extract_queue_run_id(decision_row) or selected_queue_run_id
            queue_item = fetch_queue_item(
                conn,
                queue_item_id=extract_queue_item_id(decision_row),
                queue_run_id=row_queue_run_id,
                ledger_item_id=extract_ledger_item_id(decision_row),
            )
            if queue_item is None:
                counts["skipped"] += 1
                if len(skipped_lines) < 30:
                    skipped_lines.append(f"- row {index}: skipped_missing_queue_item")
                continue

            now = db.utc_now()
            upsert_decision(
                conn,
                run_id=run_id,
                queue_item=queue_item,
                raw_label=raw_label,
                normalized_decision=normalized_decision,
                evidence_label=evidence_label,
                decision_row=decision_row,
                reviewer=reviewer,
                now=now,
            )
            selected_agent = selected_agent or queue_item["agent_key"]
            touched_queue_runs.add(int(queue_item["run_id"]))
            counts["accepted"] += 1
            counts[f"decision_{normalized_decision}"] += 1
            counts[f"evidence_{evidence_label}"] += 1
            preview_lines.append(
                (
                    f"- {normalized_decision}: item={queue_item['id']} "
                    f"segment={queue_item['segment_id']} {queue_item['relative_path']}:"
                    f"{queue_item.get('source_line_number') or '?'}:{queue_item['source_key']} "
                    f"| {short(queue_item.get('confirmed_text'), 90)}"
                )
            )

        finished_at = datetime.now().isoformat(timespec="seconds")
        for touched_queue_run_id in sorted(touched_queue_runs):
            update_queue_run_counts(conn, touched_queue_run_id, finished_at)
            update_linked_queue_run_counts(conn, touched_queue_run_id, finished_at)

        report_lines = [
            "Issue review decision ingest",
            f"Started at: {started_at.isoformat(timespec='seconds')}",
            f"Rule version: {RULE_VERSION}",
            f"Decision run id: {run_id}",
            f"Queue run id: {selected_queue_run_id or 'mixed/unknown'}",
            f"Agent: {selected_agent or 'unknown'}",
            f"Decision file: {path}",
            f"Source report: {source_report or 'none'}",
            f"Reviewer: {reviewer or 'row/default'}",
            "",
            "Summary:",
            f"- Rows loaded: {counts['total_rows']}",
            f"- Accepted decisions: {counts['accepted']}",
            f"- Skipped rows: {counts['skipped']}",
            f"- Invalid rows: {counts['invalid']}",
            f"- Safe short label: {counts['decision_safe_short_label']}",
            f"- False positive reopen: {counts['decision_false_positive_reopen']}",
            f"- Needs repair: {counts['decision_needs_repair']}",
            f"- Needs domain context: {counts['decision_needs_domain_context']}",
            f"- Needs new microagent: {counts['decision_needs_new_microagent']}",
            f"- Manual exception: {counts['decision_manual_exception']}",
            "",
            "Accepted preview:",
            *preview_lines[:80],
            "",
            "Invalid preview:",
            *invalid_lines,
            "",
            "Skipped preview:",
            *skipped_lines,
            "",
            "Safety note:",
            "- This ingest records learning evidence and queue review status only.",
            "- It does not write source files, output files, confirmations, or promoted models.",
        ]
        report_path = db.write_report(settings, "issue_review_ingest", report_lines)
        update_decision_run(
            conn,
            run_id=run_id,
            queue_run_id=selected_queue_run_id,
            agent_key=selected_agent,
            counts=counts,
            report_path=report_path,
            finished_at=finished_at,
        )
        conn.commit()

    print("[issue_review_ingest] Decisions ingested")
    print(f"[issue_review_ingest] Rule version: {RULE_VERSION}")
    print(f"[issue_review_ingest] Decision run id: {run_id}")
    print(f"[issue_review_ingest] Queue run id: {selected_queue_run_id or 'mixed/unknown'}")
    print(f"[issue_review_ingest] Rows loaded: {counts['total_rows']}")
    print(f"[issue_review_ingest] Accepted: {counts['accepted']}")
    print(f"[issue_review_ingest] Skipped: {counts['skipped']}")
    print(f"[issue_review_ingest] Invalid: {counts['invalid']}")
    print(f"[issue_review_ingest] Report: {report_path}")
    return {
        "run_id": run_id,
        "queue_run_id": selected_queue_run_id,
        "rows_loaded": counts["total_rows"],
        "accepted": counts["accepted"],
        "skipped": counts["skipped"],
        "invalid": counts["invalid"],
        "report_path": str(report_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest human decisions for issue review queues.")
    parser.add_argument("--decisions-path", required=True)
    parser.add_argument("--queue-run-id", type=int, default=None)
    parser.add_argument("--source-report", default=None)
    parser.add_argument("--reviewer", default=None)
    args = parser.parse_args()
    main(
        decisions_path=args.decisions_path,
        queue_run_id=args.queue_run_id,
        source_report=args.source_report,
        reviewer=args.reviewer,
    )
