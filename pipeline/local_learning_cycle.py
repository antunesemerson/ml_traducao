from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime

import db
import local_quality_validator


RULE_VERSION = "local_learning_cycle_v2"
HUMAN_LETTER_PATTERN = re.compile(r"[A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u00ff]")
REVIEW_LIGHT_MARKER_PATTERN = re.compile(
    r"(Concept\(|Select_CString|SelectLocalization|Custom\(\s*['\"]ES_|#help|#weak|¿|¡|«|»)",
    re.IGNORECASE,
)
REVIEW_LIGHT_TERMS = {
    "cortesanos",
    "situaciones",
    "decisiones",
    "rechaza",
    "rechazar",
    "rechazado",
    "gobernantes",
    "invitados",
    "consejo",
    "consejero",
    "consejera",
    "señorío",
    "señorio",
    "condados",
    "heredero",
    "heredera",
}
FOCUS_GROUPS = {
    "all": [],
    "core": [
        "core_l_spanish.yml",
        "game_concepts_l_spanish.yml",
        "general_tooltips_l_spanish.yml",
        "tooltip_structs_l_spanish.yml",
        "important_actions_l_spanish.yml",
        "suggestions_l_spanish.yml",
        "messages_l_spanish.yml",
        "message_filters_l_spanish.yml",
        "message_group_types_l_spanish.yml",
        "settings_l_spanish.yml",
        "keyboard_l_spanish.yml",
        "character_l_spanish.yml",
        "council_l_spanish.yml",
        "council_tasks_l_spanish.yml",
        "court_positions_l_spanish.yml",
        "court_positions_2_l_spanish.yml",
        "royal_court_window_l_spanish.yml",
        "decisions_l_spanish.yml",
        "decision_group_types_l_spanish.yml",
        "situation_group_types_l_spanish.yml",
        "situations/%",
    ],
    "titles": [
        "titles_l_spanish.yml",
        "titles_cultural_names_l_spanish.yml",
        "nicknames_l_spanish.yml",
        "historical_characters_l_spanish.yml",
        "house_%_l_spanish.yml",
        "dynasties/%",
    ],
    "world": [
        "culture/%",
        "religion/%",
        "government_l_spanish.yml",
        "laws_l_spanish.yml",
        "succession_laws_l_spanish.yml",
        "holdings_l_spanish.yml",
        "buildings_l_spanish.yml",
    ],
    "ui": [
        "gui/%",
        "%window%_l_spanish.yml",
        "map_items_l_spanish.yml",
        "my_realm_window_l_spanish.yml",
        "schemes_l_spanish.yml",
        "interactions_l_spanish.yml",
    ],
    "events": [
        "event_localization/%",
        "%events%_l_spanish.yml",
        "story_cycles/%",
    ],
}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.casefold().split())


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def is_meaningful_positive_text(value: str | None) -> bool:
    if not value:
        return False
    text = value.strip()
    if len(text) < 3:
        return False
    return bool(HUMAN_LETTER_PATTERN.search(text))


def focus_clause(focus_group: str, table_alias: str = "s") -> tuple[str, list[str]]:
    patterns = FOCUS_GROUPS.get(focus_group, [])
    if not patterns:
        return "", []
    clauses = [f"{table_alias}.relative_path LIKE ?" for _ in patterns]
    return " AND (" + " OR ".join(clauses) + ")", patterns


def has_spanish_residue(value: str | None) -> bool:
    validation = local_quality_validator.validate_text(value)
    return any(
        issue["code"] in {"spanish_punctuation", "spanish_residue", "spanish_residue_in_literal"}
        for issue in validation["issues"]
    )


def load_pattern_weights(conn) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT pattern_key, weight_adjustment
        FROM local_learning_pattern_stats
        WHERE total_count > 0
        """
    ).fetchall()
    return {row["pattern_key"]: float(row["weight_adjustment"] or 0) for row in rows}


def candidate_pattern_keys(row, validation: dict) -> list[str]:
    keys = [
        f"origin:{row['origin'] or 'unknown'}",
        f"match_type:{row['match_type'] or 'unknown'}",
        f"suggestion_status:{row['suggestion_status'] or 'unknown'}",
        f"token_status:{row['token_status'] or 'unknown'}",
        f"source_language:{row['source_language'] or 'unknown'}",
        (
            "combo:"
            f"{row['origin'] or 'unknown'}|"
            f"{row['match_type'] or 'unknown'}|"
            f"{row['suggestion_status'] or 'unknown'}"
        ),
    ]
    words = int(validation["word_count"] or 0)
    if words >= 70:
        keys.append("length:long")
    elif words >= 30:
        keys.append("length:medium")
    else:
        keys.append("length:short")
    for issue in validation["issues"]:
        keys.append(f"validator_issue:{issue['code']}")
    return sorted(set(keys))


def learned_adjustment(row, validation: dict, pattern_weights: dict[str, float]) -> tuple[float, list[str]]:
    keys = candidate_pattern_keys(row, validation)
    adjustments = [(key, pattern_weights[key]) for key in keys if key in pattern_weights]
    if not adjustments:
        return 0.0, []
    total = sum(value for _, value in adjustments)
    total = max(-0.25, min(0.20, total))
    reasons = [f"learned:{key}:{value:.3f}" for key, value in adjustments if abs(value) >= 0.001]
    return total, reasons


def score_candidate(row, pattern_weights: dict[str, float]) -> tuple[float, list[str], str]:
    reasons: list[str] = []
    validation = local_quality_validator.validate_text(row["suggested_text"])
    is_positive_queue = row["queue_source"] == "positive"
    is_review_light_queue = row["queue_source"] == "review-light"
    score = 0.55 if is_positive_queue else 0.42 if is_review_light_queue else 0.25
    if is_positive_queue:
        reasons.append("queue_source:positive")
    elif is_review_light_queue:
        reasons.append("queue_source:review_light")

    suggestion_status = row["suggestion_status"] or ""
    if suggestion_status == "safe":
        score += 0.22
        reasons.append("suggestion_status:safe")
    elif suggestion_status == "review":
        score += 0.08
        reasons.append("suggestion_status:review")
    elif suggestion_status == "blocked":
        score -= 0.28
        reasons.append("suggestion_status:blocked")

    token_status = row["token_status"] or ""
    if token_status == "ok":
        score += 0.20
        reasons.append("token_status:ok")
    else:
        score -= 0.35
        reasons.append(f"token_status:{token_status or 'unknown'}")

    match_score = float(row["match_score"] or 0)
    score += min(match_score, 1.0) * 0.18
    reasons.append(f"match_score:{match_score:.3f}")

    origin = row["origin"] or ""
    match_type = row["match_type"] or ""
    if origin == "positive_core_sample":
        score += 0.12
        reasons.append("positive_core_sample")
    elif origin in {"formatting_rule", "punctuation_rule"}:
        score += 0.25
        reasons.append(f"deterministic_origin:{origin}")
    elif origin == "persistent_residue_rule":
        score += 0.18
        reasons.append("persistent_residue_rule")
    elif origin.startswith("human_feedback_edited"):
        score += 0.18
        reasons.append(f"human_memory:{origin}")
    elif origin.startswith("human_feedback_accepted"):
        score += 0.12
        reasons.append(f"human_memory:{origin}")
    elif origin.startswith("trusted_"):
        score -= 0.05
        reasons.append(f"trusted_memory:{origin}")

    if is_positive_queue and match_type == "trusted":
        score += 0.18
        reasons.append("analysis_classification:trusted")
    elif is_positive_queue and match_type == "review_light":
        score += 0.05
        reasons.append("analysis_classification:review_light")
    elif is_review_light_queue and match_type == "review_light":
        score += 0.08
        reasons.append("analysis_classification:review_light")
    elif match_type.startswith("exact_"):
        score += 0.08
        reasons.append(f"exact_match:{match_type}")
    elif match_type.startswith("fuzzy_"):
        score -= 0.22
        reasons.append(f"fuzzy_match:{match_type}")

    old_has_residue = has_spanish_residue(row["old_text"])
    suggestion_has_residue = has_spanish_residue(row["suggested_text"])
    if old_has_residue and not suggestion_has_residue:
        score += 0.18
        reasons.append("removes_known_spanish_residue")
    elif suggestion_has_residue:
        score -= 0.35
        reasons.append("suggestion_keeps_known_spanish_residue")

    if validation["issue_count"] == 0:
        score += 0.10
        reasons.append("validator_clean")

    if (
        not is_positive_queue
        and not is_review_light_queue
        and normalize(row["suggested_text"]) == normalize(row["old_text"])
    ):
        score -= 0.20
        reasons.append("suggestion_same_as_old")

    if validation["issue_count"]:
        score -= 0.12 * validation["high_issue_count"]
        score -= 0.06 * validation["medium_issue_count"]
        issue_codes = [issue["code"] for issue in validation["issues"]]
        reasons.append(f"validator_issues:{','.join(issue_codes)}")

    adjustment, learned_reasons = learned_adjustment(row, validation, pattern_weights)
    if is_review_light_queue and validation["issue_count"] and adjustment > 0:
        adjustment = 0.0
        reasons.append("review_light_ignores_positive_learned_adjustment_with_validator_issues")
    if adjustment:
        score += adjustment
        reasons.append(f"learned_adjustment:{adjustment:.3f}")
        reasons.extend(learned_reasons[:8])

    score = max(0.0, min(score, validation["confidence_cap"]))
    threshold = float(row["auto_confidence_threshold"])
    local_status = (
        "high_confidence"
        if score >= threshold and not validation["auto_approval_blocked"]
        else "pending_human"
    )
    reasons.append(f"validator_cap:{validation['confidence_cap']:.2f}")
    reasons.append(f"validator_words:{validation['word_count']}")
    return score, reasons, local_status


def fetch_pending_candidates(conn, limit: int, auto_confidence_threshold: float, focus_group: str):
    focus_sql, focus_params = focus_clause(focus_group, "s")
    return conn.execute(
        f"""
        SELECT
            f.id AS feedback_id,
            f.suggestion_id,
            f.segment_id,
            f.suggested_text,
            ts.suggested_hash,
            ts.source_language,
            ts.origin,
            ts.match_type,
            ts.match_score,
            ts.token_status,
            ts.status AS suggestion_status,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS current_output_text,
            'pending' AS queue_source,
            ? AS focus_group,
            ? AS auto_confidence_threshold
        FROM suggestion_feedback f
        JOIN translation_suggestions ts ON ts.id = f.suggestion_id
        JOIN source_segments s ON s.id = f.segment_id
        LEFT JOIN output_segments o ON o.segment_id = s.id
        WHERE f.decision = 'pending'
          AND ts.status != 'stale'
          {focus_sql}
          AND NOT EXISTS (
              SELECT 1
              FROM local_learning_candidates c
              WHERE c.feedback_id = f.id
                AND c.suggested_hash = ts.suggested_hash
          )
        ORDER BY
            CASE ts.status
                WHEN 'safe' THEN 0
                WHEN 'review' THEN 1
                ELSE 2
            END,
            ts.match_score DESC,
            f.id ASC
        LIMIT ?
        """,
        (focus_group, auto_confidence_threshold, *focus_params, limit),
    ).fetchall()


def fetch_positive_candidates(conn, limit: int, auto_confidence_threshold: float, focus_group: str) -> list[dict]:
    focus_sql, focus_params = focus_clause(focus_group, "s")
    rows = conn.execute(
        f"""
        SELECT
            NULL AS feedback_id,
            NULL AS suggestion_id,
            s.id AS segment_id,
            s.old_text AS suggested_text,
            NULL AS suggested_hash,
            'old' AS source_language,
            'positive_core_sample' AS origin,
            a.classification AS match_type,
            COALESCE(a.confidence_score, 0.0) AS match_score,
            'ok' AS token_status,
            'sample' AS suggestion_status,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS current_output_text,
            'positive' AS queue_source,
            ? AS focus_group,
            ? AS auto_confidence_threshold
        FROM source_segments s
        JOIN segment_analysis a ON a.segment_id = s.id
        LEFT JOIN output_segments o ON o.segment_id = s.id
        WHERE s.is_active = 1
          AND s.has_old = 1
          AND s.old_text IS NOT NULL
          AND trim(s.old_text) != ''
          AND a.classification IN ('trusted', 'review_light')
          {focus_sql}
        ORDER BY
            CASE a.classification WHEN 'trusted' THEN 0 ELSE 1 END,
            a.confidence_score DESC,
            length(s.old_text) ASC,
            s.id ASC
        LIMIT ?
        """,
        (focus_group, auto_confidence_threshold, *focus_params, limit * 200),
    ).fetchall()

    selected: list[dict] = []
    for row in rows:
        item = dict(row)
        if not is_meaningful_positive_text(item["suggested_text"]):
            continue
        item["suggested_hash"] = sha256_text(item["suggested_text"])
        exists = conn.execute(
            """
            SELECT 1
            FROM local_learning_candidates
            WHERE segment_id = ?
              AND suggested_hash = ?
              AND queue_source = 'positive'
              AND focus_group = ?
            LIMIT 1
            """,
            (item["segment_id"], item["suggested_hash"], focus_group),
        ).fetchone()
        if exists:
            continue
        selected.append(item)
        if len(selected) >= limit:
            break
    return selected


def review_light_priority(item: dict) -> int:
    text = item["suggested_text"] or ""
    normalized = normalize(text)
    validation = local_quality_validator.validate_text(text)
    marker_hits = len(REVIEW_LIGHT_MARKER_PATTERN.findall(text))
    term_hits = sum(1 for term in REVIEW_LIGHT_TERMS if term in normalized)
    words = int(validation["word_count"] or 0)

    priority = 0
    priority += int(validation["high_issue_count"] or 0) * 12
    priority += int(validation["medium_issue_count"] or 0) * 6
    priority += int(validation["issue_count"] or 0) * 2
    priority += marker_hits * 5
    priority += term_hits * 8

    if 4 <= words <= 90:
        priority += 4
    elif words > 160:
        priority -= 4

    if item["match_type"] == "review_light":
        priority += 2

    return priority


def fetch_review_light_candidates(
    conn,
    limit: int,
    auto_confidence_threshold: float,
    focus_group: str,
) -> list[dict]:
    focus_sql, focus_params = focus_clause(focus_group, "s")
    rows = conn.execute(
        f"""
        SELECT
            NULL AS feedback_id,
            NULL AS suggestion_id,
            s.id AS segment_id,
            s.old_text AS suggested_text,
            NULL AS suggested_hash,
            'old' AS source_language,
            'review_light_residue_sample' AS origin,
            a.classification AS match_type,
            COALESCE(a.confidence_score, 0.0) AS match_score,
            'ok' AS token_status,
            'sample' AS suggestion_status,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS current_output_text,
            'review-light' AS queue_source,
            ? AS focus_group,
            ? AS auto_confidence_threshold
        FROM source_segments s
        JOIN segment_analysis a ON a.segment_id = s.id
        LEFT JOIN output_segments o ON o.segment_id = s.id
        LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
        WHERE s.is_active = 1
          AND s.has_old = 1
          AND s.old_text IS NOT NULL
          AND trim(s.old_text) != ''
          AND a.classification = 'review_light'
          AND sc.segment_id IS NULL
          {focus_sql}
        ORDER BY
            a.confidence_score ASC,
            length(s.old_text) ASC,
            s.id ASC
        LIMIT ?
        """,
        (focus_group, auto_confidence_threshold, *focus_params, limit * 300),
    ).fetchall()

    ranked: list[tuple[int, int, dict]] = []
    fallback: list[tuple[int, int, dict]] = []
    for row in rows:
        item = dict(row)
        if not is_meaningful_positive_text(item["suggested_text"]):
            continue
        item["suggested_hash"] = sha256_text(item["suggested_text"])
        exists = conn.execute(
            """
            SELECT 1
            FROM local_learning_candidates
            WHERE segment_id = ?
              AND suggested_hash = ?
              AND queue_source = 'review-light'
              AND focus_group = ?
            LIMIT 1
            """,
            (item["segment_id"], item["suggested_hash"], focus_group),
        ).fetchone()
        if exists:
            continue
        priority = review_light_priority(item)
        if priority > 0:
            ranked.append((priority, -int(item["segment_id"]), item))
        else:
            fallback.append((priority, -int(item["segment_id"]), item))

    ranked.sort(reverse=True)
    fallback.sort(reverse=True)
    selected = [item for _, _, item in ranked[:limit]]
    if len(selected) < limit:
        selected.extend(item for _, _, item in fallback[: limit - len(selected)])
    return selected[:limit]


def create_run(conn, limit: int, auto_confidence_threshold: float, queue_source: str, focus_group: str) -> int:
    timestamp = now()
    cursor = conn.execute(
        """
        INSERT INTO local_learning_runs (
            mode,
            limit_count,
            auto_confidence_threshold,
            status,
            started_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (f"{queue_source}:{focus_group}", limit, auto_confidence_threshold, "running", timestamp, timestamp),
    )
    return int(cursor.lastrowid)


def insert_candidate(conn, run_id: int, row, confidence: float, reasons: list[str], local_status: str) -> bool:
    timestamp = now()
    try:
        conn.execute(
            """
            INSERT INTO local_learning_candidates (
                run_id,
                feedback_id,
                suggestion_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                english_text,
                spanish_text,
                old_text,
                current_output_text,
                suggested_text,
                suggested_hash,
                queue_source,
                focus_group,
                source_language,
                origin,
                match_type,
                match_score,
                token_status,
                suggestion_status,
                local_confidence_score,
                local_status,
                reasons_json,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                row["feedback_id"],
                row["suggestion_id"],
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row["source_line_number"],
                row["english_text"],
                row["spanish_text"],
                row["old_text"],
                row["current_output_text"],
                row["suggested_text"],
                row["suggested_hash"],
                row["queue_source"],
                row["focus_group"],
                row["source_language"],
                row["origin"],
                row["match_type"],
                row["match_score"],
                row["token_status"],
                row["suggestion_status"],
                confidence,
                local_status,
                json.dumps(reasons, ensure_ascii=True),
                timestamp,
                timestamp,
            ),
        )
    except sqlite3.IntegrityError:
        return False
    return True


def main(
    limit: int | None = None,
    auto_confidence_threshold: float | None = None,
    queue_source: str | None = None,
    focus_group: str | None = None,
) -> None:
    settings = db.load_settings()
    learning_settings = settings.get("local_learning", {})
    limit = limit if limit is not None else int(learning_settings.get("review_limit", 20))
    auto_confidence_threshold = (
        auto_confidence_threshold
        if auto_confidence_threshold is not None
        else float(learning_settings.get("auto_confidence_threshold", 0.98))
    )
    queue_source = queue_source or str(learning_settings.get("queue_source", "pending"))
    focus_group = focus_group or str(learning_settings.get("focus_group", "all"))
    if queue_source not in {"pending", "positive", "review-light"}:
        raise ValueError("queue_source must be 'pending', 'positive', or 'review-light'")
    if focus_group not in FOCUS_GROUPS:
        raise ValueError(f"Unknown focus_group: {focus_group}")
    started_at = datetime.now()
    print("[local_learning_cycle] Starting local learning queue")
    print(f"[local_learning_cycle] Rule version: {RULE_VERSION}")
    print(f"[local_learning_cycle] Validator version: {local_quality_validator.RULE_VERSION}")
    print(f"[local_learning_cycle] Queue source: {queue_source}")
    print(f"[local_learning_cycle] Focus group: {focus_group}")
    print(f"[local_learning_cycle] Limit: {limit}")
    print(f"[local_learning_cycle] High confidence threshold: {auto_confidence_threshold}")
    print(f"[local_learning_cycle] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        run_id = create_run(conn, limit, auto_confidence_threshold, queue_source, focus_group)
        pattern_weights = load_pattern_weights(conn)
        if queue_source == "positive":
            rows = fetch_positive_candidates(conn, limit, auto_confidence_threshold, focus_group)
        elif queue_source == "review-light":
            rows = fetch_review_light_candidates(conn, limit, auto_confidence_threshold, focus_group)
        else:
            rows = fetch_pending_candidates(conn, limit, auto_confidence_threshold, focus_group)
        inserted = 0
        skipped = 0
        status_counts: Counter[str] = Counter()
        for row in rows:
            confidence, reasons, local_status = score_candidate(row, pattern_weights)
            if insert_candidate(conn, run_id, row, confidence, reasons, local_status):
                inserted += 1
                status_counts[local_status] += 1
            else:
                skipped += 1

        finished_at = now()
        conn.execute(
            """
            UPDATE local_learning_runs
            SET
                candidate_count = ?,
                high_confidence_count = ?,
                pending_human_count = ?,
                status = ?,
                finished_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                inserted,
                status_counts["high_confidence"],
                status_counts["pending_human"],
                "completed",
                finished_at,
                finished_at,
                run_id,
            ),
        )
        conn.commit()

    elapsed = datetime.now() - started_at
    report_lines = [
        "Local learning cycle report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Validator version: {local_quality_validator.RULE_VERSION}",
        f"Run id: {run_id}",
        f"Queue source: {queue_source}",
        f"Focus group: {focus_group}",
        "",
        "Summary:",
        f"- Limit: {limit}",
        f"- Candidates inserted: {inserted}",
        f"- Candidates skipped: {skipped}",
        f"- High confidence preview: {status_counts['high_confidence']}",
        f"- Pending human review: {status_counts['pending_human']}",
        "",
        "Human labels to use:",
        "- correct: ready as-is; tokens, structure, meaning, and Portuguese are good",
        "- minor_fix: only deterministic surface cleanup is needed, such as Spanish punctuation or simple spacing",
        "- major_fix: structure is usable and much is translated, but meaningful rewrite is still needed",
        "- residual_spanish: large Spanish residue remains, or the text is mostly Spanish",
        "- structure_error: CK3 token, literal, gender macro, markup, or command structure issue",
        "- semantic_error: fluent Portuguese, but the meaning changed",
        "- wrong: not useful, unrelated, or essentially repeats the bad old text",
        "- harmful: would worsen a good segment or break important protected structure",
    ]
    report_path = db.write_report(settings, "local_learning_cycle", report_lines)
    print(f"[local_learning_cycle] Run id: {run_id}")
    print(f"[local_learning_cycle] Candidates inserted: {inserted}")
    print(f"[local_learning_cycle] High confidence preview: {status_counts['high_confidence']}")
    print(f"[local_learning_cycle] Pending human review: {status_counts['pending_human']}")
    print(f"[local_learning_cycle] Report: {report_path}")
    print("[local_learning_cycle] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a small local learning review queue.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--auto-confidence-threshold", type=float, default=None)
    parser.add_argument("--queue-source", choices=["pending", "positive", "review-light"], default=None)
    parser.add_argument("--focus", choices=sorted(FOCUS_GROUPS), default=None)
    parsed = parser.parse_args()
    main(
        limit=parsed.limit,
        auto_confidence_threshold=parsed.auto_confidence_threshold,
        queue_source=parsed.queue_source,
        focus_group=parsed.focus,
    )
