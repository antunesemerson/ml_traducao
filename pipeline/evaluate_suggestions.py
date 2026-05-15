from __future__ import annotations

from collections import defaultdict
from datetime import datetime

import db


RULE_VERSION = "suggestion_feedback_eval_v4"


def percent(part: int, total: int) -> str:
    if total == 0:
        return "0.00%"
    return f"{part / total:.2%}"


def scalar(conn, query: str, params: tuple = ()) -> int:
    row = conn.execute(query, params).fetchone()
    if row is None:
        return 0
    return row[0] or 0


def count_by(rows, key_name: str = "key") -> list[str]:
    lines = []
    for row in rows:
        lines.append(f"- {row[key_name] or 'unknown'}: {row['total']}")
    return lines


def main() -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[evaluate_suggestions] Starting suggestion feedback evaluation")
    print(f"[evaluate_suggestions] Rule version: {RULE_VERSION}")
    print(f"[evaluate_suggestions] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        active_segments = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM source_segments
            WHERE is_active = 1
            """,
        )
        analyzed_problem_segments = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM source_segments s
            JOIN segment_analysis a ON a.segment_id = s.id
            WHERE s.is_active = 1
              AND a.classification IN ('review_needed', 'rejected')
            """,
        )
        rows = conn.execute(
            """
            SELECT
                f.segment_id,
                f.decision,
                ts.status,
                ts.match_type,
                ts.source_language,
                ts.origin
            FROM suggestion_feedback f
            LEFT JOIN translation_suggestions ts ON ts.id = f.suggestion_id
            WHERE f.decision IN ('accepted', 'rejected', 'edited', 'accepted_old')
            """
        ).fetchall()
        decision_rows = conn.execute(
            """
            SELECT decision AS key, COUNT(*) AS total
            FROM suggestion_feedback
            GROUP BY decision
            ORDER BY total DESC, decision
            """
        ).fetchall()
        current_suggestion_rows = conn.execute(
            """
            SELECT status AS key, COUNT(*) AS total
            FROM translation_suggestions
            WHERE status != 'stale'
            GROUP BY status
            ORDER BY total DESC, status
            """
        ).fetchall()
        pending_rows = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM suggestion_feedback
            WHERE decision = 'pending'
            """,
        )
        pending_segments = scalar(
            conn,
            """
            SELECT COUNT(DISTINCT segment_id)
            FROM suggestion_feedback
            WHERE decision = 'pending'
            """,
        )
        resolved_segments = scalar(
            conn,
            """
            SELECT COUNT(DISTINCT segment_id)
            FROM suggestion_feedback
            WHERE decision IN ('accepted', 'edited', 'accepted_old')
            """,
        )
        rejected_open_segments = scalar(
            conn,
            """
            SELECT COUNT(DISTINCT r.segment_id)
            FROM suggestion_feedback r
            WHERE r.decision = 'rejected'
              AND NOT EXISTS (
                  SELECT 1
                  FROM suggestion_feedback p
                  WHERE p.segment_id = r.segment_id
                    AND p.decision IN ('accepted', 'edited', 'accepted_old')
              )
            """,
        )
        human_memory_pairs = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM translation_memory
            WHERE origin LIKE 'human_feedback_%'
            """,
        )
        human_memory_segments = scalar(
            conn,
            """
            SELECT COUNT(DISTINCT source_segment_id)
            FROM translation_memory
            WHERE origin LIKE 'human_feedback_%'
              AND source_segment_id IS NOT NULL
            """,
        )
        trusted_memory_pairs = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM translation_memory
            WHERE origin LIKE 'trusted_%'
            """,
        )
        memory_origin_rows = conn.execute(
            """
            SELECT origin AS key, COUNT(*) AS total
            FROM translation_memory
            WHERE origin LIKE 'human_feedback_%'
            GROUP BY origin
            ORDER BY total DESC, origin
            """
        ).fetchall()
        output_application = conn.execute(
            """
            WITH approved AS (
                SELECT
                    f.segment_id,
                    CASE
                        WHEN f.decision = 'edited' THEN f.corrected_text
                        WHEN f.decision = 'accepted_old' THEN s.old_text
                        ELSE f.suggested_text
                    END AS approved_text,
                    ROW_NUMBER() OVER (
                        PARTITION BY f.segment_id
                        ORDER BY f.updated_at DESC, f.id DESC
                    ) AS rn
                FROM suggestion_feedback f
                LEFT JOIN translation_suggestions ts ON ts.id = f.suggestion_id
                JOIN source_segments s ON s.id = f.segment_id
                WHERE f.decision IN ('accepted', 'edited', 'accepted_old')
            )
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN o.portuguese_text = approved.approved_text THEN 1 ELSE 0 END) AS applied,
                SUM(CASE WHEN o.portuguese_text != approved.approved_text OR o.portuguese_text IS NULL THEN 1 ELSE 0 END) AS not_applied
            FROM approved
            LEFT JOIN output_segments o ON o.segment_id = approved.segment_id
            WHERE approved.rn = 1
            """
        ).fetchone()

    total = len(rows)
    accepted = sum(1 for row in rows if row["decision"] == "accepted")
    accepted_old = sum(1 for row in rows if row["decision"] == "accepted_old")
    edited = sum(1 for row in rows if row["decision"] == "edited")
    rejected = sum(1 for row in rows if row["decision"] == "rejected")
    useful = accepted + accepted_old + edited
    distinct_reviewed_segments = len({row["segment_id"] for row in rows})
    output_total = output_application["total"] or 0
    output_applied = output_application["applied"] or 0
    output_not_applied = output_application["not_applied"] or 0

    by_status = defaultdict(lambda: {"total": 0, "useful": 0, "rejected": 0})
    by_match = defaultdict(lambda: {"total": 0, "useful": 0, "rejected": 0})
    by_origin = defaultdict(lambda: {"total": 0, "useful": 0, "rejected": 0})

    for row in rows:
        decision = row["decision"]
        for bucket, key in [
            (by_status, row["status"] or "unknown"),
            (by_match, row["match_type"] or "unknown"),
            (by_origin, row["origin"] or "unknown"),
        ]:
            bucket[key]["total"] += 1
            if decision in {"accepted", "accepted_old", "edited"}:
                bucket[key]["useful"] += 1
            elif decision == "rejected":
                bucket[key]["rejected"] += 1

    elapsed = datetime.now() - started_at
    report_lines = [
        "Suggestion feedback evaluation report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        "",
        "Summary:",
        f"- Active source segments: {active_segments}",
        f"- Problem segments currently analyzed: {analyzed_problem_segments}",
        f"- Feedback rows: {total}",
        f"- Feedback segments reviewed: {distinct_reviewed_segments}",
        f"- Accepted: {accepted}",
        f"- Accepted old text: {accepted_old}",
        f"- Edited: {edited}",
        f"- Rejected: {rejected}",
        f"- Useful precision: {useful}/{total} ({percent(useful, total)})",
        f"- Manual correction rate: {edited}/{useful} useful ({percent(edited, useful)})",
        f"- Rejection rate: {rejected}/{total} reviewed ({percent(rejected, total)})",
        "",
        "Queue and resolution:",
        f"- Pending rows: {pending_rows}",
        f"- Pending segments: {pending_segments}",
        f"- Resolved segments: {resolved_segments}",
        f"- Rejected but still open segments: {rejected_open_segments}",
        f"- Segment review coverage: {distinct_reviewed_segments}/{active_segments} ({percent(distinct_reviewed_segments, active_segments)})",
        f"- Problem segment resolved coverage: {resolved_segments}/{analyzed_problem_segments} ({percent(resolved_segments, analyzed_problem_segments)})",
        "",
        "Learning memory:",
        f"- Trusted memory pairs: {trusted_memory_pairs}",
        f"- Human feedback memory pairs: {human_memory_pairs}",
        f"- Human feedback memory segments: {human_memory_segments}",
        "",
        "Output application:",
        f"- Approved segments available: {output_total}",
        f"- Approved segments already in output: {output_applied}/{output_total} ({percent(output_applied, output_total)})",
        f"- Approved segments pending apply/reindex: {output_not_applied}",
        "",
        "Precision by suggestion status:",
    ]
    for key, stats in sorted(by_status.items()):
        report_lines.append(
            f"- {key}: {stats['useful']}/{stats['total']} useful ({percent(stats['useful'], stats['total'])})"
        )

    report_lines.extend(["", "Precision by match type:"])
    for key, stats in sorted(by_match.items()):
        report_lines.append(
            f"- {key}: {stats['useful']}/{stats['total']} useful ({percent(stats['useful'], stats['total'])})"
        )

    report_lines.extend(["", "Precision by origin:"])
    for key, stats in sorted(by_origin.items()):
        report_lines.append(
            f"- {key}: {stats['useful']}/{stats['total']} useful ({percent(stats['useful'], stats['total'])})"
        )

    report_lines.extend(["", "Feedback decision counts:"])
    report_lines.extend(count_by(decision_rows))

    report_lines.extend(["", "Current suggestion status counts:"])
    report_lines.extend(count_by(current_suggestion_rows))

    report_lines.extend(["", "Human feedback memory by origin:"])
    if memory_origin_rows:
        report_lines.extend(count_by(memory_origin_rows))
    else:
        report_lines.append("- none: 0")

    report_path = db.write_report(settings, "evaluate_suggestions", report_lines)
    print(f"[evaluate_suggestions] Feedback rows: {total}")
    print(f"[evaluate_suggestions] Useful precision: {useful}/{total} ({percent(useful, total)})")
    print(f"[evaluate_suggestions] Pending queue: {pending_rows} rows / {pending_segments} segments")
    print(f"[evaluate_suggestions] Resolved segments: {resolved_segments}")
    print(f"[evaluate_suggestions] Human feedback memory pairs: {human_memory_pairs}")
    print(
        "[evaluate_suggestions] Approved output applied: "
        f"{output_applied}/{output_total} ({percent(output_applied, output_total)})"
    )
    print(f"[evaluate_suggestions] Report: {report_path}")
    print("[evaluate_suggestions] Done")


if __name__ == "__main__":
    main()
