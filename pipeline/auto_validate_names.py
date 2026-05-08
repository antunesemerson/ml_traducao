from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime

import db
import local_quality_validator
from apply_safe_output_updates import protected_tokens
from auto_validate_segments import has_human_baseline, percent, upsert_auto_confirmation


RULE_VERSION = "auto_validate_names_v1"
NAME_PATH_PREFIXES = (
    "names/",
    "dynasties/",
)


def normalize(value: str | None) -> str:
    return local_quality_validator.normalize(value)


def is_name_path(relative_path: str) -> bool:
    return any(relative_path.startswith(prefix) for prefix in NAME_PATH_PREFIXES)


def reject_reason(row: dict) -> str | None:
    relative_path = str(row["relative_path"] or "")
    english_text = str(row["english_text"] or "")
    spanish_text = str(row["spanish_text"] or "")
    candidate_text = str(row["candidate_text"] or "")
    sources_equal = normalize(english_text) == normalize(spanish_text) == normalize(candidate_text)

    if not is_name_path(relative_path):
        return "not_name_path"
    if not candidate_text.strip():
        return "empty_candidate"
    if protected_tokens(candidate_text) and not sources_equal:
        return "protected_tokens_present"
    if local_quality_validator.word_count(candidate_text) == 0 and not (
        sources_equal and protected_tokens(candidate_text)
    ):
        return "no_visible_words"
    if local_quality_validator.word_count(candidate_text) > 4 and not (
        sources_equal
        and relative_path.startswith("names/")
        and local_quality_validator.word_count(candidate_text) <= 8
    ):
        return "too_many_words"
    if normalize(english_text) != normalize(spanish_text):
        return "english_spanish_differ"
    if normalize(candidate_text) != normalize(english_text):
        return "candidate_differs_from_sources"

    validation = local_quality_validator.validate_text(candidate_text)
    if validation["issue_count"] and not sources_equal:
        issue_codes = ",".join(issue["code"] for issue in validation["issues"])
        return f"validator_issues:{issue_codes}"

    return None


def fetch_candidates(conn, limit: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.spanish_text,
            s.english_text,
            s.old_text AS candidate_text,
            a.classification,
            a.confidence_score AS analysis_confidence,
            NULL AS feedback_id,
            'name_auto_trusted' AS candidate_source
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
        LIMIT ?
        """,
        (limit * 50,),
    ).fetchall()

    accepted: list[dict] = []
    for row in rows:
        item = dict(row)
        if reject_reason(item) is None:
            accepted.append(item)
        if len(accepted) >= limit:
            break
    return accepted


def inspected_rejections(conn, limit: int) -> tuple[int, Counter[str]]:
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
        LIMIT ?
        """,
        (limit * 50,),
    ).fetchall()
    rejected: Counter[str] = Counter()
    for row in rows:
        reason = reject_reason(dict(row))
        if reason is not None:
            rejected[reason] += 1
    return len(rows), rejected


def main(limit: int | None = None, apply: bool = False) -> None:
    settings = db.load_settings()
    limit = limit if limit is not None else int(settings.get("auto_names", {}).get("review_limit", 5000))
    started_at = datetime.now()

    print("[auto_validate_names] Starting name auto validation")
    print(f"[auto_validate_names] Rule version: {RULE_VERSION}")
    print(f"[auto_validate_names] Limit: {limit}")
    print(f"[auto_validate_names] Apply: {apply}")
    print(f"[auto_validate_names] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        baseline_ok = has_human_baseline(conn)
        accepted = fetch_candidates(conn, limit) if baseline_ok else []
        inspected, rejected_counts = inspected_rejections(conn, limit)

        applied = 0
        if apply:
            for item in accepted:
                upsert_auto_confirmation(conn, item, 0.995)
                applied += 1
            conn.commit()

        total_segments = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM source_segments
                WHERE is_active = 1
                """
            ).fetchone()[0]
            or 0
        )
        confirmed_rows = conn.execute(
            """
            SELECT confirmation_level, COUNT(*) AS total
            FROM segment_confirmations
            GROUP BY confirmation_level
            """
        ).fetchall()

    confirmed = {row["confirmation_level"]: int(row["total"] or 0) for row in confirmed_rows}
    total_confirmed = sum(confirmed.values())
    elapsed = datetime.now() - started_at
    preview = accepted[:80]
    report_lines = [
        "Name auto validation report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Apply: {apply}",
        f"Limit: {limit}",
        f"Human baseline >= 100: {baseline_ok}",
        "",
        "Summary:",
        f"- Candidates inspected: {inspected}",
        f"- Auto-confirmable preview: {len(accepted)}",
        f"- Applied auto confirmations: {applied}",
        f"- Active segments: {total_segments}",
        f"- Confirmed after run: {total_confirmed} ({percent(total_confirmed, total_segments):.4f}%)",
        f"- Human confirmed: {confirmed.get('human_confirmed', 0)}",
        f"- Auto confirmed: {confirmed.get('auto_confirmed', 0)}",
        "",
        "Scope:",
        "- Paths: names/, dynasties/",
        "- Rule: english_text == spanish_text == old_text after normalization",
        "- Text only: no CK3 protected tokens, max 4 visible words",
        "",
        "Rejected reasons:",
        *[f"- {reason}: {total}" for reason, total in rejected_counts.most_common()],
        "",
        "Auto-confirmable preview:",
        *[
            (
                f"- segment {item['segment_id']} | 0.995 | {item['candidate_source']} | "
                f"{item['relative_path']}::{item['source_key']}"
            )
            for item in preview
        ],
    ]
    if not preview:
        report_lines.append("- No auto-confirmable candidates")

    report_path = db.write_report(settings, "auto_validate_names", report_lines)
    print(f"[auto_validate_names] Candidates inspected: {inspected}")
    print(f"[auto_validate_names] Auto-confirmable preview: {len(accepted)}")
    print(f"[auto_validate_names] Applied auto confirmations: {applied}")
    print(f"[auto_validate_names] Report: {report_path}")
    print("[auto_validate_names] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Auto-confirm safe name and dynasty localization rows.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum name candidates to confirm.")
    parser.add_argument("--apply", action="store_true", help="Write auto_confirmed rows. Default is report only.")
    args = parser.parse_args()
    main(limit=args.limit, apply=args.apply)
