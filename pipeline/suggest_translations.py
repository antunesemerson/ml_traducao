from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher

import db


RULE_VERSION = "translation_suggestions_v6"
BATCH_SIZE = 1000
TARGET_LANGUAGE = "pt-BR"
TARGET_CLASSIFICATIONS = ("review_needed", "rejected")
MIN_FUZZY_SCORE = 0.88
MAX_FUZZY_CANDIDATES = 500
LENGTH_BUCKET_SIZE = 20
REVIEWED_DECISIONS = ("accepted", "rejected", "edited", "accepted_old")

PROTECTED_TOKEN_PATTERN = re.compile(
    r"\$[^$\s]+\$|\[[^\]]+\]|#[A-Za-z0-9_]+|#!|@[A-Za-z0-9_]+!|\\n"
)
STRING_LITERAL_PATTERN = re.compile(r"'[^']*'|\"[^\"]*\"")
WORD_PATTERN = re.compile(r"[A-Za-z\u00c0-\u00ff]+", re.UNICODE)
PERSISTENT_SPANISH_RESIDUES = {
    "cortesano": "cortes\u00e3o",
    "cortesanos": "cortes\u00f5es",
    "gobernante": "governante",
    "gobernantes": "governantes",
    "hacendado": "propriet\u00e1rio de terras",
    "hacendados": "propriet\u00e1rios de terras",
    "invitado": "convidado",
    "invitados": "convidados",
    "nueva": "nova",
    "nuevas": "novas",
    "decisiones": "decis\u00f5es",
    "decisi\u00f3n": "decis\u00e3o",
    "rechaza": "rejeita",
    "rechazar": "rejeitar",
    "situaci\u00f3n": "situa\u00e7\u00e3o",
    "situaciones": "situa\u00e7\u00f5es",
}
MISSING_SPACE_AFTER_TOKEN_PATTERN = re.compile(r"(\]|\$[A-Za-z0-9_]+\$)(?=[A-Za-z\u00c0-\u00ff])")
SPANISH_ANGULAR_QUOTE_MARKS = ("«", "»", "Â«", "Â»")


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
    return Counter(normalize_protected_token(token) for token in PROTECTED_TOKEN_PATTERN.findall(value))


def normalize_protected_token(token: str) -> str:
    if not (token.startswith("[") and token.endswith("]")):
        return token

    command_name = token[1:].split("(", 1)[0].split("|", 1)[0].strip()
    base_name = command_name.split(".")[-1]

    if base_name == "Concept":
        seen = 0

        def replace_concept_literal(match: re.Match) -> str:
            nonlocal seen
            seen += 1
            if seen == 1:
                return match.group(0)
            return "'<TEXT>'"

        return STRING_LITERAL_PATTERN.sub(replace_concept_literal, token)

    if base_name in {
        "Select_CString",
        "SelectLocalization",
        "LocalPlayerString",
        "PlayerString",
        "GetString",
    } or base_name.startswith("SelectLocalization") or base_name.endswith("String"):
        return STRING_LITERAL_PATTERN.sub("'<TEXT>'", token)

    return token


def word_count(value: str | None) -> int:
    return len(WORD_PATTERN.findall(value or ""))


def apply_persistent_residue_replacements(value: str | None) -> str | None:
    if value is None:
        return None
    updated = value
    for spanish_term, portuguese_term in PERSISTENT_SPANISH_RESIDUES.items():
        updated = re.sub(
            rf"\b{re.escape(spanish_term)}\b",
            portuguese_term,
            updated,
            flags=re.IGNORECASE,
        )
    return updated


def has_persistent_residue(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.casefold()
    return any(re.search(rf"\b{re.escape(term)}\b", normalized) for term in PERSISTENT_SPANISH_RESIDUES)


def apply_spacing_replacements(value: str | None) -> str | None:
    if value is None:
        return None
    return MISSING_SPACE_AFTER_TOKEN_PATTERN.sub(r"\1 ", value)


def has_missing_space_after_token(value: str | None) -> bool:
    if not value:
        return False
    return bool(MISSING_SPACE_AFTER_TOKEN_PATTERN.search(value))


def remove_spanish_angular_quotes(value: str | None) -> str | None:
    if value is None:
        return None
    updated = value
    for mark in SPANISH_ANGULAR_QUOTE_MARKS:
        updated = updated.replace(mark, "")
    return updated


def has_spanish_angular_quotes(value: str | None) -> bool:
    if not value:
        return False
    return any(mark in value for mark in SPANISH_ANGULAR_QUOTE_MARKS)


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


def load_memory_cache(conn, enable_fuzzy: bool, max_fuzzy_candidates: int):
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
        if enable_fuzzy:
            bucket = length_bucket(entry["source_text"])
            fuzzy_index.setdefault(entry["source_language"], {}).setdefault(bucket, []).append(entry)

    if enable_fuzzy:
        for buckets in fuzzy_index.values():
            for entries in buckets.values():
                entries.sort(
                    key=lambda item: (item["usage_count"], item["confidence_score"]),
                    reverse=True,
                )
                del entries[max_fuzzy_candidates:]

    print(f"[suggest_translations] Memory cache loaded: {len(rows)} pairs")
    print(f"[suggest_translations] Fuzzy matching: {'enabled' if enable_fuzzy else 'disabled'}")
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
            if row["decision"] in {"accepted", "edited", "accepted_old"}:
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
    reviewed_placeholders = ", ".join("?" for _ in REVIEWED_DECISIONS)
    deleted = conn.execute(
        f"""
        DELETE FROM suggestion_feedback
        WHERE decision = 'pending'
          AND segment_id IN (
              SELECT DISTINCT segment_id
              FROM suggestion_feedback
              WHERE decision IN ('accepted', 'edited', 'accepted_old')
          )
        """,
    ).rowcount

    deleted += conn.execute(
        """
        DELETE FROM suggestion_feedback
        WHERE decision = 'pending'
        """
    ).rowcount

    inserted = conn.execute(
        f"""
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
        WHERE ts.status IN ('safe', 'review')
          AND NOT EXISTS (
              SELECT 1
              FROM suggestion_feedback f
              WHERE f.suggestion_id = ts.id
                AND f.decision IN ({reviewed_placeholders})
          )
          AND NOT EXISTS (
              SELECT 1
              FROM suggestion_feedback fs
              WHERE fs.segment_id = ts.segment_id
                AND fs.decision IN ('accepted', 'edited', 'accepted_old')
          )
        """,
        (now, now, now, *REVIEWED_DECISIONS),
    ).rowcount

    return {"deleted_pending": deleted, "inserted_pending": inserted}


def memory_exact(exact_index, source_language: str, source_text: str | None):
    if is_blank(source_text):
        return []
    return exact_index.get((source_language, sha256_text(source_text or "")), [])[:10]


def memory_fuzzy(fuzzy_index, source_language: str, source_text: str | None, max_fuzzy_candidates: int):
    if is_blank(source_text) or word_count(source_text) < 4:
        return []

    text_len = len(source_text or "")
    min_bucket = max(0, (text_len - 60) // LENGTH_BUCKET_SIZE)
    max_bucket = (text_len + 60) // LENGTH_BUCKET_SIZE
    candidates = []
    buckets = fuzzy_index.get(source_language, {})
    for bucket in range(min_bucket, max_bucket + 1):
        candidates.extend(buckets.get(bucket, [])[:max_fuzzy_candidates])
    candidates.sort(
        key=lambda item: (item["usage_count"], item["confidence_score"]),
        reverse=True,
    )
    candidates = candidates[:max_fuzzy_candidates]

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


def add_suggestion_from_memory(
    conn,
    row,
    memory_row,
    match_type: str,
    match_score: float,
    feedback_cache: dict,
) -> dict:
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

    result = upsert_suggestion(
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
    return {"result": result, "status": status}


def add_corrected_feedback_suggestion(conn, row, feedback: dict) -> dict:
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

    result = upsert_suggestion(
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
    return {"result": result, "status": status}


def add_persistent_residue_suggestion(conn, row) -> dict | None:
    base_text = row["old_text"]
    source_language = "rule"
    if is_blank(base_text) and has_persistent_residue(row["spanish_text"]):
        base_text = row["spanish_text"]
        source_language = "spanish_rule"

    if not has_persistent_residue(base_text):
        return None
    suggested_text = apply_persistent_residue_replacements(base_text)
    if suggested_text == base_text:
        return None
    token_state, token_details = token_status(row["spanish_text"], suggested_text)
    status = "safe" if token_state == "ok" else "blocked"
    reasons = [
        {
            "rule": "persistent_spanish_residue_replacement",
            "replacements": PERSISTENT_SPANISH_RESIDUES,
            "message": "Suggestion generated by replacing known persistent Spanish residue in old_text.",
        }
    ]
    if token_details:
        reasons.append(
            {
                "rule": "token_validation",
                "token_status": token_state,
                **token_details,
                "message": "Persistent residue suggestion does not preserve Spanish source protected tokens.",
            }
        )

    result = upsert_suggestion(
        conn=conn,
        segment_id=row["id"],
        suggested_text=suggested_text,
        source_language=source_language,
        origin="persistent_residue_rule",
        match_type="persistent_residue",
        match_score=0.98 if token_state == "ok" else 0.0,
        token_state=token_state,
        status=status,
        reasons=reasons,
    )
    return {"result": result, "status": status}


def add_spacing_suggestion(conn, row) -> dict | None:
    if not has_missing_space_after_token(row["old_text"]):
        return None
    suggested_text = apply_spacing_replacements(row["old_text"])
    if suggested_text == row["old_text"]:
        return None
    token_state, token_details = token_status(row["spanish_text"], suggested_text)
    status = "review" if token_state == "ok" else "blocked"
    reasons = [
        {
            "rule": "missing_space_after_token_replacement",
            "message": "Suggestion generated by adding whitespace after protected tokens glued to text.",
        }
    ]
    if token_details:
        reasons.append(
            {
                "rule": "token_validation",
                "token_status": token_state,
                **token_details,
                "message": "Spacing suggestion does not preserve Spanish source protected tokens.",
            }
        )

    result = upsert_suggestion(
        conn=conn,
        segment_id=row["id"],
        suggested_text=suggested_text,
        source_language="rule",
        origin="formatting_rule",
        match_type="missing_space_after_token",
        match_score=0.86 if token_state == "ok" else 0.0,
        token_state=token_state,
        status=status,
        reasons=reasons,
    )
    return {"result": result, "status": status}


def add_angular_quotes_suggestion(conn, row) -> dict | None:
    base_text = row["old_text"]
    source_language = "rule"
    if is_blank(base_text) and has_spanish_angular_quotes(row["spanish_text"]):
        base_text = row["spanish_text"]
        source_language = "spanish_rule"

    if not has_spanish_angular_quotes(base_text):
        return None
    suggested_text = remove_spanish_angular_quotes(base_text)
    if suggested_text == base_text:
        return None
    token_state, token_details = token_status(row["spanish_text"], suggested_text)
    status = "safe" if token_state == "ok" else "blocked"
    reasons = [
        {
            "rule": "spanish_angular_quotes_replacement",
            "message": "Suggestion generated by removing Spanish-style angular quotation marks from pt-BR output.",
        }
    ]
    if token_details:
        reasons.append(
            {
                "rule": "token_validation",
                "token_status": token_state,
                **token_details,
                "message": "Angular quote suggestion does not preserve Spanish source protected tokens.",
            }
        )

    result = upsert_suggestion(
        conn=conn,
        segment_id=row["id"],
        suggested_text=suggested_text,
        source_language=source_language,
        origin="punctuation_rule",
        match_type="spanish_angular_quotes",
        match_score=0.96 if token_state == "ok" else 0.0,
        token_state=token_state,
        status=status,
        reasons=reasons,
    )
    return {"result": result, "status": status}


def main() -> None:
    settings = db.load_settings()
    suggestion_settings = settings.get("suggestions", {})
    enable_fuzzy = bool(suggestion_settings.get("enable_fuzzy", False))
    max_fuzzy_candidates = int(suggestion_settings.get("max_fuzzy_candidates", MAX_FUZZY_CANDIDATES))
    started_at = datetime.now()
    print("[suggest_translations] Starting suggestion generation")
    print(f"[suggest_translations] Rule version: {RULE_VERSION}")
    print(f"[suggest_translations] Database: {db.get_database_path(settings)}")
    print(f"[suggest_translations] Target classifications: {', '.join(TARGET_CLASSIFICATIONS)}")
    print(f"[suggest_translations] Fuzzy enabled: {enable_fuzzy}")
    print(f"[suggest_translations] Max fuzzy candidates: {max_fuzzy_candidates}")

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
        exact_index, fuzzy_index = load_memory_cache(conn, enable_fuzzy, max_fuzzy_candidates)
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
                viable_segment_suggestions = 0

                corrected_feedback = feedback_cache["corrected_by_segment"].get(row["id"])
                if corrected_feedback:
                    suggestion = add_corrected_feedback_suggestion(conn, row, corrected_feedback)
                    result = suggestion["result"]
                    inserted += 1 if result == "inserted" else 0
                    updated += 1 if result == "updated" else 0
                    segment_suggestions += 1
                    viable_segment_suggestions += 1 if suggestion["status"] in {"safe", "review"} else 0
                    match_counts["feedback_corrected"] += 1

                residue_result = add_persistent_residue_suggestion(conn, row)
                if residue_result:
                    result = residue_result["result"]
                    inserted += 1 if result == "inserted" else 0
                    updated += 1 if result == "updated" else 0
                    segment_suggestions += 1
                    viable_segment_suggestions += 1 if residue_result["status"] in {"safe", "review"} else 0
                    match_counts["persistent_residue"] += 1

                spacing_result = add_spacing_suggestion(conn, row)
                if spacing_result:
                    result = spacing_result["result"]
                    inserted += 1 if result == "inserted" else 0
                    updated += 1 if result == "updated" else 0
                    segment_suggestions += 1
                    viable_segment_suggestions += 1 if spacing_result["status"] in {"safe", "review"} else 0
                    match_counts["missing_space_after_token"] += 1

                angular_quotes_result = add_angular_quotes_suggestion(conn, row)
                if angular_quotes_result:
                    result = angular_quotes_result["result"]
                    inserted += 1 if result == "inserted" else 0
                    updated += 1 if result == "updated" else 0
                    segment_suggestions += 1
                    viable_segment_suggestions += 1 if angular_quotes_result["status"] in {"safe", "review"} else 0
                    match_counts["spanish_angular_quotes"] += 1

                for memory_row in memory_exact(exact_index, "spanish", row["spanish_text"]):
                    suggestion = add_suggestion_from_memory(
                        conn, row, memory_row, "exact_spanish", 1.0, feedback_cache
                    )
                    result = suggestion["result"]
                    inserted += 1 if result == "inserted" else 0
                    updated += 1 if result == "updated" else 0
                    segment_suggestions += 1
                    viable_segment_suggestions += 1 if suggestion["status"] in {"safe", "review"} else 0
                    match_counts["exact_spanish"] += 1

                for memory_row in memory_exact(exact_index, "english", row["english_text"]):
                    suggestion = add_suggestion_from_memory(
                        conn, row, memory_row, "exact_english", 1.0, feedback_cache
                    )
                    result = suggestion["result"]
                    inserted += 1 if result == "inserted" else 0
                    updated += 1 if result == "updated" else 0
                    segment_suggestions += 1
                    viable_segment_suggestions += 1 if suggestion["status"] in {"safe", "review"} else 0
                    match_counts["exact_english"] += 1

                if enable_fuzzy and viable_segment_suggestions == 0:
                    for score, memory_row in memory_fuzzy(
                        fuzzy_index,
                        "spanish",
                        row["spanish_text"],
                        max_fuzzy_candidates,
                    ):
                        suggestion = add_suggestion_from_memory(
                            conn, row, memory_row, "fuzzy_spanish", score, feedback_cache
                        )
                        result = suggestion["result"]
                        inserted += 1 if result == "inserted" else 0
                        updated += 1 if result == "updated" else 0
                        segment_suggestions += 1
                        viable_segment_suggestions += 1 if suggestion["status"] in {"safe", "review"} else 0
                        match_counts["fuzzy_spanish"] += 1

                if enable_fuzzy and viable_segment_suggestions == 0:
                    for score, memory_row in memory_fuzzy(
                        fuzzy_index,
                        "english",
                        row["english_text"],
                        max_fuzzy_candidates,
                    ):
                        suggestion = add_suggestion_from_memory(
                            conn, row, memory_row, "fuzzy_english", score, feedback_cache
                        )
                        result = suggestion["result"]
                        inserted += 1 if result == "inserted" else 0
                        updated += 1 if result == "updated" else 0
                        segment_suggestions += 1
                        viable_segment_suggestions += 1 if suggestion["status"] in {"safe", "review"} else 0
                        match_counts["fuzzy_english"] += 1

                if viable_segment_suggestions:
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
        f"Fuzzy enabled: {enable_fuzzy}",
        f"Max fuzzy candidates: {max_fuzzy_candidates}",
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
