from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "segment_token_gender_split_promotion_audit_v1"


def classify_agent(row: dict[str, Any], *, min_positive: int) -> tuple[str, str]:
    positive = int(row["positive_count"] or 0)
    negative = int(row["negative_count"] or 0)
    manual = int(row["manual_exception_count"] or 0)
    needs_more = int(row["needs_more_context_count"] or 0)
    if negative > 0:
        return (
            "split_required_negative_boundary",
            "Do not promote this agent yet; split narrower or collect counterexamples before guarded policy.",
        )
    if manual > 0:
        return (
            "split_required_manual_boundary",
            "Keep manual exceptions out of automatic policy; split the positive subset before promotion.",
        )
    if needs_more > 0:
        return (
            "needs_review_completion",
            "Finish unresolved evidence labels before promotion.",
        )
    if positive >= min_positive:
        return (
            "ready_for_guarded_policy_review",
            "Eligible for a guarded policy draft with production still blocked behind audit and checkpoint.",
        )
    if positive > 0:
        return (
            "needs_more_positive_evidence",
            "Collect more positive examples before guarded policy.",
        )
    return (
        "needs_evidence",
        "No usable positive evidence yet.",
    )


def fetch_agent_rows(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            split_agent,
            COUNT(*) AS total_evidence,
            SUM(CASE WHEN evidence_label = 'positive_evidence' THEN 1 ELSE 0 END) AS positive_count,
            SUM(CASE WHEN evidence_label = 'negative_boundary' THEN 1 ELSE 0 END) AS negative_count,
            SUM(CASE WHEN evidence_label = 'needs_more_context' THEN 1 ELSE 0 END) AS needs_more_context_count,
            SUM(CASE WHEN evidence_label = 'manual_exception_only' THEN 1 ELSE 0 END) AS manual_exception_count,
            COUNT(DISTINCT method_signature) AS distinct_method_count,
            json_group_array(DISTINCT method_signature) AS method_signatures_json,
            json_group_array(policy_item_id) AS sample_policy_item_ids_json
        FROM segment_token_gender_split_evidence_items
        GROUP BY split_agent
        ORDER BY positive_count DESC, total_evidence DESC, split_agent
        """
    ).fetchall()
    return [dict(row) for row in rows]


def insert_run(conn, *, min_positive: int, started_at: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO segment_token_gender_split_promotion_audit_runs (
            rule_version,
            min_positive,
            started_at,
            updated_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (RULE_VERSION, min_positive, started_at, started_at),
    )
    return int(cur.lastrowid)


def insert_items(conn, *, run_id: int, rows: list[dict[str, Any]], now: str) -> None:
    for row in rows:
        conn.execute(
            """
            INSERT INTO segment_token_gender_split_promotion_audit_items (
                run_id,
                split_agent,
                total_evidence,
                positive_count,
                negative_count,
                needs_more_context_count,
                manual_exception_count,
                distinct_method_count,
                promotion_status,
                recommended_action,
                method_signatures_json,
                sample_policy_item_ids_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                row["split_agent"],
                row["total_evidence"],
                row["positive_count"],
                row["negative_count"],
                row["needs_more_context_count"],
                row["manual_exception_count"],
                row["distinct_method_count"],
                row["promotion_status"],
                row["recommended_action"],
                row.get("method_signatures_json"),
                row.get("sample_policy_item_ids_json"),
                now,
            ),
        )


def write_outputs(
    settings: dict[str, Any],
    *,
    run_id: int,
    rows: list[dict[str, Any]],
    min_positive: int,
    started_at: datetime,
) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{timestamp}_segment_token_gender_split_promotion_audit"
    txt_path = base.with_suffix(".txt")
    csv_path = base.with_suffix(".csv")
    jsonl_path = base.with_suffix(".jsonl")

    fieldnames = [
        "split_agent",
        "promotion_status",
        "recommended_action",
        "total_evidence",
        "positive_count",
        "negative_count",
        "manual_exception_count",
        "needs_more_context_count",
        "distinct_method_count",
        "method_signatures_json",
        "sample_policy_item_ids_json",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    status_counts = Counter(row["promotion_status"] for row in rows)
    lines = [
        "Segment token gender split promotion audit",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Audit run id: {run_id}",
        f"Minimum positives: {min_positive}",
        "",
        "Safety contract:",
        "- This audit does not promote into production.",
        "- Ready means only ready for a guarded policy draft/review.",
        "- Output/source remains untouched.",
        "",
        "Summary:",
        f"- Agents audited: {len(rows)}",
        *[f"- {key}: {value}" for key, value in status_counts.most_common()],
        "",
        "Agents:",
    ]
    for row in rows:
        methods = json.loads(row["method_signatures_json"] or "[]")
        samples = json.loads(row["sample_policy_item_ids_json"] or "[]")
        lines.extend(
            [
                f"- {row['split_agent']}: {row['promotion_status']}",
                (
                    f"  evidence total/positive/negative/manual/more_context: "
                    f"{row['total_evidence']}/{row['positive_count']}/{row['negative_count']}/"
                    f"{row['manual_exception_count']}/{row['needs_more_context_count']}"
                ),
                f"  distinct methods: {row['distinct_method_count']}",
                f"  action: {row['recommended_action']}",
                f"  methods: {', '.join(str(item) for item in methods[:8])}",
                f"  sample policy items: {', '.join(str(item) for item in samples[:12])}",
            ]
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path


def update_run(
    conn,
    *,
    run_id: int,
    rows: list[dict[str, Any]],
    report_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    finished_at: str,
) -> None:
    status_counts = Counter(row["promotion_status"] for row in rows)
    conn.execute(
        """
        UPDATE segment_token_gender_split_promotion_audit_runs
        SET
            total_agents = ?,
            ready_count = ?,
            needs_more_evidence_count = ?,
            boundary_count = ?,
            report_path = ?,
            csv_path = ?,
            jsonl_path = ?,
            finished_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            len(rows),
            status_counts["ready_for_guarded_policy_review"],
            status_counts["needs_more_positive_evidence"] + status_counts["needs_evidence"] + status_counts["needs_review_completion"],
            status_counts["split_required_negative_boundary"] + status_counts["split_required_manual_boundary"],
            str(report_path),
            str(csv_path),
            str(jsonl_path),
            finished_at,
            finished_at,
            run_id,
        ),
    )


def main(*, min_positive: int = 3) -> dict[str, Any]:
    if min_positive <= 0:
        raise RuntimeError("min_positive must be positive.")
    settings = db.load_settings()
    started_at = datetime.now()
    started_at_db = started_at.isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        run_id = insert_run(conn, min_positive=min_positive, started_at=started_at_db)
        rows = fetch_agent_rows(conn)
        enriched_rows: list[dict[str, Any]] = []
        for row in rows:
            status, action = classify_agent(row, min_positive=min_positive)
            enriched_rows.append({**row, "promotion_status": status, "recommended_action": action})
        now = db.utc_now()
        insert_items(conn, run_id=run_id, rows=enriched_rows, now=now)
        report_path, csv_path, jsonl_path = write_outputs(
            settings,
            run_id=run_id,
            rows=enriched_rows,
            min_positive=min_positive,
            started_at=started_at,
        )
        finished_at = datetime.now().isoformat(timespec="seconds")
        update_run(
            conn,
            run_id=run_id,
            rows=enriched_rows,
            report_path=report_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            finished_at=finished_at,
        )
        conn.commit()

    status_counts = Counter(row["promotion_status"] for row in enriched_rows)
    print("[segment_token_gender_split_promotion_audit] Audit generated")
    print(f"[segment_token_gender_split_promotion_audit] Rule version: {RULE_VERSION}")
    print(f"[segment_token_gender_split_promotion_audit] Audit run id: {run_id}")
    print(f"[segment_token_gender_split_promotion_audit] Agents audited: {len(enriched_rows)}")
    for key, value in status_counts.most_common():
        print(f"[segment_token_gender_split_promotion_audit] {key}: {value}")
    print(f"[segment_token_gender_split_promotion_audit] Report: {report_path}")
    print(f"[segment_token_gender_split_promotion_audit] CSV: {csv_path}")
    print(f"[segment_token_gender_split_promotion_audit] JSONL: {jsonl_path}")
    return {
        "run_id": run_id,
        "report_path": str(report_path),
        "rows": len(enriched_rows),
        "status_counts": dict(status_counts),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit promotion readiness for gender split evidence agents.")
    parser.add_argument("--min-positive", type=int, default=3)
    args = parser.parse_args()
    main(min_positive=args.min_positive)
