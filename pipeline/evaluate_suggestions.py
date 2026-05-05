from __future__ import annotations

from collections import defaultdict
from datetime import datetime

import db


RULE_VERSION = "suggestion_feedback_eval_v1"


def percent(part: int, total: int) -> str:
    if total == 0:
        return "0.00%"
    return f"{part / total:.2%}"


def main() -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[evaluate_suggestions] Starting suggestion feedback evaluation")
    print(f"[evaluate_suggestions] Rule version: {RULE_VERSION}")
    print(f"[evaluate_suggestions] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        rows = conn.execute(
            """
            SELECT
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

    total = len(rows)
    accepted = sum(1 for row in rows if row["decision"] == "accepted")
    accepted_old = sum(1 for row in rows if row["decision"] == "accepted_old")
    edited = sum(1 for row in rows if row["decision"] == "edited")
    rejected = sum(1 for row in rows if row["decision"] == "rejected")
    useful = accepted + accepted_old + edited

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
        f"- Feedback rows: {total}",
        f"- Accepted: {accepted}",
        f"- Accepted old text: {accepted_old}",
        f"- Edited: {edited}",
        f"- Rejected: {rejected}",
        f"- Useful precision: {useful}/{total} ({percent(useful, total)})",
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

    report_path = db.write_report(settings, "evaluate_suggestions", report_lines)
    print(f"[evaluate_suggestions] Feedback rows: {total}")
    print(f"[evaluate_suggestions] Useful precision: {useful}/{total} ({percent(useful, total)})")
    print(f"[evaluate_suggestions] Report: {report_path}")
    print("[evaluate_suggestions] Done")


if __name__ == "__main__":
    main()
