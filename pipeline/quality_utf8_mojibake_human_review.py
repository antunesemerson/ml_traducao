from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from typing import Any, Iterable

import db
import local_quality_validator
from apply_safe_output_updates import protected_tokens
from quality_utf8_mojibake_pairwise_evidence import EVIDENCE_TYPE
from quality_utf8_mojibake_shadow import repair_utf8_mojibake


RULE_VERSION = "quality_utf8_mojibake_human_review_v1"
REVIEW_SOURCE_TAG = "utf8_mojibake_human_review_v1"
REVIEW_LABEL_TAG = "utf8_mojibake_review_approved_v1"
REVIEWER_TAG = "codex_user_authorized_v8_review"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def append_tag(value: Any, tag: str, separator: str) -> str:
    parts = [part for part in str(value or "").split(separator) if part]
    if tag not in parts:
        parts.append(tag)
    return separator.join(parts)


def latest_pairwise_run_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_pairwise_quality_runs
        WHERE evidence_type = ?
          AND finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
        (EVIDENCE_TYPE,),
    ).fetchone()
    if not row:
        raise RuntimeError(f"No completed pairwise run exists for {EVIDENCE_TYPE}.")
    return int(row["id"])


def load_review_rows(
    conn: sqlite3.Connection,
    *,
    pairwise_run_id: int | None = None,
    segment_ids: Iterable[int] | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    run_id = pairwise_run_id or latest_pairwise_run_id(conn)
    requested = sorted(set(int(segment_id) for segment_id in (segment_ids or [])))
    filters = ["evidence.evidence_type = ?", "evidence.last_run_id = ?"]
    params: list[Any] = [EVIDENCE_TYPE, run_id]
    if requested:
        placeholders = ",".join("?" for _ in requested)
        filters.append(f"evidence.segment_id IN ({placeholders})")
        params.extend(requested)
    rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT
                evidence.id AS evidence_id,
                evidence.segment_id,
                evidence.relative_path AS evidence_relative_path,
                evidence.source_key AS evidence_source_key,
                evidence.baseline_text,
                evidence.candidate_text,
                evidence.baseline_hash,
                evidence.candidate_hash,
                evidence.preference_label,
                evidence.training_target,
                evidence.recommended_route,
                evidence.token_integrity_ok,
                evidence.post_validation_clean,
                evidence.blockers_json,
                source.relative_path,
                source.source_key,
                source.spanish_text,
                source.english_text,
                output.portuguese_text AS output_text,
                confirmation.id AS confirmation_id,
                confirmation.confirmation_level,
                confirmation.confirmed_text,
                confirmation.confirmation_source,
                confirmation.confirmation_label,
                confirmation.locked,
                confirmation.confidence_score,
                confirmation.reviewer
            FROM ml_pairwise_quality_evidence evidence
            JOIN source_segments source ON source.id = evidence.segment_id
            JOIN output_segments output ON output.segment_id = evidence.segment_id
            LEFT JOIN segment_confirmations confirmation
              ON confirmation.segment_id = evidence.segment_id
            WHERE {" AND ".join(filters)}
            ORDER BY evidence.segment_id
            """,
            params,
        ).fetchall()
    ]
    if requested:
        found = {int(row["segment_id"]) for row in rows}
        missing = sorted(set(requested) - found)
        if missing:
            raise RuntimeError(
                f"Requested segments are absent from pairwise run {run_id}: {missing}."
            )
    return run_id, rows


def review_row(row: dict[str, Any]) -> dict[str, Any]:
    baseline = str(row.get("baseline_text") or "")
    candidate = str(row.get("candidate_text") or "")
    output = str(row.get("output_text") or "")
    confirmed = row.get("confirmed_text")
    blockers: list[str] = []

    repaired, repairs = repair_utf8_mojibake(baseline)
    if not repairs or repaired == baseline:
        blockers.append("no_deterministic_repair")
    if repaired != candidate:
        blockers.append("candidate_not_deterministic_repair")
    if sha256_text(baseline) != str(row.get("baseline_hash") or ""):
        blockers.append("baseline_hash_mismatch")
    if sha256_text(candidate) != str(row.get("candidate_hash") or ""):
        blockers.append("candidate_hash_mismatch")
    if output != baseline:
        blockers.append("stale_output_text")
    if str(row.get("relative_path") or "") != str(row.get("evidence_relative_path") or ""):
        blockers.append("relative_path_mismatch")
    if str(row.get("source_key") or "") != str(row.get("evidence_source_key") or ""):
        blockers.append("source_key_mismatch")
    if row.get("confirmation_id") is None:
        blockers.append("missing_confirmation")
    if int(row.get("locked") or 0) != 1:
        blockers.append("confirmation_not_locked")
    if "human" not in str(row.get("confirmation_level") or "").lower():
        blockers.append("confirmation_not_human")
    if protected_tokens(baseline) != protected_tokens(candidate):
        blockers.append("token_signature_changed")
    if int(row.get("token_integrity_ok") or 0) != 1:
        blockers.append("evidence_token_guard_failed")
    if int(row.get("post_validation_clean") or 0) != 1:
        blockers.append("evidence_post_validation_guard_failed")
    post_issues = [
        str(issue.get("code"))
        for issue in local_quality_validator.validate_text(candidate).get("issues") or []
        if issue.get("code")
    ]
    if post_issues:
        blockers.append("candidate_has_validation_issues")
    if str(row.get("preference_label") or "") != "candidate_preferred":
        blockers.append("candidate_not_preferred")
    if str(row.get("training_target") or "") != "pairwise_preference_only":
        blockers.append("unexpected_training_target")
    if str(row.get("recommended_route") or "") != "human_unlock_review":
        blockers.append("evidence_not_routed_to_human_review")
    try:
        evidence_blockers = json.loads(str(row.get("blockers_json") or "[]"))
    except json.JSONDecodeError:
        evidence_blockers = ["invalid_blockers_json"]
    if evidence_blockers:
        blockers.append("evidence_has_blockers")

    already_reviewed = (
        str(confirmed or "") == candidate
        and REVIEW_SOURCE_TAG in str(row.get("confirmation_source") or "").split("+")
        and REVIEW_LABEL_TAG in str(row.get("confirmation_label") or "").split(";")
    )
    if not already_reviewed and str(confirmed or "") != baseline:
        blockers.append("stale_confirmation_text")

    unique_blockers = sorted(set(blockers))
    if unique_blockers:
        status = "blocked"
    elif already_reviewed:
        status = "already_reviewed"
    else:
        status = "ready"
    return {
        **row,
        "status": status,
        "blockers": unique_blockers,
        "repair_count": sum(int(repair["occurrence_count"]) for repair in repairs),
        "post_issue_codes": sorted(set(post_issues)),
    }


def collect_reviews(
    conn: sqlite3.Connection,
    *,
    pairwise_run_id: int | None = None,
    segment_ids: Iterable[int] | None = None,
    expected_count: int | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    run_id, rows = load_review_rows(
        conn, pairwise_run_id=pairwise_run_id, segment_ids=segment_ids
    )
    if expected_count is not None and len(rows) != expected_count:
        raise RuntimeError(
            f"Expected {expected_count} review rows in run {run_id}, found {len(rows)}."
        )
    reviews = [review_row(row) for row in rows]
    blocked = [review for review in reviews if review["status"] == "blocked"]
    if blocked:
        details = {
            int(review["segment_id"]): review["blockers"] for review in blocked
        }
        raise RuntimeError(f"UTF-8 human review is blocked: {details}")
    return run_id, reviews


def apply_reviews(
    conn: sqlite3.Connection,
    *,
    pairwise_run_id: int | None = None,
    segment_ids: Iterable[int] | None = None,
    expected_count: int | None = None,
) -> tuple[int, list[dict[str, Any]], int]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        run_id, reviews = collect_reviews(
            conn,
            pairwise_run_id=pairwise_run_id,
            segment_ids=segment_ids,
            expected_count=expected_count,
        )
        now = db.utc_now()
        applied = 0
        for review in reviews:
            if review["status"] != "ready":
                continue
            cursor = conn.execute(
                """
                UPDATE segment_confirmations
                SET confirmed_text = ?,
                    confirmation_source = ?,
                    confirmation_label = ?,
                    locked = 1,
                    confidence_score = 1.0,
                    reviewer = ?,
                    confirmed_at = ?,
                    updated_at = ?
                WHERE id = ?
                  AND segment_id = ?
                  AND locked = 1
                  AND confirmed_text = ?
                """,
                (
                    review["candidate_text"],
                    append_tag(review.get("confirmation_source"), REVIEW_SOURCE_TAG, "+"),
                    append_tag(review.get("confirmation_label"), REVIEW_LABEL_TAG, ";"),
                    append_tag(review.get("reviewer"), REVIEWER_TAG, ";"),
                    now,
                    now,
                    review["confirmation_id"],
                    review["segment_id"],
                    review["baseline_text"],
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Concurrent confirmation change detected for segment {review['segment_id']}."
                )
            applied += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return run_id, reviews, applied


def summary(
    run_id: int,
    reviews: list[dict[str, Any]],
    *,
    apply: bool,
    applied: int,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source": RULE_VERSION,
        "pairwise_run_id": run_id,
        "database_write": apply,
        "review_count": len(reviews),
        "ready_count": sum(review["status"] == "ready" for review in reviews),
        "already_reviewed_count": sum(
            review["status"] == "already_reviewed" for review in reviews
        ),
        "approved_count": len(reviews),
        "confirmation_write_count": applied,
        "lock_preserved_count": len(reviews),
        "output_write_count": 0,
        "reports_required": False,
        "segments": [
            {
                "segment_id": int(review["segment_id"]),
                "relative_path": review["relative_path"],
                "source_key": review["source_key"],
                "status": review["status"],
                "repair_count": review["repair_count"],
            }
            for review in reviews
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Record reviewed UTF-8 mojibake repairs in locked human confirmations. "
            "The output tree is never modified."
        )
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--pairwise-run-id", type=int)
    parser.add_argument("--segment-id", type=int, action="append", default=[])
    parser.add_argument("--expected-count", type=int)
    args = parser.parse_args()

    settings = db.load_settings()
    if args.apply:
        with db.connect(settings) as conn:
            db.ensure_database(conn)
            run_id, reviews, applied = apply_reviews(
                conn,
                pairwise_run_id=args.pairwise_run_id,
                segment_ids=args.segment_id,
                expected_count=args.expected_count,
            )
    else:
        database_path = db.get_database_path(settings)
        with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=120) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            run_id, reviews = collect_reviews(
                conn,
                pairwise_run_id=args.pairwise_run_id,
                segment_ids=args.segment_id,
                expected_count=args.expected_count,
            )
            applied = 0
    print(json.dumps(summary(run_id, reviews, apply=args.apply, applied=applied), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
