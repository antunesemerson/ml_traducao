from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime

import db


RULE_VERSION = "apply_api_reviews_v1"


def load_reviews(conn, min_confidence: float, include_approved: bool, limit: int | None):
    statuses = ["auto_ready"]
    if include_approved:
        statuses.append("approved")
    placeholders = ", ".join("?" for _ in statuses)
    limit_clause = "" if limit is None else "LIMIT ?"
    params: list = [*statuses, min_confidence]
    if limit is not None:
        params.append(limit)
    return conn.execute(
        f"""
        SELECT
            ar.id,
            ar.feedback_id,
            ar.segment_id,
            ar.decision_suggested,
            ar.corrected_text,
            ar.confidence_score,
            ar.reason,
            ar.token_validation_status
        FROM api_reviews ar
        JOIN suggestion_feedback f ON f.id = ar.feedback_id
        WHERE ar.status IN ({placeholders})
          AND (ar.status = 'approved' OR ar.confidence_score >= ?)
          AND ar.decision_suggested IN ('accepted', 'rejected', 'edited', 'accepted_old')
          AND (
              ar.decision_suggested = 'rejected'
              OR ar.token_validation_status IN ('ok', 'not_applicable')
          )
          AND f.decision = 'pending'
        ORDER BY ar.confidence_score DESC, ar.id ASC
        {limit_clause}
        """,
        params,
    ).fetchall()


def mark_high_confidence_reviews(conn, min_confidence: float) -> int:
    now = db.utc_now()
    return conn.execute(
        """
        UPDATE api_reviews
        SET
            status = 'auto_ready',
            updated_at = ?
        WHERE status = 'pending_human'
          AND confidence_score >= ?
          AND decision_suggested IN ('accepted', 'rejected', 'edited', 'accepted_old')
          AND (
              decision_suggested = 'rejected'
              OR token_validation_status IN ('ok', 'not_applicable')
          )
        """,
        (now, min_confidence),
    ).rowcount


def promote_review(conn, row, reviewer: str) -> None:
    now = db.utc_now()
    corrected_text = row["corrected_text"] if row["decision_suggested"] == "edited" else None
    reason = (
        f"api_review:{row['id']}; confidence={row['confidence_score']:.3f}; "
        f"{row['reason'] or ''}"
    ).strip()
    conn.execute(
        """
        UPDATE suggestion_feedback
        SET
            decision = ?,
            corrected_text = ?,
            reason = ?,
            reviewer = ?,
            reviewed_at = ?,
            updated_at = ?
        WHERE id = ?
          AND decision = 'pending'
        """,
        (
            row["decision_suggested"],
            corrected_text,
            reason,
            reviewer,
            now,
            now,
            row["feedback_id"],
        ),
    )
    conn.execute(
        """
        UPDATE api_reviews
        SET
            status = 'auto_applied',
            human_decision = decision_suggested,
            reviewed_by = ?,
            reviewed_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (reviewer, now, now, row["id"]),
    )


def main(
    min_confidence: float | None = None,
    include_approved: bool | None = None,
    mark_auto_ready: bool | None = None,
    limit: int | None = None,
    reviewer: str | None = None,
) -> None:
    if (
        min_confidence is None
        and include_approved is None
        and mark_auto_ready is None
        and limit is None
        and reviewer is None
    ):
        parser = argparse.ArgumentParser(description="Promote approved API reviews into suggestion_feedback.")
        parser.add_argument("--min-confidence", type=float, default=None)
        parser.add_argument(
            "--include-approved",
            action="store_true",
            help="Also apply api_reviews manually marked as status='approved'.",
        )
        parser.add_argument(
            "--mark-auto-ready",
            action="store_true",
            help="Mark high-confidence pending_human api_reviews as auto_ready before applying.",
        )
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--reviewer", default="api")
        args = parser.parse_args()
        min_confidence = args.min_confidence
        include_approved = args.include_approved
        mark_auto_ready = args.mark_auto_ready
        limit = args.limit
        reviewer = args.reviewer
    else:
        include_approved = bool(include_approved)
        mark_auto_ready = bool(mark_auto_ready)
        reviewer = reviewer or "api"

    settings = db.load_settings()
    api_settings = settings.get("api_review", {})
    min_confidence = (
        min_confidence
        if min_confidence is not None
        else float(api_settings.get("auto_apply_min_confidence", 0.95))
    )
    started_at = datetime.now()

    print("[apply_api_reviews] Starting API review promotion")
    print(f"[apply_api_reviews] Rule version: {RULE_VERSION}")
    print(f"[apply_api_reviews] Min confidence: {min_confidence}")
    print(f"[apply_api_reviews] Include approved: {include_approved}")

    marked = 0
    applied = 0
    decision_counts: Counter = Counter()

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        if mark_auto_ready:
            marked = mark_high_confidence_reviews(conn, min_confidence)
            print(f"[apply_api_reviews] Marked auto_ready: {marked}")

        rows = load_reviews(conn, min_confidence, include_approved, limit)
        print(f"[apply_api_reviews] Reviews to apply: {len(rows)}")
        for row in rows:
            promote_review(conn, row, reviewer)
            applied += 1
            decision_counts[row["decision_suggested"]] += 1
        conn.commit()

    elapsed = datetime.now() - started_at
    report_lines = [
        "Apply API reviews report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Min confidence: {min_confidence}",
        "",
        "Summary:",
        f"- Marked auto_ready: {marked}",
        f"- Applied reviews: {applied}",
        "",
        "Applied decisions:",
    ]
    for decision, count in sorted(decision_counts.items()):
        report_lines.append(f"- {decision}: {count}")
    report_path = db.write_report(settings, "apply_api_reviews", report_lines)
    print(f"[apply_api_reviews] Applied reviews: {applied}")
    print(f"[apply_api_reviews] Report: {report_path}")
    print("[apply_api_reviews] Done")


if __name__ == "__main__":
    main()
