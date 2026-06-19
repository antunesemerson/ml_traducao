from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "auto_confirmation_reopen_text_specialist_audit_v1"


def latest_diagnostic_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM auto_confirmation_reopen_text_diagnostic_runs
        WHERE finished_at IS NOT NULL
          AND total_items > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No text diagnostic run found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_auto_confirmation_reopen_text_specialist_audit"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_scope_rows(conn, *, diagnostic_run_ids: list[int]) -> list[dict[str, Any]]:
    placeholders = ", ".join("?" for _ in diagnostic_run_ids)
    rows = conn.execute(
        f"""
        WITH diagnostic AS (
            SELECT
                suggested_agent_key AS agent_key,
                text_subfamily,
                COUNT(*) AS diagnostic_count
            FROM auto_confirmation_reopen_text_diagnostic_items
            WHERE run_id IN ({placeholders})
            GROUP BY suggested_agent_key, text_subfamily
        ),
        queued AS (
            SELECT
                suggested_agent_key AS agent_key,
                text_subfamily,
                COUNT(*) AS queued_count
            FROM auto_confirmation_reopen_text_review_queue_items
            WHERE diagnostic_run_id IN ({placeholders})
            GROUP BY suggested_agent_key, text_subfamily
        ),
        reviewed AS (
            SELECT
                agent_key,
                text_subfamily,
                COUNT(*) AS reviewed_count,
                SUM(CASE WHEN evidence_label = 'positive_evidence' THEN 1 ELSE 0 END) AS positive_count,
                SUM(CASE WHEN evidence_label = 'negative_boundary' THEN 1 ELSE 0 END) AS negative_count,
                SUM(CASE WHEN evidence_label = 'needs_more_context' THEN 1 ELSE 0 END) AS needs_more_context_count,
                SUM(CASE WHEN evidence_label = 'manual_exception_only' THEN 1 ELSE 0 END) AS manual_exception_count,
                SUM(CASE WHEN risk_level = 'high' THEN 1 ELSE 0 END) AS high_risk_reviewed_count
            FROM auto_confirmation_reopen_text_review_decisions
            WHERE diagnostic_run_id IN ({placeholders})
            GROUP BY agent_key, text_subfamily
        )
        SELECT
            diagnostic.agent_key,
            diagnostic.text_subfamily,
            diagnostic.diagnostic_count,
            COALESCE(queued.queued_count, 0) AS queued_count,
            COALESCE(reviewed.reviewed_count, 0) AS reviewed_count,
            COALESCE(reviewed.positive_count, 0) AS positive_count,
            COALESCE(reviewed.negative_count, 0) AS negative_count,
            COALESCE(reviewed.needs_more_context_count, 0) AS needs_more_context_count,
            COALESCE(reviewed.manual_exception_count, 0) AS manual_exception_count,
            COALESCE(reviewed.high_risk_reviewed_count, 0) AS high_risk_reviewed_count
        FROM diagnostic
        LEFT JOIN queued
          ON queued.agent_key = diagnostic.agent_key
         AND queued.text_subfamily = diagnostic.text_subfamily
        LEFT JOIN reviewed
          ON reviewed.agent_key = diagnostic.agent_key
         AND reviewed.text_subfamily = diagnostic.text_subfamily
        ORDER BY diagnostic.diagnostic_count DESC, diagnostic.agent_key
        """,
        tuple(diagnostic_run_ids) * 3,
    ).fetchall()
    return [dict(row) for row in rows]


def classify_maturity(
    row: dict[str, Any],
    *,
    min_reviewed: int,
    max_negative_rate_for_release: float,
) -> tuple[str, str, list[str], float]:
    reviewed = int(row["reviewed_count"] or 0)
    positive = int(row["positive_count"] or 0)
    negative = int(row["negative_count"] or 0)
    more_context = int(row["needs_more_context_count"] or 0)
    manual = int(row["manual_exception_count"] or 0)
    diagnostic = int(row["diagnostic_count"] or 0)
    negative_rate = negative / reviewed if reviewed else 0.0
    reasons: list[str] = []
    if reviewed == 0:
        reasons.append("no_review_evidence")
        return "no_evidence", "generate_or_review_queue", reasons, negative_rate
    if negative > 0 or more_context > 0:
        reasons.append("negative_or_context_boundary_seen")
        if negative_rate >= max_negative_rate_for_release:
            reasons.append("negative_rate_above_release_threshold")
        return "blocked_boundary", "split_or_collect_more_negatives_before_policy", reasons, negative_rate
    if manual > 0 and positive == 0:
        reasons.append("manual_exceptions_only")
        return "manual_only", "keep_as_manual_exception_boundary", reasons, negative_rate
    if reviewed < min_reviewed:
        reasons.append("insufficient_review_count")
        return "candidate_more_evidence", "review_more_examples", reasons, negative_rate
    if positive >= min_reviewed and negative_rate <= max_negative_rate_for_release:
        reasons.append("positive_evidence_meets_minimum")
        if diagnostic > reviewed:
            reasons.append("shadow_only_remaining_unreviewed")
        return "ready_for_shadow", "create_shadow_policy_dry_run", reasons, negative_rate
    reasons.append("mixed_evidence_needs_more_review")
    return "candidate_more_evidence", "review_more_examples", reasons, negative_rate


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    audit_run_id: int,
    rows: list[dict[str, Any]],
    diagnostic_run_ids: list[int],
    min_reviewed: int,
    max_negative_rate_for_release: float,
    started_at: datetime,
) -> None:
    fieldnames = [
        "agent_key",
        "text_subfamily",
        "diagnostic_count",
        "queued_count",
        "reviewed_count",
        "positive_count",
        "negative_count",
        "needs_more_context_count",
        "manual_exception_count",
        "high_risk_reviewed_count",
        "negative_rate",
        "maturity_status",
        "recommended_action",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    by_status = Counter(row["maturity_status"] for row in rows)
    lines = [
        "Auto-confirmation text specialist audit",
        f"Rule version: {RULE_VERSION}",
        f"Audit run id: {audit_run_id}",
        f"Diagnostic run ids: {', '.join(str(item) for item in diagnostic_run_ids)}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Thresholds:",
        f"- Min reviewed for shadow: {min_reviewed}",
        f"- Max negative rate for release: {max_negative_rate_for_release:.2%}",
        "",
        "Summary:",
        f"- Agents/subfamilies inspected: {len(rows):,}",
        *[f"- {key}: {value:,}" for key, value in by_status.most_common()],
        "",
        "Rows:",
    ]
    for row in rows:
        lines.append(
            "- {agent_key}/{text_subfamily}: diag={diagnostic_count}, queued={queued_count}, "
            "reviewed={reviewed_count}, pos={positive_count}, neg={negative_count}, context={needs_more_context_count}, "
            "manual={manual_exception_count}, status={maturity_status}, action={recommended_action}".format(**row)
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This audit measures specialist maturity only.",
            "- It does not change confirmations, train models, promote policies, or write output files.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    diagnostic_run_id: int | list[int] | None = None,
    min_reviewed: int = 20,
    max_negative_rate_for_release: float = 0.02,
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    now = started_at.isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        if diagnostic_run_id is None:
            selected_diagnostic_run_ids = [latest_diagnostic_run_id(conn)]
        elif isinstance(diagnostic_run_id, int):
            selected_diagnostic_run_ids = [diagnostic_run_id]
        else:
            selected_diagnostic_run_ids = list(dict.fromkeys(int(item) for item in diagnostic_run_id))
        rows = fetch_scope_rows(conn, diagnostic_run_ids=selected_diagnostic_run_ids)
        for row in rows:
            status, action, reasons, negative_rate = classify_maturity(
                row,
                min_reviewed=min_reviewed,
                max_negative_rate_for_release=max_negative_rate_for_release,
            )
            row["negative_rate"] = negative_rate
            row["maturity_status"] = status
            row["recommended_action"] = action
            row["reasons"] = reasons
        by_status = Counter(row["maturity_status"] for row in rows)
        txt_path, csv_path, jsonl_path = report_paths(settings)
        cursor = conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_text_specialist_audit_runs (
                rule_version,
                min_reviewed,
                max_negative_rate_for_release,
                total_agents,
                no_evidence_count,
                candidate_count,
                blocked_count,
                ready_for_shadow_count,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                min_reviewed,
                max_negative_rate_for_release,
                len(rows),
                by_status["no_evidence"],
                by_status["candidate_more_evidence"] + by_status["manual_only"],
                by_status["blocked_boundary"],
                by_status["ready_for_shadow"],
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                now,
                now,
                now,
            ),
        )
        audit_run_id = int(cursor.lastrowid)
        for row in rows:
            conn.execute(
                """
                INSERT INTO auto_confirmation_reopen_text_specialist_audit_items (
                    run_id,
                    agent_key,
                    text_subfamily,
                    diagnostic_count,
                    queued_count,
                    reviewed_count,
                    positive_count,
                    negative_count,
                    needs_more_context_count,
                    manual_exception_count,
                    high_risk_reviewed_count,
                    negative_rate,
                    maturity_status,
                    recommended_action,
                    reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_run_id,
                    row["agent_key"],
                    row["text_subfamily"],
                    int(row["diagnostic_count"] or 0),
                    int(row["queued_count"] or 0),
                    int(row["reviewed_count"] or 0),
                    int(row["positive_count"] or 0),
                    int(row["negative_count"] or 0),
                    int(row["needs_more_context_count"] or 0),
                    int(row["manual_exception_count"] or 0),
                    int(row["high_risk_reviewed_count"] or 0),
                    float(row["negative_rate"] or 0),
                    row["maturity_status"],
                    row["recommended_action"],
                    json.dumps(row["reasons"], ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            audit_run_id=audit_run_id,
            rows=rows,
            diagnostic_run_ids=selected_diagnostic_run_ids,
            min_reviewed=min_reviewed,
            max_negative_rate_for_release=max_negative_rate_for_release,
            started_at=started_at,
        )
        conn.commit()

    print("[auto_confirmation_reopen_text_specialist_audit] Audit generated")
    print(f"[auto_confirmation_reopen_text_specialist_audit] Run id: {audit_run_id}")
    print(f"[auto_confirmation_reopen_text_specialist_audit] Agents/subfamilies: {len(rows):,}")
    for key, value in by_status.most_common():
        print(f"[auto_confirmation_reopen_text_specialist_audit] {key}: {value:,}")
    print(f"[auto_confirmation_reopen_text_specialist_audit] Report: {txt_path}")
    return {
        "run_id": audit_run_id,
        "rows": len(rows),
        "by_status": dict(by_status),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit maturity of text-replacement specialists.")
    parser.add_argument("--diagnostic-run-id", type=int, action="append", default=None)
    parser.add_argument("--min-reviewed", type=int, default=20)
    parser.add_argument("--max-negative-rate-for-release", type=float, default=0.02)
    args = parser.parse_args()
    main(
        diagnostic_run_id=args.diagnostic_run_id,
        min_reviewed=args.min_reviewed,
        max_negative_rate_for_release=args.max_negative_rate_for_release,
    )
