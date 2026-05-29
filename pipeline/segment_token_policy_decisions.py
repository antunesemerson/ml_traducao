from __future__ import annotations

import argparse
import csv
import json
import hashlib
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "segment_token_policy_decisions_v1"

VALID_DECISIONS = {
    "accept_policy_candidate",
    "keep_manual_exception_only",
    "reject_policy_candidate",
    "fix_confirmed_text",
    "encoding_cleanup_required",
    "manual_token_rewrite_required",
    "needs_subpolicy",
}
APPLY_DECISIONS = {"accept_policy_candidate", "keep_manual_exception_only"}
FIX_DECISIONS = {"fix_confirmed_text", "encoding_cleanup_required", "manual_token_rewrite_required"}
REJECT_DECISIONS = {"reject_policy_candidate", "needs_subpolicy"}
BLOCKED_BUCKETS = {"blocked_suspicious_confirmed_text", "blocked_variable_or_icon_change"}


def sha256_text(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def utc_now() -> str:
    return db.utc_now()


def short(value: str | None, limit: int = 160) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def latest_policy_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_token_policy_runs
        WHERE finished_at IS NOT NULL
          AND total_candidates > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No complete segment_token_policy_runs entry found.")
    return int(row["id"])


def load_decision_rows(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        rows = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
            if not line.strip():
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError(f"JSONL line {line_number} is not an object.")
            rows.append(payload)
        return rows
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            candidates = payload.get("candidates")
            if isinstance(candidates, list):
                return [row for row in candidates if isinstance(row, dict)]
            batches = payload.get("batches")
            if isinstance(batches, list):
                rows = []
                for batch in batches:
                    if isinstance(batch, dict) and isinstance(batch.get("candidates"), list):
                        rows.extend(row for row in batch["candidates"] if isinstance(row, dict))
                return rows
        raise ValueError("JSON decisions must be a list, a {candidates: [...]} object, or batches with candidates.")
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    raise ValueError(f"Unsupported decision file extension: {path.suffix}")


def extract_decision(row: dict[str, Any]) -> str | None:
    for key in ("decision", "review_decision", "human_decision", "review_label", "label"):
        value = row.get(key)
        if value:
            return str(value).strip()
    return None


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


def parse_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return [value]
    if isinstance(payload, list):
        return [str(item) for item in payload]
    return [str(payload)]


def fetch_policy_item(conn, *, policy_run_id: int, policy_item_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
            i.id AS policy_item_id,
            i.run_id AS policy_run_id,
            i.state_run_id,
            i.segment_id,
            i.relative_path,
            i.source_key,
            i.source_line_number,
            i.review_state,
            i.diff_kind,
            i.policy_bucket,
            i.risk_level,
            i.recommendation,
            i.auto_apply_allowed,
            i.needs_human_review,
            i.missing_tokens_json,
            i.extra_tokens_json,
            i.issue_flags_json,
            o.portuguese_text AS output_text,
            sc.confirmed_text
        FROM segment_token_policy_items i
        LEFT JOIN output_segments o ON o.segment_id = i.segment_id
        LEFT JOIN segment_confirmations sc ON sc.segment_id = i.segment_id
        WHERE i.run_id = ?
          AND i.id = ?
        LIMIT 1
        """,
        (policy_run_id, policy_item_id),
    ).fetchone()
    return dict(row) if row else None


def is_apply_approved(policy_item: dict[str, Any], decision: str) -> tuple[int, list[str]]:
    reasons: list[str] = []
    flags = set(parse_json_list(policy_item.get("issue_flags_json")))
    if decision not in APPLY_DECISIONS:
        reasons.append("decision_not_apply_approval")
        return 0, reasons
    if policy_item["policy_bucket"] in BLOCKED_BUCKETS:
        reasons.append("blocked_bucket_cannot_be_apply_approved")
    if policy_item["risk_level"] == "critical":
        reasons.append("critical_risk_cannot_be_apply_approved")
    if "confirmed_text_suspicious_mojibake" in flags:
        reasons.append("suspicious_confirmed_text_cannot_be_apply_approved")
    if reasons:
        return 0, reasons
    reasons.append("approved_manual_token_policy_exception")
    return 1, reasons


def insert_decision_run(
    conn,
    *,
    policy_run_id: int,
    source_report: str | None,
    decisions_path: Path,
    started_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO segment_token_policy_decision_runs (
            rule_version,
            policy_run_id,
            source_report,
            decisions_path,
            started_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (RULE_VERSION, policy_run_id, source_report, str(decisions_path), started_at, started_at),
    )
    return int(cur.lastrowid)


def upsert_decision(
    conn,
    *,
    run_id: int,
    policy_item: dict[str, Any],
    decision: str,
    approved_for_apply: int,
    corrected_text: str | None,
    notes: str | None,
    reviewer: str | None,
    reasons: list[str],
    now: str,
) -> None:
    reasons_json = json.dumps(
        {
            "rule_version": RULE_VERSION,
            "approval_reasons": reasons,
            "issue_flags": parse_json_list(policy_item.get("issue_flags_json")),
            "missing_tokens": parse_json_list(policy_item.get("missing_tokens_json")),
            "extra_tokens": parse_json_list(policy_item.get("extra_tokens_json")),
        },
        ensure_ascii=False,
    )
    conn.execute(
        """
        INSERT INTO segment_token_policy_decisions (
            run_id,
            policy_run_id,
            policy_item_id,
            segment_id,
            relative_path,
            source_key,
            policy_bucket,
            risk_level,
            decision,
            approved_for_apply,
            corrected_text,
            notes,
            reviewer,
            confirmed_text_hash,
            output_text_hash,
            reasons_json,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(policy_run_id, policy_item_id) DO UPDATE SET
            run_id = excluded.run_id,
            decision = excluded.decision,
            approved_for_apply = excluded.approved_for_apply,
            corrected_text = excluded.corrected_text,
            notes = excluded.notes,
            reviewer = excluded.reviewer,
            confirmed_text_hash = excluded.confirmed_text_hash,
            output_text_hash = excluded.output_text_hash,
            reasons_json = excluded.reasons_json,
            updated_at = excluded.updated_at
        """,
        (
            run_id,
            policy_item["policy_run_id"],
            policy_item["policy_item_id"],
            policy_item["segment_id"],
            policy_item["relative_path"],
            policy_item["source_key"],
            policy_item["policy_bucket"],
            policy_item["risk_level"],
            decision,
            approved_for_apply,
            corrected_text,
            notes,
            reviewer,
            sha256_text(policy_item.get("confirmed_text")),
            sha256_text(policy_item.get("output_text")),
            reasons_json,
            now,
            now,
        ),
    )


def update_decision_run(
    conn,
    *,
    run_id: int,
    counts: Counter,
    report_path: Path,
    finished_at: str,
) -> None:
    conn.execute(
        """
        UPDATE segment_token_policy_decision_runs
        SET
            total_decisions = ?,
            approved_count = ?,
            rejected_count = ?,
            fix_count = ?,
            skipped_count = ?,
            report_path = ?,
            finished_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            counts["accepted"],
            counts["approved_for_apply"],
            counts["rejected"],
            counts["fix"],
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
    policy_run_id: int | None = None,
    source_report: str | None = None,
    reviewer: str | None = None,
) -> None:
    settings = db.load_settings()
    path = db.project_path(decisions_path)
    if not path.exists():
        raise FileNotFoundError(path)

    rows = load_decision_rows(path)
    started_at = datetime.now()
    started_at_db = started_at.isoformat(timespec="seconds")
    counts: Counter = Counter()
    preview_lines: list[str] = []

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_policy_run_id = policy_run_id or latest_policy_run_id(conn)
        run_id = insert_decision_run(
            conn,
            policy_run_id=selected_policy_run_id,
            source_report=source_report,
            decisions_path=path,
            started_at=started_at_db,
        )

        for index, decision_row in enumerate(rows, start=1):
            policy_item_id = extract_policy_item_id(decision_row)
            decision = extract_decision(decision_row)
            if policy_item_id is None:
                counts["skipped"] += 1
                preview_lines.append(f"- row {index}: skipped_missing_policy_item_id")
                continue
            if not decision:
                counts["skipped"] += 1
                preview_lines.append(f"- item {policy_item_id}: skipped_missing_decision")
                continue
            if decision not in VALID_DECISIONS:
                counts["skipped"] += 1
                preview_lines.append(f"- item {policy_item_id}: skipped_invalid_decision={decision}")
                continue

            policy_item = fetch_policy_item(conn, policy_run_id=selected_policy_run_id, policy_item_id=policy_item_id)
            if policy_item is None:
                counts["skipped"] += 1
                preview_lines.append(f"- item {policy_item_id}: skipped_not_in_policy_run_{selected_policy_run_id}")
                continue

            approved_for_apply, reasons = is_apply_approved(policy_item, decision)
            corrected_text = decision_row.get("corrected_text") or decision_row.get("proposed_text")
            notes = decision_row.get("notes") or decision_row.get("reason")
            row_reviewer = decision_row.get("reviewer") or reviewer
            now = utc_now()
            upsert_decision(
                conn,
                run_id=run_id,
                policy_item=policy_item,
                decision=decision,
                approved_for_apply=approved_for_apply,
                corrected_text=corrected_text,
                notes=notes,
                reviewer=row_reviewer,
                reasons=reasons,
                now=now,
            )

            counts["accepted"] += 1
            counts[f"decision_{decision}"] += 1
            if approved_for_apply:
                counts["approved_for_apply"] += 1
            if decision in FIX_DECISIONS:
                counts["fix"] += 1
            if decision in REJECT_DECISIONS:
                counts["rejected"] += 1
            preview_lines.append(
                "- item {policy_item_id} | segment {segment_id} | {bucket} | {risk} | {decision} | apply={apply} | {key}".format(
                    policy_item_id=policy_item_id,
                    segment_id=policy_item["segment_id"],
                    bucket=policy_item["policy_bucket"],
                    risk=policy_item["risk_level"],
                    decision=decision,
                    apply=approved_for_apply,
                    key=policy_item["source_key"],
                )
            )

        report_lines = [
            "Segment token policy decision report",
            f"Started at: {started_at.isoformat(timespec='seconds')}",
            f"Rule version: {RULE_VERSION}",
            f"Policy run id: {selected_policy_run_id}",
            f"Decision run id: {run_id}",
            f"Decision file: {path}",
            f"Source report: {source_report or 'none'}",
            "",
            "Summary:",
            f"- Rows loaded: {len(rows)}",
            f"- Decisions accepted: {counts['accepted']}",
            f"- Approved for apply: {counts['approved_for_apply']}",
            f"- Rejected/needs subpolicy: {counts['rejected']}",
            f"- Fix/manual rewrite decisions: {counts['fix']}",
            f"- Skipped rows: {counts['skipped']}",
            "",
            "Decision counts:",
        ]
        for key, value in sorted(counts.items()):
            if key.startswith("decision_"):
                report_lines.append(f"- {key.removeprefix('decision_')}: {value}")
        report_lines.extend(["", "Preview:", *preview_lines[:120]])
        report_path = db.write_report(settings, "segment_token_policy_decisions", report_lines)

        finished_at = datetime.now().isoformat(timespec="seconds")
        update_decision_run(conn, run_id=run_id, counts=counts, report_path=report_path, finished_at=finished_at)
        conn.commit()

    print("[segment_token_policy_decisions] Decisions ingested")
    print(f"[segment_token_policy_decisions] Rule version: {RULE_VERSION}")
    print(f"[segment_token_policy_decisions] Policy run id: {selected_policy_run_id}")
    print(f"[segment_token_policy_decisions] Decision run id: {run_id}")
    print(f"[segment_token_policy_decisions] Rows loaded: {len(rows)}")
    print(f"[segment_token_policy_decisions] Decisions accepted: {counts['accepted']}")
    print(f"[segment_token_policy_decisions] Approved for apply: {counts['approved_for_apply']}")
    print(f"[segment_token_policy_decisions] Skipped rows: {counts['skipped']}")
    print(f"[segment_token_policy_decisions] Report: {report_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest human decisions for segment token policy queues.")
    parser.add_argument("decisions_path")
    parser.add_argument("--policy-run-id", type=int, default=None)
    parser.add_argument("--source-report", default=None)
    parser.add_argument("--reviewer", default=None)
    args = parser.parse_args()
    main(
        decisions_path=args.decisions_path,
        policy_run_id=args.policy_run_id,
        source_report=args.source_report,
        reviewer=args.reviewer,
    )
