from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime

import db
import local_quality_validator


RULE_VERSION = "apply_name_equivalences_v1"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize(value: str | None) -> str:
    return local_quality_validator.normalize(value)


def fetch_confirmed_equivalences(conn, limit: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT *
        FROM name_equivalences
        WHERE status = 'human_confirmed'
          AND source_kind = 'character_name'
        ORDER BY evidence_count DESC, id ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def matching_segments(conn, source_name: str) -> list[dict]:
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
          AND s.relative_path LIKE 'names/%'
          AND s.english_text IS NOT NULL
          AND s.spanish_text IS NOT NULL
          AND trim(s.english_text) = ?
          AND trim(s.spanish_text) = ?
        ORDER BY s.id ASC
        """,
        (source_name, source_name),
    ).fetchall()
    return [dict(row) for row in rows]


def apply_confirmation(conn, equivalence: dict, segment: dict, timestamp: str) -> None:
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
        VALUES (?, 'human_confirmed', ?, 'name_equivalence', 'human_confirmed_name_equivalence', 1, ?, ?, ?, ?)
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
            segment["segment_id"],
            equivalence["portuguese_name"],
            equivalence["confidence_score"] or 0.96,
            equivalence["reviewer"] or "name_equivalence",
            equivalence["reviewed_at"] or timestamp,
            timestamp,
            timestamp,
        ),
    )


def main(limit: int | None = None, apply: bool = False) -> None:
    settings = db.load_settings()
    limit = limit if limit is not None else int(settings.get("name_equivalences", {}).get("apply_limit", 500))
    started_at = datetime.now()
    timestamp = now()

    print("[apply_name_equivalences] Starting name equivalence apply")
    print(f"[apply_name_equivalences] Rule version: {RULE_VERSION}")
    print(f"[apply_name_equivalences] Limit: {limit}")
    print(f"[apply_name_equivalences] Apply: {apply}")
    print(f"[apply_name_equivalences] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        equivalences = fetch_confirmed_equivalences(conn, limit)
        result_counts: Counter[str] = Counter()
        preview: list[tuple[dict, dict]] = []

        for equivalence in equivalences:
            segments = matching_segments(conn, equivalence["source_name"])
            if not segments:
                result_counts["no_unconfirmed_segments"] += 1
                continue
            for segment in segments:
                if normalize(segment["english_text"]) != normalize(segment["spanish_text"]):
                    result_counts["skipped_source_mismatch"] += 1
                    continue
                preview.append((equivalence, segment))
                if apply:
                    apply_confirmation(conn, equivalence, segment, timestamp)
                    result_counts["applied_segments"] += 1
                else:
                    result_counts["preview_segments"] += 1
        if apply:
            conn.commit()

    elapsed = datetime.now() - started_at
    report_lines = [
        "Name equivalence apply report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Apply: {apply}",
        "",
        "Summary:",
        f"- Confirmed equivalences inspected: {len(equivalences)}",
        *[f"- {key}: {value}" for key, value in result_counts.most_common()],
        "",
        "Preview:",
    ]
    for equivalence, segment in preview[:80]:
        report_lines.append(
            (
                f"- segment {segment['segment_id']} | {segment['source_key']} | "
                f"{equivalence['source_name']} -> {equivalence['portuguese_name']}"
            )
        )
    if not preview:
        report_lines.append("- No matching segments")

    report_path = db.write_report(settings, "apply_name_equivalences", report_lines)
    print(f"[apply_name_equivalences] Confirmed equivalences inspected: {len(equivalences)}")
    for key, value in result_counts.most_common():
        print(f"[apply_name_equivalences] {key}: {value}")
    print(f"[apply_name_equivalences] Report: {report_path}")
    print("[apply_name_equivalences] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apply human-confirmed historical name equivalences.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum confirmed equivalences to inspect.")
    parser.add_argument("--apply", action="store_true", help="Write human-locked segment confirmations.")
    args = parser.parse_args()
    main(limit=args.limit, apply=args.apply)
