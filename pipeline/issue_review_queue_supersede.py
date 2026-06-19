from __future__ import annotations

import argparse
from pathlib import Path

import db


RULE_VERSION = "issue_review_queue_supersede_v1"
SUPERSEDED_STATUS = "superseded"


def report_path(settings: dict, queue_run_id: int) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = db.utc_now().replace(":", "").replace("-", "").replace("Z", "")
    return reports_dir / f"{stamp}_issue_review_queue_{queue_run_id}_superseded.txt"


def main(*, queue_run_id: int, reason: str, allow_reviewed: bool = False) -> dict[str, object]:
    settings = db.load_settings()
    conn = db.connect(settings)
    now = db.utc_now()
    with conn:
        queue_run = conn.execute(
            """
            SELECT *
            FROM ml_issue_review_queue_runs
            WHERE id = ?
            LIMIT 1
            """,
            (queue_run_id,),
        ).fetchone()
        if queue_run is None:
            raise RuntimeError(f"Queue run not found: {queue_run_id}")

        decision_count = int(
            conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM ml_issue_review_decisions
                WHERE queue_run_id = ?
                """,
                (queue_run_id,),
            ).fetchone()["count"]
            or 0
        )
        if decision_count and not allow_reviewed:
            raise RuntimeError(
                f"Queue run {queue_run_id} already has {decision_count} decision(s). "
                "Pass --allow-reviewed only for an intentional archival supersede."
            )

        before = conn.execute(
            """
            SELECT review_status, COUNT(*) AS count
            FROM ml_issue_review_queue_items
            WHERE run_id = ?
            GROUP BY review_status
            ORDER BY review_status
            """,
            (queue_run_id,),
        ).fetchall()

        conn.execute(
            """
            UPDATE ml_issue_review_queue_items
            SET
                review_status = ?,
                reviewer_notes = TRIM(COALESCE(reviewer_notes, '') || CASE
                    WHEN COALESCE(reviewer_notes, '') = '' THEN ''
                    ELSE ' | '
                END || ?),
                reviewed_at = ?
            WHERE run_id = ?
              AND review_status != 'reviewed'
            """,
            (SUPERSEDED_STATUS, f"{RULE_VERSION}: {reason}", now, queue_run_id),
        )
        changed = int(conn.total_changes)

        conn.execute(
            """
            UPDATE ml_issue_review_queue_runs
            SET
                open_count = 0,
                updated_at = ?
            WHERE id = ?
            """,
            (now, queue_run_id),
        )

        after = conn.execute(
            """
            SELECT review_status, COUNT(*) AS count
            FROM ml_issue_review_queue_items
            WHERE run_id = ?
            GROUP BY review_status
            ORDER BY review_status
            """,
            (queue_run_id,),
        ).fetchall()

    path = report_path(settings, queue_run_id)
    lines = [
        "Issue review queue supersede",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {queue_run_id}",
        f"Agent key: {queue_run['agent_key']}",
        f"Reason: {reason}",
        f"Decision count: {decision_count}",
        f"Rows touched: {changed}",
        "",
        "Before:",
        *[f"- {row['review_status']}: {int(row['count']):,}" for row in before],
        "",
        "After:",
        *[f"- {row['review_status']}: {int(row['count']):,}" for row in after],
        "",
        "Safety note:",
        "- This script does not delete evidence.",
        "- Superseded rows are ignored by event-surface queue de-duplication.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("[issue_review_queue_supersede] Queue superseded")
    print(f"[issue_review_queue_supersede] Queue run id: {queue_run_id}")
    print(f"[issue_review_queue_supersede] Rows touched: {changed:,}")
    print(f"[issue_review_queue_supersede] Report: {path}")
    return {"queue_run_id": queue_run_id, "rows_touched": changed, "report_path": str(path)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mark a stale issue review queue as superseded.")
    parser.add_argument("--queue-run-id", type=int, required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--allow-reviewed", action="store_true")
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id, reason=args.reason, allow_reviewed=args.allow_reviewed)
