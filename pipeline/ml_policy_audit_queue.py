from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "ml_policy_audit_queue_v1"
FOCUS_VALUES = {"all", "new_safe", "demoted_safe"}


def percent(part: int, total: int) -> str:
    if total == 0:
        return "0.00%"
    return f"{part / total:.2%}"


def latest_policy_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_policy_runs
        WHERE scored_count > 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No completed ml_policy_runs found. Run ml-group-threshold-policy first.")
    return int(row["id"])


def load_policy_run(conn, policy_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_policy_runs
        WHERE id = ?
        """,
        (policy_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No ml_policy_runs row found for id {policy_run_id}.")
    return dict(row)


def parse_groups(value: str | None) -> set[str]:
    if not value:
        return set()
    return {chunk.strip() for chunk in value.split(",") if chunk.strip()}


def focus_clause(focus: str) -> str:
    if focus == "new_safe":
        return "mpi.new_safe = 1"
    if focus == "demoted_safe":
        return "mpi.demoted_safe = 1"
    return "(mpi.new_safe = 1 OR mpi.demoted_safe = 1)"


def audit_kind(row: dict[str, Any]) -> str:
    if int(row.get("new_safe") or 0):
        return "new_safe"
    if int(row.get("demoted_safe") or 0):
        return "demoted_safe"
    return "policy_review"


def fetch_rows(
    conn,
    policy_run_id: int,
    focus: str,
    groups: set[str],
    limit: int,
) -> list[dict[str, Any]]:
    if focus == "all" and limit > 1:
        new_limit = max(1, limit // 2)
        demoted_limit = max(1, limit - new_limit)
        return fetch_rows(conn, policy_run_id, "new_safe", groups, new_limit) + fetch_rows(
            conn,
            policy_run_id,
            "demoted_safe",
            groups,
            demoted_limit,
        )
    where = [f"mpi.run_id = ?", focus_clause(focus)]
    params: list[Any] = [policy_run_id]
    if groups:
        placeholders = ",".join("?" for _ in groups)
        where.append(f"mpi.policy_group IN ({placeholders})")
        params.extend(sorted(groups))
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT
            mpi.*,
            s.english_text,
            s.spanish_text,
            s.old_text,
            s.source_line_number,
            msi.candidate_text,
            msi.reasons_json AS score_reasons_json,
            msi.issues_json AS score_issues_json,
            o.portuguese_text AS output_text
        FROM ml_policy_items mpi
        JOIN source_segments s ON s.id = mpi.segment_id
        JOIN ml_score_items msi ON msi.id = mpi.score_item_id
        LEFT JOIN output_segments o ON o.segment_id = mpi.segment_id
        WHERE {" AND ".join(where)}
        ORDER BY
            CASE
                WHEN mpi.new_safe = 1 THEN 0
                WHEN mpi.demoted_safe = 1 THEN 1
                ELSE 2
            END,
            mpi.policy_group,
            mpi.learned_positive DESC,
            mpi.model_safe_probability DESC,
            mpi.segment_id
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    result = [dict(row) for row in rows]
    for row in result:
        row["audit_kind"] = audit_kind(row)
    return result


def group_summary(conn, policy_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            policy_group,
            COUNT(*) AS total_review_targets,
            SUM(new_safe) AS new_safe,
            SUM(demoted_safe) AS demoted_safe,
            SUM(CASE WHEN new_safe = 1 AND learned_positive = 1 THEN 1 ELSE 0 END) AS new_safe_learned_positive,
            SUM(CASE WHEN learned_negative = 1 THEN 1 ELSE 0 END) AS learned_negative
        FROM ml_policy_items
        WHERE run_id = ?
          AND (new_safe = 1 OR demoted_safe = 1)
        GROUP BY policy_group
        ORDER BY new_safe DESC, demoted_safe DESC, policy_group
        """,
        (policy_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def write_csv(settings: dict, rows: list[dict[str, Any]]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = reports_dir / f"{timestamp}_ml_policy_audit_queue.csv"
    fieldnames = [
        "audit_kind",
        "policy_group",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "model_safe_probability",
        "policy_threshold",
        "policy_require_learned_positive",
        "score_final_action",
        "policy_action",
        "new_safe",
        "demoted_safe",
        "learned_positive",
        "learned_negative",
        "english_text",
        "spanish_text",
        "old_text",
        "output_text",
        "candidate_text",
        "policy_reasons_json",
        "review_decision",
        "review_notes",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "audit_kind": row["audit_kind"],
                    "policy_group": row["policy_group"],
                    "segment_id": row["segment_id"],
                    "relative_path": row["relative_path"],
                    "source_key": row["source_key"],
                    "source_line_number": row["source_line_number"],
                    "model_safe_probability": row.get("model_safe_probability"),
                    "policy_threshold": row["policy_threshold"],
                    "policy_require_learned_positive": row["policy_require_learned_positive"],
                    "score_final_action": row["score_final_action"],
                    "policy_action": row["policy_action"],
                    "new_safe": row["new_safe"],
                    "demoted_safe": row["demoted_safe"],
                    "learned_positive": row["learned_positive"],
                    "learned_negative": row["learned_negative"],
                    "english_text": row.get("english_text") or "",
                    "spanish_text": row.get("spanish_text") or "",
                    "old_text": row.get("old_text") or "",
                    "output_text": row.get("output_text") or "",
                    "candidate_text": row.get("candidate_text") or "",
                    "policy_reasons_json": row.get("reasons_json") or "",
                    "review_decision": "",
                    "review_notes": "",
                }
            )
    return path


def sample_lines(rows: list[dict[str, Any]], limit: int) -> list[str]:
    if not rows:
        return ["- none"]
    lines = []
    for row in rows[:limit]:
        lines.append(
            f"- {row['audit_kind']} | {row['policy_group']} | "
            f"safe_prob={float(row.get('model_safe_probability') or 0.0):.4f} | "
            f"{row['relative_path']}::{row['source_key']} | candidate=\"{row.get('candidate_text') or ''}\""
        )
    return lines


def main(
    policy_run_id: int | None = None,
    focus: str = "all",
    groups_value: str | None = None,
    limit: int = 200,
    sample_limit: int = 60,
) -> None:
    if focus not in FOCUS_VALUES:
        raise ValueError(f"Invalid focus {focus!r}. Expected one of: {sorted(FOCUS_VALUES)}")
    settings = db.load_settings()
    started_at = datetime.now()
    groups = parse_groups(groups_value)
    print("[ml_policy_audit_queue] Starting policy audit queue")
    print(f"[ml_policy_audit_queue] Rule version: {RULE_VERSION}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        policy_run_id = policy_run_id or latest_policy_run_id(conn)
        policy_run = load_policy_run(conn, policy_run_id)
        rows = fetch_rows(conn, policy_run_id, focus, groups, limit)
        summaries = group_summary(conn, policy_run_id)

    csv_path = write_csv(settings, rows)
    counts = Counter(row["audit_kind"] for row in rows)
    elapsed = datetime.now() - started_at
    report_lines = [
        "ML policy audit queue",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Policy run id: {policy_run_id}",
        f"Policy rule version: {policy_run['rule_version']}",
        f"Score run id: {policy_run['score_run_id']}",
        f"Model version: {policy_run['model_version']}",
        f"Focus: {focus}",
        f"Groups filter: {', '.join(sorted(groups)) if groups else 'all'}",
        f"Limit: {limit}",
        f"CSV: {csv_path}",
        "",
        "Queue summary:",
        f"- rows: {len(rows)}",
        f"- new_safe: {counts['new_safe']}",
        f"- demoted_safe: {counts['demoted_safe']}",
        "",
        "Policy run summary:",
        f"- scored: {policy_run['scored_count']}",
        f"- active auto safe: {policy_run['active_auto_safe_count']} ({percent(int(policy_run['active_auto_safe_count'] or 0), int(policy_run['scored_count'] or 0))})",
        f"- policy auto safe: {policy_run['policy_auto_safe_count']} ({percent(int(policy_run['policy_auto_safe_count'] or 0), int(policy_run['scored_count'] or 0))})",
        f"- new safe: {policy_run['new_safe_count']} ({percent(int(policy_run['new_safe_count'] or 0), int(policy_run['scored_count'] or 0))})",
        f"- active safe below stricter policy: {policy_run['demoted_safe_count']}",
        "",
        "Review targets by group:",
    ]
    for summary in summaries:
        report_lines.extend(
            [
                f"- {summary['policy_group']}:",
                f"  - total review targets: {summary['total_review_targets']}",
                f"  - new safe: {summary['new_safe']}",
                f"  - demoted safe: {summary['demoted_safe']}",
                f"  - new safe learned positive: {summary['new_safe_learned_positive']}",
                f"  - learned negative: {summary['learned_negative']}",
            ]
        )
    report_lines.extend(
        [
            "",
            f"Top {min(sample_limit, len(rows))} queue rows:",
            *sample_lines(rows, sample_limit),
            "",
            "Interpretation:",
            "- new_safe rows are coverage gained by the group policy.",
            "- demoted_safe rows are active-safe rows that would fail a stricter policy, but are protected while protect_active_safe is enabled.",
            "- This queue is for audit and learning only; it does not apply output changes.",
        ]
    )
    report_path = db.write_report(settings, "ml_policy_audit_queue", report_lines)
    print(f"[ml_policy_audit_queue] Rows: {len(rows)}")
    print(f"[ml_policy_audit_queue] New safe: {counts['new_safe']}")
    print(f"[ml_policy_audit_queue] Demoted safe: {counts['demoted_safe']}")
    print(f"[ml_policy_audit_queue] CSV: {csv_path}")
    print(f"[ml_policy_audit_queue] Report: {report_path}")
    print("[ml_policy_audit_queue] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build an audit queue from materialized ML group policy decisions.")
    parser.add_argument("--policy-run-id", type=int, default=None)
    parser.add_argument("--focus", choices=sorted(FOCUS_VALUES), default="all")
    parser.add_argument("--groups", default=None)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--sample-limit", type=int, default=60)
    args = parser.parse_args()
    main(
        policy_run_id=args.policy_run_id,
        focus=args.focus,
        groups_value=args.groups,
        limit=args.limit,
        sample_limit=args.sample_limit,
    )
