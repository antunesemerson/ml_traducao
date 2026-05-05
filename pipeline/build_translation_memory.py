from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime

import db


MIN_CONFIDENCE = 0.9
BATCH_SIZE = 5000
TARGET_LANGUAGE = "pt-BR"
RULE_VERSION = "translation_memory_v1"

SKIP_REASON_RULES = {
    "empty_segment_preserved",
    "technical_segment_preserved",
}

PROTECTED_TOKEN_PATTERN = re.compile(
    r"\$[^$\s]+\$|\[[^\]]+\]|#[A-Za-z0-9_]+|#!|@[A-Za-z0-9_]+!|\\n"
)
WORD_PATTERN = re.compile(r"[A-Za-z\u00c0-\u00ff]+", re.UNICODE)
MACRO_ONLY_PATTERN = re.compile(r"^[\s$A-Z0-9_|\[\].:#@!\\/-]+$")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def is_blank(value: str | None) -> bool:
    return value is None or value.strip() == ""


def strip_protected_tokens(value: str | None) -> str:
    if not value:
        return ""
    stripped = PROTECTED_TOKEN_PATTERN.sub(" ", value)
    stripped = re.sub(r"[$#@!|_:\[\].()0-9\\/-]+", " ", stripped)
    return " ".join(stripped.split())


def is_technical_text(value: str | None) -> bool:
    if is_blank(value):
        return False
    human_text = strip_protected_tokens(value)
    if not human_text:
        return True
    if MACRO_ONLY_PATTERN.match(value or ""):
        return True
    words = WORD_PATTERN.findall(human_text)
    return len(words) <= 1 and any(marker in (value or "") for marker in ["$", "[", "#", "@"])


def reason_rules(reasons_json: str | None) -> set[str]:
    if not reasons_json:
        return set()
    try:
        reasons = json.loads(reasons_json)
    except json.JSONDecodeError:
        return set()
    return {reason.get("rule") for reason in reasons if reason.get("rule")}


def should_skip_pair(
    source_text: str | None,
    target_text: str | None,
    reasons_json: str | None,
) -> tuple[bool, str | None]:
    if is_blank(source_text):
        return True, "blank_source_text"
    if is_blank(target_text):
        return True, "blank_target_text"
    if is_technical_text(source_text) or is_technical_text(target_text):
        return True, "technical_text"
    rules = reason_rules(reasons_json)
    if rules & SKIP_REASON_RULES:
        return True, "analysis_rule_skip"
    return False, None


def upsert_memory_pair(
    conn,
    source_segment_id: int,
    source_language: str,
    source_text: str,
    target_text: str,
    confidence_score: float,
    origin: str,
) -> str:
    source_hash = sha256_text(source_text)
    target_hash = sha256_text(target_text)
    now = db.utc_now()
    existing = conn.execute(
        """
        SELECT id, usage_count, confidence_score
        FROM translation_memory
        WHERE source_language = ?
          AND target_language = ?
          AND source_hash = ?
          AND target_hash = ?
          AND origin = ?
        LIMIT 1
        """,
        (source_language, TARGET_LANGUAGE, source_hash, target_hash, origin),
    ).fetchone()

    if existing:
        best_confidence = max(existing["confidence_score"] or 0, confidence_score)
        conn.execute(
            """
            UPDATE translation_memory
            SET
                source_segment_id = ?,
                source_text = ?,
                target_text = ?,
                confidence_score = ?,
                usage_count = usage_count + 1,
                last_seen_at = ?
            WHERE id = ?
            """,
            (
                source_segment_id,
                source_text,
                target_text,
                best_confidence,
                now,
                existing["id"],
            ),
        )
        return "updated"

    conn.execute(
        """
        INSERT INTO translation_memory (
            source_segment_id,
            source_language,
            target_language,
            source_text,
            target_text,
            source_hash,
            target_hash,
            confidence_score,
            origin,
            usage_count,
            created_at,
            last_seen_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            source_segment_id,
            source_language,
            TARGET_LANGUAGE,
            source_text,
            target_text,
            source_hash,
            target_hash,
            confidence_score,
            origin,
            now,
            now,
        ),
    )
    return "inserted"


def build_feedback_memory(conn) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT
            f.segment_id,
            f.decision,
            f.corrected_text,
            s.spanish_text,
            s.english_text,
            s.old_text,
            ts.suggested_text,
            ts.match_score
        FROM suggestion_feedback f
        LEFT JOIN translation_suggestions ts ON ts.id = f.suggestion_id
        JOIN source_segments s ON s.id = f.segment_id
        WHERE f.decision IN ('accepted', 'edited', 'accepted_old')
          AND s.is_active = 1
        """
    ).fetchall()

    inserted = 0
    updated = 0
    skipped = 0
    for row in rows:
        if row["decision"] == "accepted_old":
            target_text = row["old_text"]
            decision_origin = "accepted_old"
        elif row["decision"] == "edited":
            target_text = row["corrected_text"]
            decision_origin = "edited"
        elif row["decision"] == "accepted":
            target_text = row["suggested_text"]
            decision_origin = "accepted"
        if is_blank(target_text):
            skipped += 1
            continue
        confidence = 1.0 if decision_origin in {"edited", "accepted_old"} else max(row["match_score"] or 0.95, 0.95)
        for source_language, source_text, origin in [
            ("spanish", row["spanish_text"], f"human_feedback_{decision_origin}_spanish"),
            ("english", row["english_text"], f"human_feedback_{decision_origin}_english"),
        ]:
            skip, _ = should_skip_pair(source_text, target_text, None)
            if skip:
                skipped += 1
                continue
            result = upsert_memory_pair(
                conn=conn,
                source_segment_id=row["segment_id"],
                source_language=source_language,
                source_text=source_text,
                target_text=target_text,
                confidence_score=confidence,
                origin=origin,
            )
            if result == "inserted":
                inserted += 1
            else:
                updated += 1

    return {"inserted": inserted, "updated": updated, "skipped": skipped}


def main() -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[build_translation_memory] Starting translation memory build")
    print(f"[build_translation_memory] Rule version: {RULE_VERSION}")
    print(f"[build_translation_memory] Database: {db.get_database_path(settings)}")
    print(f"[build_translation_memory] Minimum confidence: {MIN_CONFIDENCE}")

    processed_segments = 0
    inserted_pairs = 0
    updated_pairs = 0
    skipped_pairs = 0
    skip_counts: Counter = Counter()
    origin_counts: Counter = Counter()
    feedback_stats = {"inserted": 0, "updated": 0, "skipped": 0}

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        conn.execute(
            """
            UPDATE translation_memory
            SET usage_count = 0
            WHERE origin IN (
                'trusted_spanish_old',
                'trusted_english_old',
                'human_feedback_accepted_spanish',
                'human_feedback_accepted_english',
                'human_feedback_edited_spanish',
                'human_feedback_edited_english',
                'human_feedback_accepted_old_spanish',
                'human_feedback_accepted_old_english'
            )
            """
        )
        total = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM source_segments s
            JOIN segment_analysis a ON a.segment_id = s.id
            WHERE s.is_active = 1
              AND a.classification = 'trusted'
              AND a.confidence_score >= ?
            """,
            (MIN_CONFIDENCE,),
        ).fetchone()["total"]
        print(f"[build_translation_memory] Trusted segments eligible: {total}")

        offset = 0
        while True:
            rows = conn.execute(
                """
                SELECT
                    s.id,
                    s.spanish_text,
                    s.english_text,
                    s.old_text,
                    a.confidence_score,
                    a.reasons_json
                FROM source_segments s
                JOIN segment_analysis a ON a.segment_id = s.id
                WHERE s.is_active = 1
                  AND a.classification = 'trusted'
                  AND a.confidence_score >= ?
                ORDER BY s.id
                LIMIT ? OFFSET ?
                """,
                (MIN_CONFIDENCE, BATCH_SIZE, offset),
            ).fetchall()
            if not rows:
                break

            for row in rows:
                processed_segments += 1
                pairs = [
                    ("spanish", row["spanish_text"], "trusted_spanish_old"),
                    ("english", row["english_text"], "trusted_english_old"),
                ]
                for source_language, source_text, origin in pairs:
                    skip, reason = should_skip_pair(
                        source_text,
                        row["old_text"],
                        row["reasons_json"],
                    )
                    if skip:
                        skipped_pairs += 1
                        skip_counts[reason or "unknown"] += 1
                        continue

                    result = upsert_memory_pair(
                        conn=conn,
                        source_segment_id=row["id"],
                        source_language=source_language,
                        source_text=source_text,
                        target_text=row["old_text"],
                        confidence_score=row["confidence_score"],
                        origin=origin,
                    )
                    origin_counts[origin] += 1
                    if result == "inserted":
                        inserted_pairs += 1
                    else:
                        updated_pairs += 1

            conn.commit()
            offset += len(rows)
            if (
                processed_segments == len(rows)
                or processed_segments % (BATCH_SIZE * 2) == 0
                or processed_segments == total
            ):
                print(
                    "[build_translation_memory] "
                    f"{processed_segments}/{total} trusted segments processed "
                    f"({processed_segments / total:.1%})"
                )

        feedback_stats = build_feedback_memory(conn)
        conn.commit()

    elapsed = datetime.now() - started_at
    report_lines = [
        "Translation memory build report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Minimum confidence: {MIN_CONFIDENCE}",
        "",
        "Summary:",
        f"- Trusted segments processed: {processed_segments}",
        f"- Pairs inserted: {inserted_pairs}",
        f"- Pairs updated: {updated_pairs}",
        f"- Pairs skipped: {skipped_pairs}",
        f"- Feedback pairs inserted: {feedback_stats['inserted']}",
        f"- Feedback pairs updated: {feedback_stats['updated']}",
        f"- Feedback pairs skipped: {feedback_stats['skipped']}",
        "",
        "Origins:",
    ]
    for origin, count in origin_counts.most_common():
        report_lines.append(f"- {origin}: {count}")

    report_lines.extend(["", "Skip reasons:"])
    for reason, count in skip_counts.most_common():
        report_lines.append(f"- {reason}: {count}")

    report_path = db.write_report(settings, "build_translation_memory", report_lines)
    print(f"[build_translation_memory] Trusted segments processed: {processed_segments}")
    print(f"[build_translation_memory] Pairs inserted: {inserted_pairs}")
    print(f"[build_translation_memory] Pairs updated: {updated_pairs}")
    print(f"[build_translation_memory] Pairs skipped: {skipped_pairs}")
    print(
        "[build_translation_memory] Feedback pairs: "
        f"{feedback_stats['inserted']} inserted, "
        f"{feedback_stats['updated']} updated, "
        f"{feedback_stats['skipped']} skipped"
    )
    print(f"[build_translation_memory] Report: {report_path}")
    print("[build_translation_memory] Done")


if __name__ == "__main__":
    main()
