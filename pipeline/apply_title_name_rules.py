from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime

import db


RULE_VERSION = "title_name_rules_v1"
AUTO_SCORE = 0.995

TITLE_TEMPLATE_REPLACEMENTS = {
    "TITLE_DEFINITIVE_NAME": "o $TIER|U$ de $NAME$",
    "TITLE_CLAN_TIERED_NAME": "o $NAME$ $TIER|U$",
    "TITLE_CLAN_TIERED_WITH_UNDERLYING_NAME": "o $NAME$ $TIER|U$ #F ($TIER|U$ de $BASE_NAME$) #!",
    "TITLE_CLAN_TIERED_WITH_UNDERLYING_NAME_DEFINITE_FORM": "o $TIER|U$ $NAME$ #F ($BASE_NAME$) #!",
}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def short(value: str | None, limit: int = 180) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def candidate_for(row: dict) -> tuple[str | None, str | None]:
    key = str(row["source_key"] or "")
    old = str(row["old_text"] or "")
    english = str(row["english_text"] or "")
    spanish = str(row["spanish_text"] or "")

    if key in TITLE_TEMPLATE_REPLACEMENTS:
        return TITLE_TEMPLATE_REPLACEMENTS[key], "title_template_ptbr"

    if old.startswith("Familia $dynn_") and english.endswith(" Family"):
        return old.replace("Familia ", "Família ", 1), "dynasty_family_label"

    if english and english == spanish == old:
        return old, "proper_title_name_preserved"

    return None, None


def fetch_rows(conn, limit: int | None) -> list[dict]:
    limit_sql = ""
    params: list[object] = []
    if limit is not None:
        limit_sql = "LIMIT ?"
        params.append(limit)
    rows = conn.execute(
        f"""
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.english_text,
            s.spanish_text,
            s.old_text,
            sc.locked,
            sc.confirmation_level
        FROM source_segments s
        LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
        WHERE s.is_active = 1
          AND s.relative_path = 'titles_l_spanish.yml'
          AND COALESCE(sc.locked, 0) = 0
          AND (
            s.source_key IN (
                'TITLE_DEFINITIVE_NAME',
                'TITLE_CLAN_TIERED_NAME',
                'TITLE_CLAN_TIERED_WITH_UNDERLYING_NAME',
                'TITLE_CLAN_TIERED_WITH_UNDERLYING_NAME_DEFINITE_FORM'
            )
            OR s.old_text LIKE 'Familia $dynn_%'
            OR (
                s.english_text = s.spanish_text
                AND s.spanish_text = s.old_text
                AND s.old_text != ''
            )
          )
        ORDER BY s.id ASC
        {limit_sql}
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def apply_confirmations(conn, accepted: list[dict]) -> None:
    timestamp = now()
    conn.executemany(
        """
        INSERT INTO segment_confirmations (
            segment_id, confirmation_level, confirmed_text, confirmation_source,
            confirmation_label, locked, confidence_score, reviewer, confirmed_at, updated_at
        )
        VALUES (?, 'auto_confirmed', ?, 'title_name_rules', ?, 0, ?, 'title_name_rules', ?, ?)
        ON CONFLICT(segment_id) DO UPDATE SET
            confirmation_level = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmation_level
                ELSE 'auto_confirmed'
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
        [
            (
                item["segment_id"],
                item["confirmed_text"],
                item["label"],
                AUTO_SCORE,
                timestamp,
                timestamp,
                timestamp,
            )
            for item in accepted
        ],
    )


def main(limit: int | None = None, apply: bool = False) -> None:
    settings = db.load_settings()
    started_at = datetime.now()

    print("[apply_title_name_rules] Starting title name rules")
    print(f"[apply_title_name_rules] Rule version: {RULE_VERSION}")
    print(f"[apply_title_name_rules] Apply: {apply}")
    print(f"[apply_title_name_rules] Limit: {limit or 'none'}")
    print(f"[apply_title_name_rules] Database: {db.get_database_path(settings)}")

    accepted: list[dict] = []
    skipped = Counter()

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        rows = fetch_rows(conn, limit)
        for row in rows:
            confirmed_text, label = candidate_for(row)
            if not confirmed_text or not label:
                skipped["no_rule"] += 1
                continue
            accepted.append({**row, "confirmed_text": confirmed_text, "label": label})
        if apply:
            apply_confirmations(conn, accepted)
            conn.commit()

    elapsed = datetime.now() - started_at
    label_counts = Counter(item["label"] for item in accepted)
    report_lines = [
        "Title name rules report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Apply: {apply}",
        f"Limit: {limit or 'none'}",
        "",
        "Summary:",
        f"- Candidates accepted: {len(accepted)}",
        f"- Confirmations written: {len(accepted) if apply else 0}",
        "",
        "Labels:",
        *[f"- {label}: {count}" for label, count in label_counts.most_common()],
        "",
        "Skipped:",
        *[f"- {reason}: {count}" for reason, count in skipped.most_common()],
        "",
        "Preview:",
    ]
    for item in accepted[:80]:
        report_lines.extend(
            [
                f"- segment {item['segment_id']} | {item['label']} | {item['source_key']}",
                f"  EN: {short(item['english_text'])}",
                f"  ES: {short(item['spanish_text'])}",
                f"  OLD: {short(item['old_text'])}",
                f"  OUT: {short(item['confirmed_text'])}",
            ]
        )
    if not accepted:
        report_lines.append("- No candidates accepted")

    report_path = db.write_report(settings, "apply_title_name_rules", report_lines)
    print(f"[apply_title_name_rules] Candidates accepted: {len(accepted)}")
    print(f"[apply_title_name_rules] Confirmations written: {len(accepted) if apply else 0}")
    for label, count in label_counts.most_common():
        print(f"[apply_title_name_rules] {label}: {count}")
    print(f"[apply_title_name_rules] Report: {report_path}")
    print("[apply_title_name_rules] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apply conservative rules for CK3 title names and title templates.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum candidates to inspect.")
    parser.add_argument("--apply", action="store_true", help="Write auto_confirmed rows. Default is dry-run.")
    args = parser.parse_args()
    main(limit=args.limit, apply=args.apply)
