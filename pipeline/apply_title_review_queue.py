from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime

import db


RULE_VERSION = "apply_title_review_queue_v1"
REVIEWED_STATUSES = {"accepted", "accepted_old", "edited"}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def short(value: str | None, limit: int = 180) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    return value


def chosen_text(row: dict) -> tuple[str | None, str | None]:
    status = row["status"]
    if status == "accepted":
        text = clean_text(row["proposed_text"])
        if text is None:
            return None, "accepted row has no proposed_text"
        return text, None
    if status == "accepted_old":
        text = clean_text(row["old_text"])
        if text is None:
            return None, "accepted_old row has no old_text"
        return text, None
    if status == "edited":
        text = clean_text(row["corrected_text"])
        if text is None:
            return None, "edited row has no corrected_text"
        return text, None
    return None, f"unsupported status {status}"


def fetch_reviewed_rows(conn, limit: int | None) -> list[dict]:
    params: list[object] = []
    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT ?"
        params.append(limit)
    placeholders = ", ".join("?" for _ in REVIEWED_STATUSES)
    params = [*sorted(REVIEWED_STATUSES), *params]
    rows = conn.execute(
        f"""
        SELECT *
        FROM title_review_queue
        WHERE status IN ({placeholders})
          AND applied_at IS NULL
        ORDER BY id ASC
        {limit_sql}
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def apply_confirmation(conn, row: dict, confirmed_text: str, timestamp: str) -> None:
    reviewer = row["reviewer"] or "title_review_queue"
    confidence = 1.0 if row["status"] in {"accepted", "accepted_old", "edited"} else row["confidence_score"]
    conn.execute(
        """
        INSERT INTO segment_confirmations (
            segment_id,
            confirmation_level,
            confirmed_text,
            confirmation_source,
            confirmation_label,
            locked,
            confidence_score,
            reviewer,
            confirmed_at,
            updated_at
        )
        VALUES (?, 'human_confirmed', ?, 'title_review_queue', ?, 1, ?, ?, ?, ?)
        ON CONFLICT(segment_id) DO UPDATE SET
            confirmation_level = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmation_level
                ELSE 'human_confirmed'
            END,
            confirmed_text = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmed_text
                ELSE excluded.confirmed_text
            END,
            confirmation_source = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmation_source
                ELSE excluded.confirmation_source
            END,
            confirmation_label = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmation_label
                ELSE excluded.confirmation_label
            END,
            locked = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.locked
                ELSE 1
            END,
            confidence_score = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confidence_score
                ELSE excluded.confidence_score
            END,
            reviewer = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.reviewer
                ELSE excluded.reviewer
            END,
            updated_at = ?
        """,
        (
            row["segment_id"],
            confirmed_text,
            f"title_{row['status']}_{row['bucket']}",
            confidence,
            reviewer,
            row["reviewed_at"] or timestamp,
            timestamp,
            timestamp,
        ),
    )


def mark_queue_row(conn, queue_id: int, timestamp: str, result: str) -> None:
    conn.execute(
        """
        UPDATE title_review_queue
        SET applied_at = ?,
            apply_result = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (timestamp, result, timestamp, queue_id),
    )


def main(limit: int | None = None, apply: bool = False) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    timestamp = now()

    print("[apply_title_review_queue] Starting title review queue apply")
    print(f"[apply_title_review_queue] Rule version: {RULE_VERSION}")
    print(f"[apply_title_review_queue] Limit: {limit or 'none'}")
    print(f"[apply_title_review_queue] Apply: {apply}")
    print(f"[apply_title_review_queue] Database: {db.get_database_path(settings)}")

    preview: list[tuple[dict, str]] = []
    result_counts: Counter[str] = Counter()

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        rows = fetch_reviewed_rows(conn, limit)
        for row in rows:
            text, problem = chosen_text(row)
            if problem:
                result_counts["skipped_invalid_review"] += 1
                if apply:
                    mark_queue_row(conn, row["id"], timestamp, problem)
                continue
            preview.append((row, text or ""))
            if apply:
                apply_confirmation(conn, row, text or "", timestamp)
                mark_queue_row(conn, row["id"], timestamp, "applied")
                result_counts["applied"] += 1
            else:
                result_counts["preview"] += 1
        if apply:
            conn.commit()

    elapsed = datetime.now() - started_at
    report_lines = [
        "Title review queue apply report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Apply: {apply}",
        f"Limit: {limit or 'none'}",
        "",
        "Summary:",
        f"- Reviewed queue rows inspected: {len(rows)}",
        *[f"- {key}: {value}" for key, value in result_counts.most_common()],
        "",
        "Preview:",
    ]
    for row, text in preview[:100]:
        report_lines.extend(
            [
                f"- queue {row['id']} | segment {row['segment_id']} | {row['status']} | {row['bucket']} | {row['source_key']}",
                f"  OLD: {short(row['old_text'])}",
                f"  PROPOSED: {short(row['proposed_text'])}",
                f"  CONFIRMED: {short(text)}",
            ]
        )
    if not preview:
        report_lines.append("- No reviewed rows ready to apply")

    report_path = db.write_report(settings, "apply_title_review_queue", report_lines)
    print(f"[apply_title_review_queue] Reviewed queue rows inspected: {len(rows)}")
    for key, value in result_counts.most_common():
        print(f"[apply_title_review_queue] {key}: {value}")
    print(f"[apply_title_review_queue] Report: {report_path}")
    print("[apply_title_review_queue] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apply reviewed title queue rows as locked confirmations.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum reviewed queue rows to inspect.")
    parser.add_argument("--apply", action="store_true", help="Write human-locked segment confirmations.")
    args = parser.parse_args()
    main(limit=args.limit, apply=args.apply)
