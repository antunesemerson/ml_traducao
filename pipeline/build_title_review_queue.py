from __future__ import annotations

import argparse
import re
from collections import Counter
from datetime import datetime

import db


RULE_VERSION = "build_title_review_queue_v1"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def short(value: str | None, limit: int = 180) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def is_adjective_key(source_key: str) -> bool:
    return source_key.endswith("_adj")


def source_prefix(source_key: str) -> str:
    return source_key.split("_", 1)[0]


def classify(row: dict) -> tuple[str, str, str | None, float, str]:
    key = str(row["source_key"] or "")
    english = str(row["english_text"] or "")
    old = str(row["old_text"] or "")
    spanish = str(row["spanish_text"] or "")

    if key.endswith("_article"):
        article_map = {"la": "a", "el": "o", "the": ""}
        proposed = article_map.get(old.casefold())
        return (
            "article",
            "review_article",
            proposed,
            0.70,
            "Article row can affect title grammar; review before applying.",
        )

    if is_adjective_key(key):
        proposed = english if english and english != old else None
        return (
            "adjective",
            "review_adjective",
            proposed,
            0.55,
            "Adjective/exonym rows are context-sensitive and often need PT-BR wording.",
        )

    if old.startswith("Compañía "):
        return (
            "mercenary_company",
            "translate_phrase",
            old.replace("Compañía ", "Companhia ", 1),
            0.82,
            "Spanish company label with a mechanical PT-BR base translation.",
        )

    if old.startswith("Islas del "):
        return (
            "island_title",
            "translate_phrase",
            old.replace("Islas del ", "Ilhas do ", 1),
            0.82,
            "Spanish island title with a mechanical PT-BR base translation.",
        )

    if old.startswith("Islas de "):
        return (
            "island_title",
            "translate_phrase",
            old.replace("Islas de ", "Ilhas de ", 1),
            0.80,
            "Spanish island title with a mechanical PT-BR base translation.",
        )

    if " del norte" in old:
        return (
            "directional_exonym",
            "translate_phrase",
            old.replace(" del norte", " do norte"),
            0.78,
            "Spanish directional phrase.",
        )

    if " del sur" in old:
        return (
            "directional_exonym",
            "translate_phrase",
            old.replace(" del sur", " do sul"),
            0.78,
            "Spanish directional phrase.",
        )

    if re.search(r"\b(de|del|la|las|los|el)\b", old, re.IGNORECASE):
        if english and english != old:
            return (
                "exonym",
                "review_exonym",
                english,
                0.55,
                "Spanish/French-looking article or preposition in a title; English/original may be safer but needs review.",
            )
        return (
            "proper_name_with_article",
            "preserve_likely_name",
            old,
            0.86,
            "Article/preposition is part of an unchanged proper title name.",
        )

    if any(char in old for char in ("ñ", "Ñ")):
        return (
            "spanish_exonym",
            "review_exonym",
            english if english else old,
            0.52,
            "Spanish ñ appears in title; may be a Spanish exonym or legitimate local form.",
        )

    if source_prefix(key) in {"b", "c", "d", "k", "e"}:
        return (
            "title_name_uncertain",
            "review_title_name",
            english if english and english != old else old,
            0.50,
            "Remaining title name differs from reference and needs human or curated rule review.",
        )

    return (
        "other",
        "review",
        english if english and english != old else old,
        0.40,
        "Unclassified title row.",
    )


def fetch_pending_title_rows(conn, limit: int | None) -> list[dict]:
    params: list[object] = []
    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT ?"
        params.append(limit)
    rows = conn.execute(
        f"""
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text
        FROM source_segments s
        LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
        WHERE s.is_active = 1
          AND s.relative_path = 'titles_l_spanish.yml'
          AND sc.segment_id IS NULL
        ORDER BY s.id ASC
        {limit_sql}
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def upsert_queue(conn, items: list[dict]) -> None:
    timestamp = now()
    conn.executemany(
        """
        INSERT INTO title_review_queue (
            segment_id, relative_path, source_key, source_line_number,
            english_text, spanish_text, old_text, proposed_text,
            bucket, recommendation, confidence_score, status, reason,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
        ON CONFLICT(segment_id) DO UPDATE SET
            relative_path = excluded.relative_path,
            source_key = excluded.source_key,
            source_line_number = excluded.source_line_number,
            english_text = excluded.english_text,
            spanish_text = excluded.spanish_text,
            old_text = excluded.old_text,
            proposed_text = excluded.proposed_text,
            bucket = excluded.bucket,
            recommendation = excluded.recommendation,
            confidence_score = excluded.confidence_score,
            reason = CASE
                WHEN title_review_queue.status = 'pending' THEN excluded.reason
                ELSE title_review_queue.reason
            END,
            updated_at = excluded.updated_at
        """,
        [
            (
                item["segment_id"],
                item["relative_path"],
                item["source_key"],
                item["source_line_number"],
                item["english_text"],
                item["spanish_text"],
                item["old_text"],
                item["proposed_text"],
                item["bucket"],
                item["recommendation"],
                item["confidence_score"],
                item["reason"],
                timestamp,
                timestamp,
            )
            for item in items
        ],
    )


def main(limit: int | None = None) -> None:
    settings = db.load_settings()
    started_at = datetime.now()

    print("[build_title_review_queue] Starting title review queue")
    print(f"[build_title_review_queue] Rule version: {RULE_VERSION}")
    print(f"[build_title_review_queue] Limit: {limit or 'none'}")
    print(f"[build_title_review_queue] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        rows = fetch_pending_title_rows(conn, limit)
        items: list[dict] = []
        for row in rows:
            bucket, recommendation, proposed_text, confidence_score, reason = classify(row)
            items.append(
                {
                    **row,
                    "bucket": bucket,
                    "recommendation": recommendation,
                    "proposed_text": proposed_text,
                    "confidence_score": confidence_score,
                    "reason": reason,
                }
            )
        upsert_queue(conn, items)
        conn.commit()

    elapsed = datetime.now() - started_at
    bucket_counts = Counter(item["bucket"] for item in items)
    recommendation_counts = Counter(item["recommendation"] for item in items)
    report_lines = [
        "Title review queue report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Limit: {limit or 'none'}",
        "",
        "Summary:",
        f"- Pending title rows inspected: {len(rows)}",
        f"- Queue rows upserted: {len(items)}",
        "",
        "Buckets:",
        *[f"- {bucket}: {count}" for bucket, count in bucket_counts.most_common()],
        "",
        "Recommendations:",
        *[f"- {recommendation}: {count}" for recommendation, count in recommendation_counts.most_common()],
        "",
    ]
    for bucket, _ in bucket_counts.most_common():
        report_lines.extend([f"Samples: {bucket}", ""])
        for item in [item for item in items if item["bucket"] == bucket][:20]:
            report_lines.extend(
                [
                    f"- segment {item['segment_id']} | {item['source_key']} | {item['recommendation']} | {item['confidence_score']:.2f}",
                    f"  EN: {short(item['english_text'])}",
                    f"  ES: {short(item['spanish_text'])}",
                    f"  OLD: {short(item['old_text'])}",
                    f"  PROPOSED: {short(item['proposed_text'])}",
                    f"  reason: {item['reason']}",
                ]
            )
        report_lines.append("")

    report_path = db.write_report(settings, "build_title_review_queue", report_lines)
    print(f"[build_title_review_queue] Pending title rows inspected: {len(rows)}")
    print(f"[build_title_review_queue] Queue rows upserted: {len(items)}")
    for bucket, count in bucket_counts.most_common():
        print(f"[build_title_review_queue] {bucket}: {count}")
    print(f"[build_title_review_queue] Report: {report_path}")
    print("[build_title_review_queue] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a review queue for unresolved CK3 title localization rows.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum pending title rows to queue.")
    args = parser.parse_args()
    main(limit=args.limit)
