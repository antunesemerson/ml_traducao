from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short
from segment_token_gender_split_evidence_queue import EVIDENCE_LABELS
from segment_token_policy_decisions import load_decision_rows, sha256_text
from segment_token_policy_review_queue import latest_policy_run_id


RULE_VERSION = "segment_token_gender_split_evidence_ingest_v1"
VALID_LABELS = set(EVIDENCE_LABELS)
POSITIVE_LABEL = "positive_evidence"
NEGATIVE_LABEL = "negative_boundary"
MORE_CONTEXT_LABEL = "needs_more_context"
MANUAL_LABEL = "manual_exception_only"


def load_queue_rows(path: Path) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"Queue JSONL line {line_number} is not an object.")
        policy_item_id = payload.get("policy_item_id")
        if policy_item_id in (None, ""):
            raise ValueError(f"Queue JSONL line {line_number} has no policy_item_id.")
        rows[int(policy_item_id)] = payload
    return rows


def extract_policy_item_id(row: dict[str, Any]) -> int | None:
    for key in ("policy_item_id", "item_id", "id"):
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def extract_label(row: dict[str, Any]) -> str | None:
    for key in ("evidence_label", "decision", "review_decision", "label"):
        value = row.get(key)
        if value:
            return str(value).strip()
    return None


def insert_run(
    conn,
    *,
    policy_run_id: int,
    source_queue_path: Path,
    decisions_path: Path,
    started_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO segment_token_gender_split_evidence_runs (
            rule_version,
            policy_run_id,
            source_queue_path,
            decisions_path,
            started_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (RULE_VERSION, policy_run_id, str(source_queue_path), str(decisions_path), started_at, started_at),
    )
    return int(cur.lastrowid)


def upsert_item(
    conn,
    *,
    run_id: int,
    queue_row: dict[str, Any],
    decision_row: dict[str, Any],
    evidence_label: str,
    now: str,
) -> None:
    corrected_text = decision_row.get("corrected_text") or decision_row.get("proposed_text") or ""
    notes = decision_row.get("notes") or decision_row.get("reason") or ""
    reviewer = decision_row.get("reviewer") or ""
    conn.execute(
        """
        INSERT INTO segment_token_gender_split_evidence_items (
            run_id,
            policy_run_id,
            policy_item_id,
            segment_id,
            relative_path,
            source_key,
            source_line_number,
            gender_subtype,
            split_agent,
            split_maturity,
            method_signature,
            evidence_label,
            corrected_text,
            notes,
            reviewer,
            hypothesis,
            missing_tokens_json,
            extra_tokens_json,
            split_reasons_json,
            confirmed_text_hash,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(policy_run_id, policy_item_id, split_agent) DO UPDATE SET
            run_id = excluded.run_id,
            split_maturity = excluded.split_maturity,
            method_signature = excluded.method_signature,
            evidence_label = excluded.evidence_label,
            corrected_text = excluded.corrected_text,
            notes = excluded.notes,
            reviewer = excluded.reviewer,
            hypothesis = excluded.hypothesis,
            missing_tokens_json = excluded.missing_tokens_json,
            extra_tokens_json = excluded.extra_tokens_json,
            split_reasons_json = excluded.split_reasons_json,
            confirmed_text_hash = excluded.confirmed_text_hash,
            updated_at = excluded.updated_at
        """,
        (
            run_id,
            int(queue_row["policy_run_id"]),
            int(queue_row["policy_item_id"]),
            int(queue_row["segment_id"]),
            queue_row["relative_path"],
            queue_row["source_key"],
            queue_row.get("source_line_number"),
            queue_row["gender_subtype"],
            queue_row["split_agent"],
            queue_row["split_maturity"],
            queue_row["method_signature"],
            evidence_label,
            corrected_text,
            notes,
            reviewer,
            queue_row.get("evidence_hypothesis") or "",
            json.dumps(queue_row.get("missing_tokens") or [], ensure_ascii=False),
            json.dumps(queue_row.get("extra_tokens") or [], ensure_ascii=False),
            json.dumps(queue_row.get("split_reasons") or [], ensure_ascii=False),
            sha256_text(queue_row.get("confirmed_text")),
            now,
            now,
        ),
    )


def update_run(
    conn,
    *,
    run_id: int,
    counts: Counter,
    report_path: Path,
    finished_at: str,
) -> None:
    conn.execute(
        """
        UPDATE segment_token_gender_split_evidence_runs
        SET
            total_decisions = ?,
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
            counts["accepted"],
            counts[POSITIVE_LABEL],
            counts[NEGATIVE_LABEL],
            counts[MORE_CONTEXT_LABEL],
            counts[MANUAL_LABEL],
            counts["skipped"],
            str(report_path),
            finished_at,
            finished_at,
            run_id,
        ),
    )


def main(
    *,
    decisions_path: str,
    source_queue_path: str,
    policy_run_id: int | None = None,
    reviewer: str | None = None,
) -> dict[str, Any]:
    settings = db.load_settings()
    decisions_file = db.project_path(decisions_path)
    source_queue_file = db.project_path(source_queue_path)
    if not decisions_file.exists():
        raise FileNotFoundError(decisions_file)
    if not source_queue_file.exists():
        raise FileNotFoundError(source_queue_file)

    decision_rows = load_decision_rows(decisions_file)
    queue_by_item = load_queue_rows(source_queue_file)
    started_at = datetime.now()
    started_at_db = started_at.isoformat(timespec="seconds")
    counts: Counter = Counter()
    preview_lines: list[str] = []

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_policy_run_id = policy_run_id or latest_policy_run_id(conn)
        run_id = insert_run(
            conn,
            policy_run_id=selected_policy_run_id,
            source_queue_path=source_queue_file,
            decisions_path=decisions_file,
            started_at=started_at_db,
        )

        for index, decision_row in enumerate(decision_rows, start=1):
            policy_item_id = extract_policy_item_id(decision_row)
            label = extract_label(decision_row)
            if policy_item_id is None:
                counts["skipped"] += 1
                preview_lines.append(f"- row {index}: skipped_missing_policy_item_id")
                continue
            if label not in VALID_LABELS:
                counts["skipped"] += 1
                preview_lines.append(f"- item {policy_item_id}: skipped_invalid_evidence_label={label}")
                continue
            queue_row = queue_by_item.get(policy_item_id)
            if queue_row is None:
                counts["skipped"] += 1
                preview_lines.append(f"- item {policy_item_id}: skipped_missing_source_queue_row")
                continue
            if int(queue_row["policy_run_id"]) != selected_policy_run_id:
                counts["skipped"] += 1
                preview_lines.append(
                    f"- item {policy_item_id}: skipped_policy_run_mismatch={queue_row['policy_run_id']} expected={selected_policy_run_id}"
                )
                continue

            if reviewer and not decision_row.get("reviewer"):
                decision_row = {**decision_row, "reviewer": reviewer}
            now = db.utc_now()
            upsert_item(
                conn,
                run_id=run_id,
                queue_row=queue_row,
                decision_row=decision_row,
                evidence_label=label,
                now=now,
            )
            counts["accepted"] += 1
            counts[label] += 1
            counts[f"agent_{queue_row['split_agent']}"] += 1
            preview_lines.append(
                (
                    f"- item {policy_item_id} | {queue_row['split_agent']} | "
                    f"{queue_row['method_signature']} | {label} | {queue_row['source_key']}"
                )
            )

        report_lines = [
            "Segment token gender split evidence ingest report",
            f"Started at: {started_at.isoformat(timespec='seconds')}",
            f"Rule version: {RULE_VERSION}",
            f"Policy run id: {selected_policy_run_id}",
            f"Evidence run id: {run_id}",
            f"Decision file: {decisions_file}",
            f"Source queue: {source_queue_file}",
            "",
            "Safety contract:",
            "- Learning evidence only: yes",
            "- Writes output/source: no",
            "- Writes segment_token_policy_decisions: no",
            "- Approves apply: no",
            "",
            "Summary:",
            f"- Rows loaded: {len(decision_rows)}",
            f"- Evidence accepted: {counts['accepted']}",
            f"- Positive evidence: {counts[POSITIVE_LABEL]}",
            f"- Negative boundary: {counts[NEGATIVE_LABEL]}",
            f"- Needs more context: {counts[MORE_CONTEXT_LABEL]}",
            f"- Manual exception only: {counts[MANUAL_LABEL]}",
            f"- Skipped rows: {counts['skipped']}",
            "",
            "Agent counts:",
        ]
        for key, value in sorted(counts.items()):
            if key.startswith("agent_"):
                report_lines.append(f"- {key.removeprefix('agent_')}: {value}")
        report_lines.extend(["", "Preview:", *preview_lines[:120]])
        report_path = db.write_report(settings, "segment_token_gender_split_evidence_ingest", report_lines)

        finished_at = datetime.now().isoformat(timespec="seconds")
        update_run(conn, run_id=run_id, counts=counts, report_path=report_path, finished_at=finished_at)
        conn.commit()

    print("[segment_token_gender_split_evidence_ingest] Evidence ingested")
    print(f"[segment_token_gender_split_evidence_ingest] Rule version: {RULE_VERSION}")
    print(f"[segment_token_gender_split_evidence_ingest] Policy run id: {selected_policy_run_id}")
    print(f"[segment_token_gender_split_evidence_ingest] Evidence run id: {run_id}")
    print(f"[segment_token_gender_split_evidence_ingest] Rows loaded: {len(decision_rows)}")
    print(f"[segment_token_gender_split_evidence_ingest] Evidence accepted: {counts['accepted']}")
    print(f"[segment_token_gender_split_evidence_ingest] Positive evidence: {counts[POSITIVE_LABEL]}")
    print(f"[segment_token_gender_split_evidence_ingest] Negative boundary: {counts[NEGATIVE_LABEL]}")
    print(f"[segment_token_gender_split_evidence_ingest] Manual exception only: {counts[MANUAL_LABEL]}")
    print(f"[segment_token_gender_split_evidence_ingest] Skipped rows: {counts['skipped']}")
    print(f"[segment_token_gender_split_evidence_ingest] Report: {report_path}")
    return {
        "run_id": run_id,
        "report_path": str(report_path),
        "accepted": counts["accepted"],
        "positive": counts[POSITIVE_LABEL],
        "negative": counts[NEGATIVE_LABEL],
        "manual_exception": counts[MANUAL_LABEL],
        "skipped": counts["skipped"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest learning-only evidence labels for gender split agents.")
    parser.add_argument("decisions_path")
    parser.add_argument("--source-queue", required=True)
    parser.add_argument("--policy-run-id", type=int, default=None)
    parser.add_argument("--reviewer", default=None)
    args = parser.parse_args()
    main(
        decisions_path=args.decisions_path,
        source_queue_path=args.source_queue,
        policy_run_id=args.policy_run_id,
        reviewer=args.reviewer,
    )
