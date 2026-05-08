from __future__ import annotations

from datetime import datetime

import db
from auto_validate_segments import upsert_auto_confirmation


RULE_VERSION = "report_dynasty_prefixes_v1"


def suggested_action(source_key: str, english_text: str, old_text: str) -> tuple[str, str, str | None]:
    key = source_key.strip()
    english = english_text.strip()
    old = old_text.strip()
    if english and english == old:
        return "confirm_current", "source and current text are identical", old_text
    if key == "dynnp__":
        return "confirm_current", "technical empty/symbol prefix is identical", old_text
    if key == "dynnp_of" and old == "de":
        return "keep_old", "English 'of' naturally maps to Portuguese 'de'", old_text
    if key in {"dynnp_af", "dynnp_an", "dynnp_da", "dynnp_del", "dynnp_della", "dynnpat_pre_di"}:
        return "prefer_source", f"cultural name particle; source '{english}' is safer than '{old}'", english_text
    if key == "dynnpat_pre_merch":
        return "prefer_source", "current text is a literal common-word translation", english_text
    return "review", "manual decision needed", None


def main(apply: bool = False) -> None:
    settings = db.load_settings()
    started_at = datetime.now()

    print("[report_dynasty_prefixes] Starting dynasty prefix report")
    print(f"[report_dynasty_prefixes] Rule version: {RULE_VERSION}")
    print(f"[report_dynasty_prefixes] Apply: {apply}")
    print(f"[report_dynasty_prefixes] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        rows = conn.execute(
            """
            SELECT
                s.id AS segment_id,
                s.relative_path,
                s.source_key,
                s.english_text,
                s.spanish_text,
                s.old_text
            FROM source_segments s
            LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
            WHERE s.is_active = 1
              AND sc.segment_id IS NULL
              AND s.relative_path LIKE 'dynasties/%'
              AND s.source_key LIKE 'dynnp_%'
            ORDER BY s.id ASC
            """
        ).fetchall()
        applied = 0
        if apply:
            for row in rows:
                action, _, candidate_text = suggested_action(
                    row["source_key"],
                    row["english_text"] or "",
                    row["old_text"] or "",
                )
                if action == "review" or candidate_text is None:
                    continue
                item = {
                    "segment_id": row["segment_id"],
                    "candidate_text": candidate_text,
                    "candidate_source": f"dynasty_prefix_{action}",
                    "feedback_id": None,
                }
                upsert_auto_confirmation(conn, item, 0.992)
                applied += 1
            conn.commit()

    report_lines = [
        "Dynasty prefix report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Apply: {apply}",
        "",
        f"Rows inspected: {len(rows)}",
        f"Applied: {applied}",
        "",
        "Rows:",
    ]
    for row in rows:
        action, reason, candidate_text = suggested_action(
            row["source_key"],
            row["english_text"] or "",
            row["old_text"] or "",
        )
        report_lines.extend(
            [
                f"- segment {row['segment_id']} | {row['source_key']} | action={action}",
                f"  EN: {row['english_text']}",
                f"  ES: {row['spanish_text']}",
                f"  OLD: {row['old_text']}",
                f"  CANDIDATE: {candidate_text if candidate_text is not None else ''}",
                f"  reason: {reason}",
            ]
        )

    report_path = db.write_report(settings, "report_dynasty_prefixes", report_lines)
    print(f"[report_dynasty_prefixes] Rows inspected: {len(rows)}")
    print(f"[report_dynasty_prefixes] Applied: {applied}")
    print(f"[report_dynasty_prefixes] Report: {report_path}")
    print("[report_dynasty_prefixes] Done")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Report and optionally apply dynasty prefix decisions.")
    parser.add_argument("--apply", action="store_true", help="Apply recommended prefix confirmations.")
    args = parser.parse_args()
    main(apply=args.apply)
