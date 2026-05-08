from __future__ import annotations

from collections import defaultdict
from datetime import datetime

import auto_validate_names
import db


RULE_VERSION = "report_name_rejections_v1"
REASONS = (
    "candidate_differs_from_sources",
    "english_spanish_differ",
    "protected_tokens_present",
    "validator_issues:spanish_residue",
    "validator_issues:mojibake_or_unexpected_script",
    "too_many_words",
    "no_visible_words",
)


def fetch_rows(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.spanish_text,
            s.english_text,
            s.old_text AS candidate_text
        FROM source_segments s
        JOIN segment_analysis a ON a.segment_id = s.id
        LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
        WHERE s.is_active = 1
          AND s.has_old = 1
          AND s.old_text IS NOT NULL
          AND trim(s.old_text) != ''
          AND a.classification = 'trusted'
          AND COALESCE(a.confidence_score, 0) >= 0.99
          AND sc.segment_id IS NULL
          AND (
              s.relative_path LIKE 'names/%'
              OR s.relative_path LIKE 'dynasties/%'
          )
        ORDER BY s.relative_path ASC, s.id ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def short(value: str | None, limit: int = 130) -> str:
    text = (value or "").replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def main(sample_limit: int = 30) -> None:
    settings = db.load_settings()
    started_at = datetime.now()

    print("[report_name_rejections] Starting rejected name report")
    print(f"[report_name_rejections] Rule version: {RULE_VERSION}")
    print(f"[report_name_rejections] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        rows = fetch_rows(conn)

    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        reason = auto_validate_names.reject_reason(row) or "accepted"
        buckets[reason].append(row)

    report_lines = [
        "Name rejection report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Rows inspected: {len(rows)}",
        "",
        "Counts:",
        *[f"- {reason}: {len(buckets.get(reason, []))}" for reason in REASONS],
        "",
    ]

    for reason in REASONS:
        sample = buckets.get(reason, [])[:sample_limit]
        report_lines.extend([f"Samples: {reason}", ""])
        if not sample:
            report_lines.append("- No rows")
        for row in sample:
            report_lines.extend(
                [
                    f"- segment {row['segment_id']} | {row['relative_path']}::{row['source_key']}",
                    f"  EN: {short(row['english_text'])}",
                    f"  ES: {short(row['spanish_text'])}",
                    f"  OLD: {short(row['candidate_text'])}",
                ]
            )
        report_lines.append("")

    report_path = db.write_report(settings, "name_rejections", report_lines)
    print(f"[report_name_rejections] Rows inspected: {len(rows)}")
    for reason in REASONS:
        print(f"[report_name_rejections] {reason}: {len(buckets.get(reason, []))}")
    print(f"[report_name_rejections] Report: {report_path}")
    print("[report_name_rejections] Done")


if __name__ == "__main__":
    main()
