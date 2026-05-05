from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher

import db


RULE_VERSION = "translation_suggestions_v2"
BATCH_SIZE = 1000
TARGET_LANGUAGE = "pt-BR"
TARGET_CLASSIFICATIONS = ("review_needed", "rejected")
MIN_FUZZY_SCORE = 0.88
MAX_FUZZY_CANDIDATES = 500
LENGTH_BUCKET_SIZE = 20

PROTECTED_TOKEN_PATTERN = re.compile(
    r"\$[^$\s]+\$|\[[^\]]+\]|#[A-Za-z0-9_]+|#!|@[A-Za-z0-9_]+!|\\n"
)
WORD_PATTERN = re.compile(r"[A-Za-z\u00c0-\u00ff]+", re.UNICODE)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_for_compare(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.casefold().split())


def similarity(left: str | None, right: str | None) -> float:
    left_norm = normalize_for_compare(left)
    right_norm = normalize_for_compare(right)
    if not left_norm and not right_norm:
        return 1.0
    if not left_norm or not right_norm:
        return 0.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def is_blank(value: str | None) -> bool:
    return value is None or value.strip() == ""


def protected_tokens(value: str | None) -> Counter:
    if not value:
        return Counter()
    return Counter(PROTECTED_TOKEN_PATTERN.findall(value))


def word_count(value: str | None) -> int:
    return len(WORD_PATTERN.findall(value or ""))


def token_status(source_text: str | None, suggested_text: str | None) -> tuple[str, dict]:
    source_tokens = protected_tokens(source_text)
    suggested_tokens = protected_tokens(suggested_text)
    if source_tokens == suggested_tokens:
        return "ok", {}

    missing = list((source_tokens - suggested_tokens).elements())
    extra = list((suggested_tokens - source_tokens).elements())
    status = "missing_tokens" if missing else "extra_tokens"
    return status, {"missing": missing[:30], "extra": extra[:30]}


def suggestion_status(match_type: str, match_score: float, token_state: str) -> str:
    if token_state != "ok":
        return "blocked"
    if match_type in {"exact_spanish", "exact_english"}:
        return "safe"
    if match_score >= 0.94:
        return "safe"
    return "review"


def feedback_decision_status(decision: str | None) -> str | None:
    if decision in {"accepted", "edited", "accepted_old"}:
        return "safe"
    if decision == "rejected":
        return "blocked"
    return None


def length_bucket(value: str | None) -> int:
    return len(value or "") // LENGTH_BUCKET_SIZE


def load_memory_cache(conn):
    print("[suggest_translations] Loading translation memory cache")
    rows = conn.execute(
        """
        SELECT
            id,
            source_language,
            source_text,
            target_text,
            confidence_score,
            origin,
            usage_count
        FROM translation_memory
        WHERE target_language = ?
        ORDER BY confidence_score DESC, usage_count DESC, id ASC
        """,
        (TARGET_LANGUAGE,),
    ).fetchall()

    exact_index: dict[tuple[str, str], list[dict]] = {}
    fuzzy_index: dict[str, dict[int, list[dict]]] = {"spanish": {}, "english": {}}
    for row in rows:
        entry = {
            "id": row["id"],
            "source_language": row["source_language"],
            "source_text": row["source_text"],
            "target_text": row["target_text"],
            "confidence_score": row["confidence_score"],
            "origin": row["origin"],
            "usage_count": row["usage_count"],
        }
        exact_key = (entry["source_language"], sha256_text(entry["source_text"]))
        exact_index.setdefault(exact_key, []).append(entry)
        bucket = length_bucket(entry["source_text"])
        fuzzy_index.setdefault(entry["source_language"], {}).setdefault(bucket, []).append(entry)

    for buckets in fuzzy_index.values():
        for entries in buckets.values():
            entries.sort(
                key=lambda item: (item["usage_count"], item["confidence_score"]),
                reverse=True,
            )

    print(f"[suggest_translations] Memory cache loaded: {len(rows)} pairs")
    return exact_index, fuzzy_index


def load_feedback_cache(conn):
    print("[suggest_translations] Loading suggestion feedback cache")
    rows = conn.execute(
        """
        SELECT
            f.suggestion_id,
            f.segment_id,
            f.decision,
            f.corrected_text,
            f.reason,
            f.reviewed_at,
            ts.suggested_hash,
            ts.source_language,
            ts.origin,
            ts.match_type
        FROM suggestion_feedback f
        LEFT JOIN translation_suggestions ts ON ts.id = f.suggestion_id
        WHERE f.decision IN ('accepted', 'rejected', 'edited', 'accepted_old')
        ORDER BY f.reviewed_at ASC, f.id ASC
        """
    ).fetchall()

    by_suggestion_id = {}
    by_signature = {}
    corrected_by_segment = {}
    rejected_hashes_by_segment = {}

    for row in rows:
        feedback = {
            "decision": row["decision"],
            "corrected_text": row["corrected_text"],
            "reason": row["reason"],
            "reviewed_at": row["reviewed_at"],
        }
        if row["suggestion_id"] is not None:
            by_suggestion_id[row["suggestion_id"]] = feedback
        if row["suggested_hash"]:
            signature = (
                row["suggested_hash"],
                row["source_language"],
                row["origin"],
                row["match_type"],
            )
            by_signature[signature] = feedback
            if row["decision"] == "rejected":
                rejected_hashes_by_segment.setdefault(row["segment_id"], set()).add(row["suggested_hash"])
        if row["decision"] == "edited" and not is_blank(row["corrected_text"]):
            corrected_by_segment[row["segment_id"]] = feedback

    print(f"[suggest_translations] Feedback cache loaded: {len(rows)} reviews")
    return {
        "by_suggestion_id": by_suggestion_id,
        "by_signature": by_signature,
        "corrected_by_segment": corrected_by_segment,
        "rejected_hashes_by_segment": rejected_hashes_by_segment,
    }


def sync_pending_feedback_queue(conn) -> dict[str, int]:
    now = db.utc_now()
    deleted = conn.execute(
        """
        DELETE FROM suggestion_feedback
        WHERE decision = 'pending'
        """
    ).rowcount

    inserted = conn.execute(
        """
        INSERT INTO suggestion_feedback (
            suggestion_id,
            segment_id,
            decision,
            suggested_text,
            corrected_text,
            reason,
            reviewer,
            reviewed_at,
            created_at,
            updated_at
        )
        SELECT
            ts.id,
            ts.segment_id,
            'pending',
            ts.suggested_text,
            NULL,
            NULL,
            NULL,
            ?,
            ?,
            ?
        FROM translation_suggestions ts
        WHERE ts.status != 'stale'
          AND NOT EXISTS (
              SELECT 1
              FROM suggestion_feedback f
              WHERE f.suggestion_id = ts.id
                AND f.decision IN ('accepted', 'rejected', 'edited', 'accepted_old')
          )
        """,
        (now, now, now),
    ).rowcount

    return {"deleted_pending": deleted, "inserted_pending": inserted}


def memory_exact(exact_index, source_language: str, source_text: str | None):
    if is_blank(source_text):
        return []
    return exact_index.get((source_language, sha256_text(source_text or "")), [])[:10]


def memory_fuzzy(fuzzy_index, source_language: str, source_text: str | None):
    if is_blank(source_text) or word_count(source_text) < 4:
        return []

    text_len = len(source_text or "")
    min_bucket = max(0, (text_len - 60) // LENGTH_BUCKET_SIZE)
    max_bucket = (text_len + 60) // LENGTH_BUCKET_SIZE
    candidates = []
    buckets = fuzzy_index.get(source_language, {})
    for bucket in range(min_bucket, max_bucket + 1):
        candidates.extend(buckets.get(bucket, [])[:MAX_FUZZY_CANDIDATES])
    candidates.sort(
        key=lambda item: (item["usage_count"], item["confidence_score"]),
        reverse=True,
    )
    candidates = candidates[:MAX_FUZZY_CANDIDATES]

    scored = []
    for row in candidates:
        score = similarity(source_text, row["source_text"])
        if score >= MIN_FUZZY_SCORE:
            scored.append((score, row))
    scored.sort(key=lambda item: (item[0], item[1]["confidence_score"], item[1]["usage_count"]), reverse=True)
    return scored[:5]


def upsert_suggestion(
    conn,
    segment_id: int,
    suggested_text: str,
    source_language: str,
    origin: str,
    match_type: str,
    match_score: float,
    token_state: str,
    status: str,
    reasons: list[dict],
) -> str:
    suggested_hash = sha256_text(suggested_text)
    now = db.utc_now()
    existing = conn.execute(
        """
        SELECT id
        FROM translation_suggestions
        WHERE segment_id = ?
          AND suggested_hash = ?
          AND source_language = ?
          AND origin = ?
          AND match_type = ?
        LIMIT 1
        """,
        (segment_id, suggested_hash, source_language, origin, match_type),
    ).fetchone()

    payload = json.dumps(reasons, ensure_ascii=False)
    if existing:
        conn.execute(
            """
            UPDATE translation_suggestions
            SET
                suggested_text = ?,
                match_score = ?,
                token_status = ?,
                status = ?,
                reasons_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                suggested_text,
                match_score,
                token_state,
                status,
                payload,
                now,
                existing["id"],
            ),
        )
        return "updated"

    conn.execute(
        """
        INSERT INTO translation_suggestions (
            segment_id,
            suggested_text,
            suggested_hash,
            source_language,
            origin,
            match_type,
            match_score,
            token_status,
            status,
            reasons_json,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            segment_id,
            suggested_text,
            suggested_hash,
            source_language,
            origin,
            match_type,
            match_score,
            token_state,
            status,
            payload,
            now,
            now,
        ),
    )
    return "inserted"


def apply_feedback_to_suggestion(
    row,
    suggested_text: str,
    source_language: str,
    origin: str,
    match_type: str,
    match_score: float,
    token_state: str,
    status: str,
    reasons: list[dict],
    feedback_cache: dict,
) -> tuple[str, float, list[dict]]:
    suggested_hash = sha256_text(suggested_text)
    signature = (suggested_hash, source_language, origin, match_type)
    feedback = feedback_cache["by_signature"].get(signature)

    if suggested_hash in feedback_cache["rejected_hashes_by_segment"].get(row["id"], set()):
        status = "blocked"
        match_score = min(match_score, 0.2)
        reasons.append(
            {
                "rule": "feedback_rejected_for_segment",
                "message": "This same suggested text was rejected for this segment.",
            }
        )
        return status, match_score, reasons

    if feedback:
        decision_status = feedback_decision_status(feedback["decision"])
        if decision_status:
            status = decision_status
        if feedback["decision"] in {"accepted", "edited"}:
            match_score = min(1.0, match_score + 0.05)
        elif feedback["decision"] == "rejected":
            match_score = min(match_score, 0.35)
        reasons.append(
            {
                "rule": "feedback_signature",
                "decision": feedback["decision"],
                "reviewed_at": feedback["reviewed_at"],
                "message": "Prior human feedback was found for this suggestion signature.",
            }
        )

    if token_state != "ok":
        status = "blocked"

    return status, match_score, reasons


def add_suggestion_from_memory(conn, row, memory_row, match_type: str, match_score: float, feedback_cache: dict) -> str:
    token_state, token_details = token_status(row["spanish_text"], memory_row["target_text"])
    status = suggestion_status(match_type, match_score, token_state)
    reasons = [
        {
            "rule": match_type,
            "memory_id": memory_row["id"],
            "memory_confidence": memory_row["confidence_score"],
            "memory_usage_count": memory_row["usage_count"],
            "message": "Suggestion generated from trusted translation memory.",
        }
    ]
    if token_details:
        reasons.append(
            {
                "rule": "token_validation",
                "token_status": token_state,
                **token_details,
                "message": "Suggested text does not preserve Spanish source protected tokens.",
            }
        )

    status, match_score, reasons = apply_feedback_to_suggestion(
        row=row,
        suggested_text=memory_row["target_text"],
        source_language=memory_row["source_language"],
        origin=memory_row["origin"],
        match_type=match_type,
        match_score=match_score,
        token_state=token_state,
        status=status,
        reasons=reasons,
        feedback_cache=feedback_cache,
    )

    return upsert_suggestion(
        conn=conn,
        segment_id=row["id"],
        suggested_text=memory_row["target_text"],
        source_language=memory_row["source_language"],
        origin=memory_row["origin"],
        match_type=match_type,
        match_score=match_score,
        token_state=token_state,
        status=status,
        reasons=reasons,
    )


def add_corrected_feedback_suggestion(conn, row, feedback: dict) -> str:
    suggested_text = feedback["corrected_text"]
    token_state, token_details = token_status(row["spanish_text"], suggested_text)
    status = "safe" if token_state == "ok" else "blocked"
    reasons = [
        {
            "rule": "feedback_corrected_text",
            "decision": feedback["decision"],
            "reviewed_at": feedback["reviewed_at"],
            "message": "Suggestion generated from human-corrected feedback.",
        }
    ]
    if token_details:
        reasons.append(
            {
                "rule": "token_validation",
                "token_status": token_state,
                **token_details,
                "message": "Corrected feedback text does not preserve Spanish source protected tokens.",
            }
        )

    return upsert_suggestion(
        conn=conn,
        segment_id=row["id"],
        suggested_text=suggested_text,
        source_language="feedback",
        origin="human_feedback",
        match_type="feedback_corrected",
        match_score=1.0 if token_state == "ok" else 0.0,
        token_state=token_state,
        status=status,
        reasons=reasons,
    )


def main() -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[suggest_translations] Starting suggestion generation")
    print(f"[suggest_translations] Rule version: {RULE_VERSION}")
    print(f"[suggest_translations] Database: {db.get_database_path(settings)}")
    print(f"[suggest_translations] Target classifications: {', '.join(TARGET_CLASSIFICATIONS)}")

    processed_segments = 0
    segments_with_suggestions = 0
    inserted = 0
    updated = 0
    no_match = 0
    status_counts: Counter = Counter()
    match_counts: Counter = Counter()
    feedback_queue_stats = {"deleted_pending": 0, "inserted_pending": 0}

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        print("[suggest_translations] Marking previous suggestions as stale")
        conn.execute(
            """
            UPDATE translation_suggestions
            SET status = 'stale',
                updated_at = ?
            WHERE status != 'stale'
            """,
            (db.utc_now(),),
        )
        print("[suggest_translations] Previous suggestions marked as stale")
        exact_index, fuzzy_index = load_memory_cache(conn)
        feedback_cache = load_feedback_cache(conn)
        placeholders = ", ".join("?" for _ in TARGET_CLASSIFICATIONS)
        total = conn.execute(
            f"""
            SELECT COUNT(*) AS total
            FROM source_segments s
            JOIN segment_analysis a ON a.segment_id = s.id
            WHERE s.is_active = 1
              AND a.classification IN ({placeholders})
            """,
            TARGET_CLASSIFICATIONS,
        ).fetchone()["total"]
        print(f"[suggest_translations] Segments to inspect: {total}")

        offset = 0
        while True:
            rows = conn.execute(
                f"""
                SELECT
                    s.id,
                    s.relative_path,
                    s.source_line_number,
                    s.source_key,
                    s.spanish_text,
                    s.english_text,
                    s.old_text,
                    a.confidence_score,
                    a.classification
                FROM source_segments s
                JOIN segment_analysis a ON a.segment_id = s.id
                WHERE s.is_active = 1
                  AND a.classification IN ({placeholders})
                ORDER BY s.id
                LIMIT ? OFFSET ?
                """,
                (*TARGET_CLASSIFICATIONS, BATCH_SIZE, offset),
            ).fetchall()
            if not rows:
                break

            for row in rows:
                processed_segments += 1
                segment_suggestions = 0

                corrected_feedback = feedback_cache["corrected_by_segment"].get(row["id"])
                if corrected_feedback:
                    result = add_corrected_feedback_suggestion(conn, row, corrected_feedback)
                    inserted += 1 if result == "inserted" else 0
                    updated += 1 if result == "updated" else 0
                    segment_suggestions += 1
                    match_counts["feedback_corrected"] += 1

                for memory_row in memory_exact(exact_index, "spanish", row["spanish_text"]):
                    result = add_suggestion_from_memory(
                        conn, row, memory_row, "exact_spanish", 1.0, feedback_cache
                    )
                    inserted += 1 if result == "inserted" else 0
                    updated += 1 if result == "updated" else 0
                    segment_suggestions += 1
                    match_counts["exact_spanish"] += 1

                for memory_row in memory_exact(exact_index, "english", row["english_text"]):
                    result = add_suggestion_from_memory(
                        conn, row, memory_row, "exact_english", 1.0, feedback_cache
                    )
                    inserted += 1 if result == "inserted" else 0
                    updated += 1 if result == "updated" else 0
                    segment_suggestions += 1
                    match_counts["exact_english"] += 1

                if segment_suggestions == 0:
                    for score, memory_row in memory_fuzzy(fuzzy_index, "spanish", row["spanish_text"]):
                        result = add_suggestion_from_memory(
                            conn, row, memory_row, "fuzzy_spanish", score, feedback_cache
                        )
                        inserted += 1 if result == "inserted" else 0
                        updated += 1 if result == "updated" else 0
                        segment_suggestions += 1
                        match_counts["fuzzy_spanish"] += 1

                if segment_suggestions == 0:
                    for score, memory_row in memory_fuzzy(fuzzy_index, "english", row["english_text"]):
                        result = add_suggestion_from_memory(
                            conn, row, memory_row, "fuzzy_english", score, feedback_cache
                        )
                        inserted += 1 if result == "inserted" else 0
                        updated += 1 if result == "updated" else 0
                        segment_suggestions += 1
                        match_counts["fuzzy_english"] += 1

                if segment_suggestions:
                    segments_with_suggestions += 1
                else:
                    no_match += 1

                if processed_segments % 100 == 0:
                    print(
                        "[suggest_translations] "
                        f"{processed_segments}/{total} segments inspected "
                        f"({processed_segments / total:.1%})"
                    )

            conn.commit()
            offset += len(rows)
            if (
                processed_segments == len(rows)
                or processed_segments % (BATCH_SIZE * 2) == 0
                or processed_segments == total
            ):
                print(
                    "[suggest_translations] "
                    f"{processed_segments}/{total} segments inspected "
                    f"({processed_segments / total:.1%})"
                )

        status_rows = conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM translation_suggestions
            GROUP BY status
            ORDER BY status
            """
        ).fetchall()
        for row in status_rows:
            status_counts[row["status"]] = row["count"]

        feedback_queue_stats = sync_pending_feedback_queue(conn)
        conn.commit()

    elapsed = datetime.now() - started_at
    report_lines = [
        "Translation suggestion report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        "",
        "Summary:",
        f"- Segments inspected: {processed_segments}",
        f"- Segments with suggestions: {segments_with_suggestions}",
        f"- Segments without match: {no_match}",
        f"- Suggestions inserted: {inserted}",
        f"- Suggestions updated: {updated}",
        f"- Pending feedback deleted/rebuilt: {feedback_queue_stats['deleted_pending']}",
        f"- Pending feedback inserted: {feedback_queue_stats['inserted_pending']}",
        "",
        "Match types generated this run:",
    ]
    for match_type, count in match_counts.most_common():
        report_lines.append(f"- {match_type}: {count}")

    report_lines.extend(["", "Suggestion table status totals:"])
    for status, count in sorted(status_counts.items()):
        report_lines.append(f"- {status}: {count}")

    report_path = db.write_report(settings, "suggest_translations", report_lines)
    print(f"[suggest_translations] Segments inspected: {processed_segments}")
    print(f"[suggest_translations] Segments with suggestions: {segments_with_suggestions}")
    print(f"[suggest_translations] Segments without match: {no_match}")
    print(f"[suggest_translations] Suggestions inserted: {inserted}")
    print(f"[suggest_translations] Suggestions updated: {updated}")
    print(
        "[suggest_translations] Pending feedback rebuilt: "
        f"{feedback_queue_stats['deleted_pending']} deleted, "
        f"{feedback_queue_stats['inserted_pending']} inserted"
    )
    for status, count in sorted(status_counts.items()):
        print(f"[suggest_translations] {status}: {count}")
    print(f"[suggest_translations] Report: {report_path}")
    print("[suggest_translations] Done")


if __name__ == "__main__":
    main()
