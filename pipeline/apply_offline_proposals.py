from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime

import db
import local_quality_validator
from apply_safe_output_updates import protected_tokens
from offline_residual_proposals import (
    has_separated_gender_suffix,
    has_punctuation_repair_risk,
    issue_codes,
    is_clean_punctuation_only_proposal,
    token_status,
    translatable_literal_risk_terms,
    visible_spanish_risk_terms,
)


RULE_VERSION = "apply_offline_proposals_v2"
DEFAULT_MIN_SCORE = 0.90
DEFAULT_ALLOWED_SOURCES = {
    "exact_confirmed_memory",
    "visible_phrase_replacement",
    "visible_word_replacement",
    "inline_literal_replacement",
    "inline_literal_replacement+visible_phrase_replacement",
    "normalize_spanish_punctuation",
    "normalize_spanish_punctuation+visible_phrase_replacement",
    "normalize_spanish_punctuation+visible_word_replacement",
    "remove_space_before_punctuation",
    "space_after_token",
    "space_after_token+visible_phrase_replacement",
    "space_after_token+visible_word_replacement",
    "space_after_token+inline_literal_replacement+visible_word_replacement",
    "space_after_token+inline_literal_replacement+visible_phrase_replacement",
    "space_after_token+inline_literal_replacement+visible_phrase_replacement+visible_word_replacement",
    "inline_literal_replacement+space_after_token+visible_word_replacement",
    "inline_literal_replacement+space_after_token+visible_phrase_replacement",
    "inline_literal_replacement+space_after_token+visible_phrase_replacement+visible_word_replacement",
    "space_before_token",
    "space_after_token+remove_space_before_punctuation",
    "remove_space_before_punctuation+space_after_token",
    "remove_space_before_punctuation+visible_phrase_replacement",
    "normalize_spanish_punctuation+space_after_token+visible_phrase_replacement",
    "normalize_spanish_punctuation+space_after_token+visible_word_replacement",
    "normalize_spanish_punctuation+inline_literal_replacement+visible_word_replacement",
    "fix_mojibake+normalize_spanish_punctuation+visible_phrase_replacement",
    "inline_literal_replacement+visible_word_replacement",
}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def latest_run_id(conn) -> int | None:
    row = conn.execute("SELECT MAX(id) AS id FROM offline_proposal_runs").fetchone()
    if not row or row["id"] is None:
        return None
    return int(row["id"])


def fetch_candidates(
    conn,
    run_id: int,
    min_score: float,
    limit: int | None,
    path_like: str | None,
    include_literal_changed: bool,
) -> list[dict]:
    params: list[object] = [run_id, min_score]
    path_sql = ""
    allowed_sources_sql = ",".join("?" for _ in DEFAULT_ALLOWED_SOURCES)
    params.extend(sorted(DEFAULT_ALLOWED_SOURCES))
    if path_like:
        path_sql = "AND op.relative_path LIKE ?"
        params.append(path_like)
    token_sql = "AND op.token_status = 'ok'"
    if include_literal_changed:
        token_sql = "AND op.token_status IN ('ok', 'literal_changed')"
    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT ?"
        params.append(limit)
    rows = conn.execute(
        f"""
        SELECT
            op.*,
            s.spanish_text,
            sc.locked AS existing_locked,
            sc.confirmation_level AS existing_confirmation_level
        FROM offline_proposals op
        JOIN source_segments s ON s.id = op.segment_id
        LEFT JOIN segment_confirmations sc ON sc.segment_id = op.segment_id
        WHERE op.run_id = ?
          AND op.status = 'auto_ready'
          AND op.confidence_score >= ?
          AND op.apply_result IS NULL
          AND sc.segment_id IS NULL
          AND op.proposal_source IN ({allowed_sources_sql})
          {token_sql}
          {path_sql}
        ORDER BY
            op.confidence_score DESC,
            op.segment_id ASC
        {limit_sql}
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def validate_candidate(item: dict, include_literal_changed: bool) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    status = token_status(item["spanish_text"], item["proposed_text"])
    try:
        rules = json.loads(item.get("rules_json") or "[]")
    except json.JSONDecodeError:
        rules = []
    risk_terms = visible_spanish_risk_terms(item["proposed_text"])
    literal_risk_terms = translatable_literal_risk_terms(item["proposed_text"])
    quality = local_quality_validator.validate_text(item["proposed_text"])
    issues = issue_codes(quality)
    punctuation_only_clean = is_clean_punctuation_only_proposal(
        rules,
        quality,
        item["proposed_text"],
        risk_terms,
    )
    if status == "mismatch":
        reasons.append("token_structure_mismatch")
    if status == "literal_changed" and not include_literal_changed:
        reasons.append("literal_changed_not_allowed")
    if item["token_status"] != status:
        reasons.append("stored_token_status_changed")
    if has_separated_gender_suffix(item["proposed_text"]):
        reasons.append("gender_token_suffix_separated")
    if has_punctuation_repair_risk(item["proposed_text"]):
        reasons.append("punctuation_repair_risk")
    if "spanish_residue_in_literal" in issues:
        reasons.append("spanish_residue_in_literal")
    if literal_risk_terms:
        reasons.append("translatable_literal_risk_terms")
    if risk_terms:
        reasons.append("visible_spanish_risk_terms")
    if quality["high_issue_count"] > 0 and not punctuation_only_clean:
        reasons.append("remaining_high_issues")
    if quality["medium_issue_count"] > 0 and not punctuation_only_clean:
        reasons.append("remaining_medium_issues")
    if protected_tokens(item["spanish_text"]) != protected_tokens(item["proposed_text"]) and status != "literal_changed":
        reasons.append("protected_tokens_mismatch")
    return not reasons, reasons


def apply_candidates(conn, candidates: list[dict], reviewer: str) -> None:
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
        VALUES (?, 'auto_confirmed', ?, 'offline_proposals', ?, 0, ?, NULL, NULL, ?, ?, ?)
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
                item["proposed_text"],
                item["proposal_source"],
                item["confidence_score"],
                reviewer,
                timestamp,
                timestamp,
                timestamp,
            )
            for item in candidates
        ],
    )
    conn.executemany(
        """
        UPDATE offline_proposals
        SET applied_at = ?,
            apply_result = 'applied',
            updated_at = ?
        WHERE id = ?
        """,
        [(timestamp, timestamp, item["id"]) for item in candidates],
    )


def mark_skipped(conn, skipped: list[tuple[int, str]]) -> None:
    if not skipped:
        return
    timestamp = now()
    conn.executemany(
        """
        UPDATE offline_proposals
        SET apply_result = ?,
            updated_at = ?
        WHERE id = ?
        """,
        [(reason, timestamp, proposal_id) for proposal_id, reason in skipped],
    )


def build_report(
    started_at: datetime,
    run_id: int,
    apply: bool,
    min_score: float,
    limit: int | None,
    path_like: str | None,
    include_literal_changed: bool,
    accepted: list[dict],
    skipped: list[tuple[int, str]],
) -> list[str]:
    elapsed = datetime.now() - started_at
    package_counts = Counter(item["relative_path"] for item in accepted)
    source_counts = Counter(item["proposal_source"] for item in accepted)
    skipped_counts = Counter(reason for _, reason in skipped)
    lines = [
        "Apply offline proposals report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Apply: {apply}",
        f"Min score: {min_score}",
        f"Limit: {limit or 'none'}",
        f"Path filter: {path_like or 'none'}",
        f"Include literal-changed token proposals: {include_literal_changed}",
        "",
        "Summary:",
        f"- Valid candidates selected: {len(accepted)}",
        f"- Confirmations written: {len(accepted) if apply else 0}",
        f"- Skipped after revalidation: {len(skipped)}",
        "",
        "Proposal sources:",
        *[f"- {source}: {count}" for source, count in source_counts.most_common()],
        "",
        "Skipped reasons:",
        *[f"- {reason}: {count}" for reason, count in skipped_counts.most_common()],
        "",
        "Top packages:",
        *[f"- {path}: {count}" for path, count in package_counts.most_common(30)],
        "",
        "Preview:",
    ]
    for item in accepted[:60]:
        before = (item["original_text"] or "").replace("\n", "\\n")
        after = (item["proposed_text"] or "").replace("\n", "\\n")
        if len(before) > 180:
            before = before[:180] + "..."
        if len(after) > 180:
            after = after[:180] + "..."
        lines.extend(
            [
                f"- segment {item['segment_id']} | {item['confidence_score']:.3f} | {item['relative_path']}::{item['source_key']}",
                f"  before: {before}",
                f"  after:  {after}",
            ]
        )
    if not accepted:
        lines.append("- No candidates selected")
    return lines


def main(
    run_id: int | None = None,
    min_score: float = DEFAULT_MIN_SCORE,
    limit: int | None = None,
    path_like: str | None = None,
    include_literal_changed: bool = False,
    apply: bool = False,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[apply_offline_proposals] Starting offline proposal apply")
    print(f"[apply_offline_proposals] Rule version: {RULE_VERSION}")
    print(f"[apply_offline_proposals] Apply: {apply}")
    print(f"[apply_offline_proposals] Min score: {min_score}")
    print(f"[apply_offline_proposals] Limit: {limit or 'none'}")
    print(f"[apply_offline_proposals] Path filter: {path_like or 'none'}")
    print(f"[apply_offline_proposals] Include literal-changed token proposals: {include_literal_changed}")
    print(f"[apply_offline_proposals] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_run_id = run_id if run_id is not None else latest_run_id(conn)
        if selected_run_id is None:
            raise RuntimeError("No offline_proposal_runs found. Run offline-proposals first.")
        print(f"[apply_offline_proposals] Run id: {selected_run_id}")
        rows = fetch_candidates(conn, selected_run_id, min_score, limit, path_like, include_literal_changed)
        accepted: list[dict] = []
        skipped: list[tuple[int, str]] = []
        for item in rows:
            ok, reasons = validate_candidate(item, include_literal_changed)
            if ok:
                accepted.append(item)
            else:
                skipped.append((item["id"], ",".join(reasons)))
        if apply:
            apply_candidates(conn, accepted, reviewer="offline_proposals")
            mark_skipped(conn, skipped)
            conn.commit()

    report_lines = build_report(
        started_at,
        selected_run_id,
        apply,
        min_score,
        limit,
        path_like,
        include_literal_changed,
        accepted,
        skipped,
    )
    report_path = db.write_report(settings, "apply_offline_proposals", report_lines)
    print(f"[apply_offline_proposals] Valid candidates selected: {len(accepted)}")
    print(f"[apply_offline_proposals] Confirmations written: {len(accepted) if apply else 0}")
    print(f"[apply_offline_proposals] Skipped after revalidation: {len(skipped)}")
    print(f"[apply_offline_proposals] Report: {report_path}")
    print("[apply_offline_proposals] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Promote offline auto-ready proposals to segment confirmations.")
    parser.add_argument("--run-id", type=int, default=None, help="offline_proposal_runs id. Default: latest.")
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--path-like", default=None)
    parser.add_argument(
        "--include-literal-changed",
        action="store_true",
        help="Allow proposals that changed translatable string literals inside CK3 tokens.",
    )
    parser.add_argument("--apply", action="store_true", help="Write auto_confirmed rows. Default is dry-run.")
    args = parser.parse_args()
    main(
        run_id=args.run_id,
        min_score=args.min_score,
        limit=args.limit,
        path_like=args.path_like,
        include_literal_changed=args.include_literal_changed,
        apply=args.apply,
    )
