from __future__ import annotations

import argparse
import re
from collections import Counter
from datetime import datetime

import db
import local_quality_validator
from apply_safe_output_updates import protected_tokens


RULE_VERSION = "auto_validate_segments_v1"
TECHNICAL_PATH_PREFIXES = (
    "custom_localization/",
    "debug",
)
TECHNICAL_KEY_PATTERN = re.compile(
    r"(^CustomLoc_ES_|^num_suffix_\d+$|_article$|ARTICLE$|SEPARATOR$|PREFIX$)"
)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def percent(part: int, total: int) -> float:
    if total == 0:
        return 0.0
    return part / total * 100


def has_human_baseline(conn) -> bool:
    total = conn.execute(
        """
        SELECT COUNT(*)
        FROM segment_confirmations
        WHERE confirmation_level = 'human_confirmed'
          AND locked = 1
        """
    ).fetchone()[0]
    return int(total or 0) >= 100


def token_status(spanish_text: str | None, candidate_text: str | None) -> str:
    if protected_tokens(spanish_text) == protected_tokens(candidate_text):
        return "ok"
    return "mismatch"


def is_visible_human_candidate(item: dict) -> tuple[bool, str]:
    relative_path = str(item.get("relative_path") or "")
    source_key = str(item.get("source_key") or "")
    candidate_text = str(item.get("candidate_text") or "")

    if any(relative_path.startswith(prefix) for prefix in TECHNICAL_PATH_PREFIXES):
        return False, "technical_path"
    if TECHNICAL_KEY_PATTERN.search(source_key):
        return False, "technical_key"
    if local_quality_validator.word_count(candidate_text) == 0:
        return False, "no_human_words"
    if len(candidate_text.strip()) < 3:
        return False, "too_short"
    return True, "ok"


def auto_score(row, candidate_text: str, source: str) -> tuple[float, list[str]]:
    reasons: list[str] = [f"source:{source}"]
    validation = local_quality_validator.validate_text(candidate_text)
    score = 0.0
    source_key = str(row["source_key"] or "")
    english_text = str(row["english_text"] or "")
    spanish_text = str(row["spanish_text"] or "")

    if source == "old_trusted":
        score = 0.74
        if row["classification"] == "trusted":
            score += 0.15
            reasons.append("analysis:trusted")
        if float(row["analysis_confidence"] or 0) >= 0.99:
            score += 0.05
            reasons.append("analysis_confidence:0.99+")
    elif source == "safe_suggestion":
        score = 0.64
        if row["suggestion_status"] == "safe":
            score += 0.12
            reasons.append("suggestion_status:safe")
        if row["token_status"] == "ok":
            score += 0.08
            reasons.append("suggestion_token_status:ok")
        if float(row["match_score"] or 0) >= 0.98:
            score += 0.05
            reasons.append("match_score:0.98+")

    if validation["issue_count"] == 0:
        score += 0.10
        reasons.append("validator_clean")
    else:
        issue_codes = [issue["code"] for issue in validation["issues"]]
        reasons.append(f"validator_issues:{','.join(issue_codes)}")
        score -= 0.25

    if validation["word_count"] <= 8:
        score += 0.04
        reasons.append("length:short")
    elif validation["word_count"] <= 30:
        reasons.append("length:medium")
    else:
        score -= 0.15
        reasons.append("length:long")

    if token_status(row["spanish_text"], candidate_text) == "ok":
        score += 0.07
        reasons.append("protected_tokens:ok")
    else:
        score -= 0.50
        reasons.append("protected_tokens:mismatch")

    if validation["auto_approval_blocked"]:
        score = min(score, 0.60)
        reasons.append("auto_blocked_by_validator")

    if (
        local_quality_validator.normalize(english_text)
        == local_quality_validator.normalize(spanish_text)
        and local_quality_validator.normalize(candidate_text)
        != local_quality_validator.normalize(english_text)
    ):
        score = min(score, 0.60)
        reasons.append("source_identity_changed")

    if (
        (".desc" in source_key or ".tt" in source_key)
        and validation["word_count"] <= 4
        and local_quality_validator.normalize(english_text)
        != local_quality_validator.normalize(spanish_text)
    ):
        score = min(score, 0.60)
        reasons.append("contextual_short_key")

    return max(0.0, min(score, 0.99)), reasons


def fetch_old_trusted_candidates(conn, limit: int) -> list[dict]:
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
            NULL AS suggestion_id,
            NULL AS feedback_id,
            NULL AS suggestion_status,
            NULL AS token_status,
            NULL AS match_score
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
        ORDER BY length(s.old_text) ASC, s.id ASC
        LIMIT ?
        """,
        (limit * 50,),
    ).fetchall()
    filtered: list[dict] = []
    for row in rows:
        item = dict(row) | {"candidate_source": "old_trusted"}
        visible_ok, _ = is_visible_human_candidate(item)
        if not visible_ok:
            continue
        filtered.append(item)
        if len(filtered) >= limit:
            break
    return filtered


def fetch_safe_suggestion_candidates(conn, limit: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.spanish_text,
            s.english_text,
            s.old_text,
            ts.suggested_text AS candidate_text,
            a.classification,
            a.confidence_score AS analysis_confidence,
            ts.id AS suggestion_id,
            f.id AS feedback_id,
            ts.status AS suggestion_status,
            ts.token_status,
            ts.match_score
        FROM translation_suggestions ts
        JOIN suggestion_feedback f ON f.suggestion_id = ts.id
        JOIN source_segments s ON s.id = ts.segment_id
        LEFT JOIN segment_analysis a ON a.segment_id = s.id
        LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
        WHERE s.is_active = 1
          AND f.decision = 'pending'
          AND ts.status = 'safe'
          AND ts.token_status = 'ok'
          AND COALESCE(ts.match_score, 0) >= 0.98
          AND sc.segment_id IS NULL
        ORDER BY ts.match_score DESC, length(ts.suggested_text) ASC, ts.id ASC
        LIMIT ?
        """,
        (limit * 50,),
    ).fetchall()
    filtered: list[dict] = []
    for row in rows:
        item = dict(row) | {"candidate_source": "safe_suggestion"}
        visible_ok, _ = is_visible_human_candidate(item)
        if not visible_ok:
            continue
        filtered.append(item)
        if len(filtered) >= limit:
            break
    return filtered


def upsert_auto_confirmation(conn, item: dict, score: float) -> None:
    timestamp = now()
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
            candidate_id,
            feedback_id,
            reviewer,
            confirmed_at,
            updated_at
        )
        VALUES (?, 'auto_confirmed', ?, ?, 'auto_validated', 0, ?, NULL, ?, 'local_auto', ?, ?)
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
            feedback_id = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.feedback_id
                ELSE excluded.feedback_id
            END,
            reviewer = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.reviewer
                ELSE excluded.reviewer
            END,
            updated_at = ?
        """,
        (
            item["segment_id"],
            item["candidate_text"],
            item["candidate_source"],
            score,
            item.get("feedback_id"),
            timestamp,
            timestamp,
            timestamp,
        ),
    )


def main(limit: int | None = None, min_score: float | None = None, apply: bool = False) -> None:
    settings = db.load_settings()
    auto_settings = settings.get("auto_validation", {})
    limit = limit if limit is not None else int(auto_settings.get("review_limit", 500))
    min_score = min_score if min_score is not None else float(auto_settings.get("min_score", 0.98))
    started_at = datetime.now()

    print("[auto_validate_segments] Starting auto validation")
    print(f"[auto_validate_segments] Rule version: {RULE_VERSION}")
    print(f"[auto_validate_segments] Validator version: {local_quality_validator.RULE_VERSION}")
    print(f"[auto_validate_segments] Limit per source: {limit}")
    print(f"[auto_validate_segments] Min score: {min_score}")
    print(f"[auto_validate_segments] Apply: {apply}")
    print(f"[auto_validate_segments] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        baseline_ok = has_human_baseline(conn)
        old_rows = fetch_old_trusted_candidates(conn, limit)
        suggestion_rows = fetch_safe_suggestion_candidates(conn, limit)
        candidates = [*old_rows, *suggestion_rows]

        accepted: list[tuple[dict, float, list[str]]] = []
        rejected_counts: Counter[str] = Counter()
        source_counts: Counter[str] = Counter()
        for item in candidates:
            visible_ok, visible_reason = is_visible_human_candidate(item)
            if not visible_ok:
                rejected_counts[visible_reason] += 1
                continue
            score, reasons = auto_score(item, item["candidate_text"], item["candidate_source"])
            source_counts[item["candidate_source"]] += 1
            if not baseline_ok:
                rejected_counts["human_baseline_below_100"] += 1
                continue
            if score < min_score:
                rejected_counts["score_below_threshold"] += 1
                continue
            accepted.append((item, score, reasons))

        applied = 0
        if apply:
            for item, score, _ in accepted:
                upsert_auto_confirmation(conn, item, score)
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
    preview = accepted[:50]
    report_lines = [
        "Auto validation report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Validator version: {local_quality_validator.RULE_VERSION}",
        f"Apply: {apply}",
        f"Limit per source: {limit}",
        f"Min score: {min_score}",
        f"Human baseline >= 100: {baseline_ok}",
        "",
        "Summary:",
        f"- Candidates inspected: {len(candidates)}",
        f"- Auto-confirmable preview: {len(accepted)}",
        f"- Applied auto confirmations: {applied}",
        f"- Active segments: {total_segments}",
        f"- Confirmed after run: {total_confirmed} ({percent(total_confirmed, total_segments):.4f}%)",
        f"- Human confirmed: {confirmed.get('human_confirmed', 0)}",
        f"- Auto confirmed: {confirmed.get('auto_confirmed', 0)}",
        "",
        "Sources inspected:",
        *[f"- {source}: {total}" for source, total in sorted(source_counts.items())],
        "",
        "Rejected reasons:",
        *[f"- {reason}: {total}" for reason, total in rejected_counts.most_common()],
        "",
        "Auto-confirmable preview:",
        *[
            (
                f"- segment {item['segment_id']} | {score:.3f} | {item['candidate_source']} | "
                f"{item['relative_path']}::{item['source_key']} | {'; '.join(reasons[:8])}"
            )
            for item, score, reasons in preview
        ],
    ]
    if not preview:
        report_lines.append("- No auto-confirmable candidates")

    report_path = db.write_report(settings, "auto_validate_segments", report_lines)
    print(f"[auto_validate_segments] Candidates inspected: {len(candidates)}")
    print(f"[auto_validate_segments] Auto-confirmable preview: {len(accepted)}")
    print(f"[auto_validate_segments] Applied auto confirmations: {applied}")
    print(f"[auto_validate_segments] Report: {report_path}")
    print("[auto_validate_segments] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate segments for conservative automatic confirmation.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum candidates per source.")
    parser.add_argument("--min-score", type=float, default=None, help="Minimum score for auto confirmation.")
    parser.add_argument("--apply", action="store_true", help="Write auto_confirmed rows. Default is report only.")
    args = parser.parse_args()
    main(limit=args.limit, min_score=args.min_score, apply=args.apply)
