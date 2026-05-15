from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from typing import Any

import db
import local_quality_validator
from pending_diagnostic import classify_bucket, fast_classify_item, fetch_pending_rows, sample_text


RULE_VERSION = "bulk_confirm_likely_v1"
DEFAULT_LABEL = "bulk_likely_confirmable"
DEFAULT_SCORE = 0.94
SPANISH_CUSTOM_TOKEN_MARKERS = ("Custom('ES_", 'Custom("ES_')


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def percent(part: int, total: int) -> float:
    if total == 0:
        return 0.0
    return part / total * 100


def audit_candidate(item: dict[str, Any], max_words: int, min_score: float) -> tuple[bool, str]:
    candidate_text = item["candidate_text"] or ""
    validation = local_quality_validator.validate_text(candidate_text)
    if classify_bucket(item) != "likely_confirmable":
        return False, f"bucket:{classify_bucket(item)}"
    if item["candidate_source"] != "old_text":
        return False, f"source:{item['candidate_source']}"
    if item["token_status"] != "ok":
        return False, f"token:{item['token_status']}"
    if int(item["word_count"] or 0) > max_words:
        return False, "too_many_words"
    if float(item["confidence_score"] or 0) < min_score:
        return False, "low_score"
    if int(item["issue_count"] or 0) != 0:
        return False, "fast_issues"
    if int(validation["issue_count"] or 0) != 0:
        codes = ",".join(issue["code"] for issue in validation["issues"][:4])
        return False, f"quality:{codes or 'issues'}"
    if any(marker in candidate_text for marker in SPANISH_CUSTOM_TOKEN_MARKERS):
        return False, "spanish_custom_token"
    return True, "accepted"


def is_candidate(item: dict[str, Any], max_words: int, min_score: float) -> bool:
    return audit_candidate(item, max_words=max_words, min_score=min_score)[0]


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
        VALUES (?, 'auto_confirmed', ?, ?, ?, 0, ?, NULL, NULL, ?, ?, ?)
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
                "bulk_confirm_likely",
                label,
                max(float(item["confidence_score"] or 0), DEFAULT_SCORE),
                reviewer,
                timestamp,
                timestamp,
                timestamp,
            )
            for item in candidates
        ],
    )


def build_report_lines(
    started_at: datetime,
    elapsed,
    apply: bool,
    inspected: int,
    candidates: list[dict[str, Any]],
    rejected_reasons: Counter,
    limit: int | None,
    max_words: int,
    min_score: float,
    label: str,
    sample_limit: int,
) -> list[str]:
    package_counts = Counter(item["relative_path"] for item in candidates)
    word_counts = Counter(int(item["word_count"] or 0) for item in candidates)
    lines = [
        "Bulk confirm likely report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Apply: {apply}",
        f"Limit: {limit or 'none'}",
        f"Max words: {max_words}",
        f"Min score: {min_score}",
        f"Confirmation label: {label}",
        "",
        "Summary:",
        f"- Pending inspected: {inspected}",
        f"- Candidates selected: {len(candidates)} ({percent(len(candidates), inspected):.2f}%)",
        f"- Confirmations written: {len(candidates) if apply else 0}",
        "",
        "Rejected by audit:",
        *[f"- {reason}: {count}" for reason, count in rejected_reasons.most_common(30)],
        "",
        "Word counts:",
        *[f"- {words} words: {count}" for words, count in sorted(word_counts.items())[:40]],
        "",
        "Top packages:",
        *[f"- {path}: {count}" for path, count in package_counts.most_common(40)],
        "",
        "Samples:",
    ]
    for item in candidates[:sample_limit]:
        lines.append(
            f"- segment {item['segment_id']} | {item['confidence_score']:.3f} | "
            f"{item['relative_path']}::{item['source_key']} | {sample_text(item['candidate_text'], 180)}"
        )
    if not candidates:
        lines.append("- No candidates selected")
    return lines


def main(
    limit: int | None = None,
    max_words: int = 30,
    min_score: float = 0.94,
    sample_limit: int = 50,
    apply: bool = False,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[bulk_confirm_likely] Starting bulk likely confirmation")
    print(f"[bulk_confirm_likely] Rule version: {RULE_VERSION}")
    print(f"[bulk_confirm_likely] Apply: {apply}")
    print(f"[bulk_confirm_likely] Limit: {limit or 'none'}")
    print(f"[bulk_confirm_likely] Max words: {max_words}")
    print(f"[bulk_confirm_likely] Min score: {min_score}")
    print(f"[bulk_confirm_likely] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        rows = fetch_pending_rows(conn, limit, None)
        items = [fast_classify_item(row) for row in rows]
        candidates: list[dict[str, Any]] = []
        rejected_reasons: Counter = Counter()
        for item in items:
            accepted, reason = audit_candidate(item, max_words=max_words, min_score=min_score)
            if accepted:
                candidates.append(item)
            else:
                rejected_reasons[reason] += 1
        if apply:
            apply_confirmations(conn, candidates, reviewer="bulk_auto", label=DEFAULT_LABEL)
            conn.commit()

    elapsed = datetime.now() - started_at
    lines = build_report_lines(
        started_at=started_at,
        elapsed=elapsed,
        apply=apply,
        inspected=len(rows),
        candidates=candidates,
        rejected_reasons=rejected_reasons,
        limit=limit,
        max_words=max_words,
        min_score=min_score,
        label=DEFAULT_LABEL,
        sample_limit=sample_limit,
    )
    report_path = db.write_report(settings, "bulk_confirm_likely", lines)

    print(f"[bulk_confirm_likely] Pending inspected: {len(rows)}")
    print(f"[bulk_confirm_likely] Candidates selected: {len(candidates)}")
    print(f"[bulk_confirm_likely] Confirmations written: {len(candidates) if apply else 0}")
    print(f"[bulk_confirm_likely] Report: {report_path}")
    print("[bulk_confirm_likely] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bulk-confirm low-risk likely Portuguese rows.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum pending rows inspected.")
    parser.add_argument("--max-words", type=int, default=30, help="Maximum visible word count.")
    parser.add_argument("--min-score", type=float, default=0.94, help="Minimum fast confidence score.")
    parser.add_argument("--sample-limit", type=int, default=50, help="Preview sample rows in report.")
    parser.add_argument("--apply", action="store_true", help="Write auto confirmations.")
    args = parser.parse_args()
    main(
        limit=args.limit,
        max_words=args.max_words,
        min_score=args.min_score,
        sample_limit=args.sample_limit,
        apply=args.apply,
    )
