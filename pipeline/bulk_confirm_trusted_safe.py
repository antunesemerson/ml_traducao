from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from typing import Any

import db
import local_quality_validator
from apply_safe_output_updates import protected_tokens


RULE_VERSION = "bulk_confirm_trusted_safe_v1"
DEFAULT_LABEL = "bulk_trusted_safe"
DEFAULT_REVIEWER = "bulk_trusted_safe"

FRAGILE_PRONOUN_TOKENS = (
    "GetHerHim",
    "GetHerHis",
    "GetSheHe",
    "GetHerselfHimself",
    "GetHersHis",
)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def percent(part: int, total: int) -> float:
    if total == 0:
        return 0.0
    return part / total * 100


def compact(value: str | None, limit: int = 220) -> str:
    text = (value or "").replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def has_fragile_pronouns(value: str | None) -> bool:
    text = value or ""
    return any(token in text for token in FRAGILE_PRONOUN_TOKENS)


def quote_count(value: str | None) -> int:
    return (value or "").count('"')


def fetch_rows(conn, limit: int | None, exclude_path_like: tuple[str, ...]) -> list[dict[str, Any]]:
    params: list[Any] = []
    exclude_path_sql = ""
    for pattern in exclude_path_like:
        exclude_path_sql += " AND s.relative_path NOT LIKE ?"
        params.append(pattern)
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
            s.english_text,
            s.spanish_text,
            s.old_text,
            a.classification,
            a.confidence_score AS analysis_score
        FROM source_segments s
        JOIN segment_analysis a ON a.segment_id = s.id
        LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
        WHERE s.is_active = 1
          AND sc.segment_id IS NULL
          AND a.classification = 'trusted'
          AND s.old_text IS NOT NULL
          {exclude_path_sql}
        ORDER BY
            a.confidence_score DESC,
            s.relative_path,
            s.source_line_number,
            s.id
        {limit_sql}
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def evaluate_row(
    row: dict[str, Any],
    min_analysis_score: float,
    max_words: int,
    allow_medium_issues: bool,
    exclude_fragile_pronouns: bool,
    require_quote_parity: bool,
) -> tuple[bool, dict[str, Any]]:
    candidate = row["old_text"] or ""
    quality = local_quality_validator.validate_text(candidate)
    reasons: list[str] = []

    if float(row["analysis_score"] or 0) < min_analysis_score:
        reasons.append("analysis_score_below_min")
    if not candidate.strip():
        reasons.append("empty_old_text")
    if int(quality["word_count"] or 0) > max_words:
        reasons.append("too_long")
    if int(quality["high_issue_count"] or 0) > 0:
        reasons.append("high_quality_issue")
    if not allow_medium_issues and int(quality["medium_issue_count"] or 0) > 0:
        reasons.append("medium_quality_issue")
    if protected_tokens(row["spanish_text"]) != protected_tokens(candidate):
        reasons.append("protected_tokens_mismatch")
    if exclude_fragile_pronouns and has_fragile_pronouns(candidate):
        reasons.append("fragile_pronoun")
    if require_quote_parity and quote_count(row["english_text"]) != quote_count(candidate):
        reasons.append("quote_parity_mismatch")

    evaluated = {
        **row,
        "candidate_text": candidate,
        "quality": quality,
        "word_count": int(quality["word_count"] or 0),
        "issue_count": int(quality["issue_count"] or 0),
        "high_issue_count": int(quality["high_issue_count"] or 0),
        "medium_issue_count": int(quality["medium_issue_count"] or 0),
        "reasons": reasons,
    }
    return not reasons, evaluated


def apply_confirmations(conn, candidates: list[dict[str, Any]], reviewer: str, label: str) -> None:
    timestamp = now()
    conn.executemany(
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
        VALUES (?, 'auto_confirmed', ?, 'bulk_confirm_trusted_safe', ?, 0, ?, NULL, NULL, ?, ?, ?)
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
                item["candidate_text"],
                label,
                min(0.99, max(0.95, float(item["analysis_score"] or 0))),
                reviewer,
                timestamp,
                timestamp,
                timestamp,
            )
            for item in candidates
        ],
    )


def build_report_lines(
    *,
    started_at: datetime,
    elapsed,
    apply: bool,
    inspected: int,
    candidates: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    limit: int | None,
    min_analysis_score: float,
    max_words: int,
    allow_medium_issues: bool,
    exclude_fragile_pronouns: bool,
    require_quote_parity: bool,
    exclude_path_like: tuple[str, ...],
    sample_limit: int,
    label: str,
) -> list[str]:
    package_counts = Counter(item["relative_path"] for item in candidates)
    rejection_counts = Counter(reason for item in rejected for reason in item["reasons"])
    word_counts = Counter(item["word_count"] for item in candidates)
    lines = [
        "Bulk confirm trusted safe report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Apply: {apply}",
        f"Limit: {limit or 'none'}",
        f"Min analysis score: {min_analysis_score}",
        f"Max words: {max_words}",
        f"Allow medium issues: {allow_medium_issues}",
        f"Exclude fragile pronouns: {exclude_fragile_pronouns}",
        f"Require quote parity: {require_quote_parity}",
        f"Exclude path like: {', '.join(exclude_path_like) if exclude_path_like else 'none'}",
        f"Confirmation label: {label}",
        "",
        "Summary:",
        f"- Trusted pending inspected: {inspected}",
        f"- Candidates selected: {len(candidates)} ({percent(len(candidates), inspected):.2f}%)",
        f"- Rejected by guardrails: {len(rejected)} ({percent(len(rejected), inspected):.2f}%)",
        f"- Confirmations written: {len(candidates) if apply else 0}",
        "",
        "Candidate word counts:",
        *[f"- {words} words: {count}" for words, count in sorted(word_counts.items())[:50]],
        "",
        "Top candidate packages:",
        *[f"- {path}: {count}" for path, count in package_counts.most_common(50)],
        "",
        "Rejection reasons:",
        *[f"- {reason}: {count}" for reason, count in rejection_counts.most_common(50)],
        "",
        "Candidate samples:",
    ]
    for item in candidates[:sample_limit]:
        lines.append(
            f"- segment {item['segment_id']} | {item['analysis_score']:.3f} | "
            f"{item['relative_path']}::{item['source_key']} | {compact(item['candidate_text'])}"
        )
    if not candidates:
        lines.append("- No candidates selected")

    lines.extend(["", "Rejected samples:"])
    for item in rejected[:sample_limit]:
        lines.append(
            f"- segment {item['segment_id']} | {', '.join(item['reasons'])} | "
            f"{item['relative_path']}::{item['source_key']} | {compact(item['candidate_text'])}"
        )
    if not rejected:
        lines.append("- No rejected rows")
    return lines


def main(
    *,
    limit: int | None = None,
    min_analysis_score: float = 0.99,
    max_words: int = 80,
    allow_medium_issues: bool = False,
    exclude_fragile_pronouns: bool = True,
    require_quote_parity: bool = False,
    exclude_path_like: tuple[str, ...] = (),
    sample_limit: int = 40,
    apply: bool = False,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    label = DEFAULT_LABEL
    print("[bulk_confirm_trusted_safe] Starting trusted-safe bulk confirmation")
    print(f"[bulk_confirm_trusted_safe] Rule version: {RULE_VERSION}")
    print(f"[bulk_confirm_trusted_safe] Apply: {apply}")
    print(f"[bulk_confirm_trusted_safe] Limit: {limit or 'none'}")
    print(f"[bulk_confirm_trusted_safe] Min analysis score: {min_analysis_score}")
    print(f"[bulk_confirm_trusted_safe] Max words: {max_words}")
    print(f"[bulk_confirm_trusted_safe] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        rows = fetch_rows(conn, limit=limit, exclude_path_like=exclude_path_like)
        candidates: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for row in rows:
            accepted, evaluated = evaluate_row(
                row,
                min_analysis_score=min_analysis_score,
                max_words=max_words,
                allow_medium_issues=allow_medium_issues,
                exclude_fragile_pronouns=exclude_fragile_pronouns,
                require_quote_parity=require_quote_parity,
            )
            if accepted:
                candidates.append(evaluated)
            else:
                rejected.append(evaluated)
        if apply:
            apply_confirmations(conn, candidates, reviewer=DEFAULT_REVIEWER, label=label)
            conn.commit()

    elapsed = datetime.now() - started_at
    lines = build_report_lines(
        started_at=started_at,
        elapsed=elapsed,
        apply=apply,
        inspected=len(rows),
        candidates=candidates,
        rejected=rejected,
        limit=limit,
        min_analysis_score=min_analysis_score,
        max_words=max_words,
        allow_medium_issues=allow_medium_issues,
        exclude_fragile_pronouns=exclude_fragile_pronouns,
        require_quote_parity=require_quote_parity,
        exclude_path_like=exclude_path_like,
        sample_limit=sample_limit,
        label=label,
    )
    report_path = db.write_report(settings, "bulk_confirm_trusted_safe", lines)

    print(f"[bulk_confirm_trusted_safe] Trusted pending inspected: {len(rows)}")
    print(f"[bulk_confirm_trusted_safe] Candidates selected: {len(candidates)}")
    print(f"[bulk_confirm_trusted_safe] Confirmations written: {len(candidates) if apply else 0}")
    print(f"[bulk_confirm_trusted_safe] Report: {report_path}")
    print("[bulk_confirm_trusted_safe] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bulk-confirm trusted pending rows that pass strict local quality guardrails.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum trusted pending rows inspected.")
    parser.add_argument("--min-analysis-score", type=float, default=0.99, help="Minimum segment_analysis confidence.")
    parser.add_argument("--max-words", type=int, default=80, help="Maximum visible word count.")
    parser.add_argument("--allow-medium-issues", action="store_true", help="Allow medium local-quality issues.")
    parser.add_argument("--include-fragile-pronouns", action="store_true", help="Do not exclude candidate texts with fragile pronoun tokens.")
    parser.add_argument("--require-quote-parity", action="store_true", help="Require straight double-quote count to match English.")
    parser.add_argument("--exclude-path-like", action="append", default=[], help="Exclude relative_path LIKE pattern. Can be repeated.")
    parser.add_argument("--sample-limit", type=int, default=40, help="Number of samples included in the report.")
    parser.add_argument("--apply", action="store_true", help="Write auto confirmations. Default is dry-run.")
    args = parser.parse_args()
    main(
        limit=args.limit,
        min_analysis_score=args.min_analysis_score,
        max_words=args.max_words,
        allow_medium_issues=args.allow_medium_issues,
        exclude_fragile_pronouns=not args.include_fragile_pronouns,
        require_quote_parity=args.require_quote_parity,
        exclude_path_like=tuple(args.exclude_path_like),
        sample_limit=args.sample_limit,
        apply=args.apply,
    )
