from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime

import db


RULE_VERSION = "apply_learned_validation_v1"
DEFAULT_ACTIONS = ("auto_safe",)
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


def latest_run_id(conn) -> int | None:
    row = conn.execute("SELECT MAX(id) AS id FROM learned_validation_runs").fetchone()
    if not row or row["id"] is None:
        return None
    return int(row["id"])


def parse_actions(value: str | None) -> tuple[str, ...]:
    if not value:
        return DEFAULT_ACTIONS
    actions = tuple(action.strip() for action in value.split(",") if action.strip())
    return actions or DEFAULT_ACTIONS


def fetch_candidates(
    conn,
    run_id: int,
    actions: tuple[str, ...],
    min_score: float,
    limit: int | None,
    path_like: str | None,
    max_words: int | None,
    exclude_audit_flags: bool,
    exclude_path_like: tuple[str, ...],
    exclude_fragile_pronouns: bool,
    require_quote_parity: bool,
    require_source_match: bool,
) -> list[dict]:
    placeholders = ",".join("?" for _ in actions)
    params: list[object] = [run_id, *actions, min_score]
    path_sql = ""
    if path_like:
        path_sql = "AND lvi.relative_path LIKE ?"
        params.append(path_like)
    max_words_sql = ""
    if max_words is not None:
        max_words_sql = "AND lvi.word_count <= ?"
        params.append(max_words)
    audit_flags_sql = ""
    if exclude_audit_flags:
        audit_flags_sql = """
          AND lvi.reasons_json NOT LIKE '%technical_row%'
          AND lvi.reasons_json NOT LIKE '%sensitive_path%'
        """
    exclude_path_sql = ""
    for pattern in exclude_path_like:
        exclude_path_sql += " AND lvi.relative_path NOT LIKE ?"
        params.append(pattern)
    fragile_pronoun_sql = ""
    if exclude_fragile_pronouns:
        fragile_pronoun_sql = " ".join(
            "AND COALESCE(lvi.candidate_text, '') NOT LIKE ?" for _ in FRAGILE_PRONOUN_TOKENS
        )
        params.extend(f"%{token}%" for token in FRAGILE_PRONOUN_TOKENS)
    quote_parity_sql = ""
    if require_quote_parity:
        quote_parity_sql = """
          AND (
            length(COALESCE(s.english_text, '')) - length(replace(COALESCE(s.english_text, ''), '"', ''))
          ) = (
            length(COALESCE(lvi.candidate_text, '')) - length(replace(COALESCE(lvi.candidate_text, ''), '"', ''))
          )
        """
    source_match_sql = ""
    if require_source_match:
        source_match_sql = """
          AND COALESCE(lvi.candidate_text, '') = COALESCE(s.english_text, '')
          AND COALESCE(lvi.candidate_text, '') = COALESCE(s.spanish_text, '')
        """
    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT ?"
        params.append(limit)

    rows = conn.execute(
        f"""
        SELECT
            lvi.*,
            sc.segment_id AS existing_confirmation_id
        FROM learned_validation_items lvi
        JOIN source_segments s ON s.id = lvi.segment_id
        LEFT JOIN segment_confirmations sc ON sc.segment_id = lvi.segment_id
        WHERE lvi.run_id = ?
          AND lvi.action IN ({placeholders})
          AND lvi.confidence_score >= ?
          AND lvi.token_status = 'ok'
          AND lvi.high_issue_count = 0
          AND lvi.existing_confirmation_id IS NULL
          {path_sql}
          {max_words_sql}
          {audit_flags_sql}
          {exclude_path_sql}
          {fragile_pronoun_sql}
          {quote_parity_sql}
          {source_match_sql}
        ORDER BY
            lvi.confidence_score DESC,
            lvi.word_count ASC,
            lvi.segment_id ASC
        {limit_sql}
        """.replace("lvi.existing_confirmation_id", "sc.segment_id"),
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


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
        VALUES (
            ?,
            'auto_confirmed',
            ?,
            ?,
            ?,
            0,
            ?,
            NULL,
            NULL,
            ?,
            ?,
            ?
        )
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
                f"learned_validation:{item['candidate_source']}",
                item["action"],
                item["confidence_score"],
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
    run_id: int,
    actions: tuple[str, ...],
    min_score: float,
    limit: int | None,
    path_like: str | None,
    max_words: int | None,
    exclude_audit_flags: bool,
    exclude_path_like: tuple[str, ...],
    exclude_fragile_pronouns: bool,
    require_quote_parity: bool,
    require_source_match: bool,
    apply: bool,
    candidates: list[dict],
) -> list[str]:
    action_counts = Counter(item["action"] for item in candidates)
    source_counts = Counter(item["candidate_source"] for item in candidates)
    package_counts = Counter(item["relative_path"] for item in candidates)
    lines = [
        "Apply learned validation report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Apply: {apply}",
        f"Actions: {', '.join(actions)}",
        f"Min score: {min_score}",
        f"Limit: {limit or 'none'}",
        f"Path filter: {path_like or 'none'}",
        f"Max words: {max_words if max_words is not None else 'none'}",
        f"Exclude audit flags: {exclude_audit_flags}",
        f"Exclude path filters: {', '.join(exclude_path_like) if exclude_path_like else 'none'}",
        f"Exclude fragile pronouns: {exclude_fragile_pronouns}",
        f"Require quote parity: {require_quote_parity}",
        f"Require source match: {require_source_match}",
        "",
        "Summary:",
        f"- Candidates selected: {len(candidates)}",
        f"- Confirmations written: {len(candidates) if apply else 0}",
        "",
        "Actions:",
        *[
            f"- {action}: {count} ({percent(count, len(candidates)):.2f}%)"
            for action, count in action_counts.most_common()
        ],
        "",
        "Candidate sources:",
        *[f"- {source}: {count}" for source, count in source_counts.most_common()],
        "",
        "Top packages:",
        *[f"- {path}: {count}" for path, count in package_counts.most_common(30)],
        "",
        "Preview:",
    ]
    for item in candidates[:50]:
        lines.append(
            f"- segment {item['segment_id']} | {item['confidence_score']:.3f} | "
            f"{item['action']} | {item['relative_path']}::{item['source_key']}"
        )
    if not candidates:
        lines.append("- No candidates selected")
    return lines


def main(
    run_id: int | None = None,
    actions_value: str | None = None,
    min_score: float = 0.95,
    limit: int | None = None,
    path_like: str | None = None,
    max_words: int | None = None,
    exclude_audit_flags: bool = False,
    exclude_path_like: tuple[str, ...] = (),
    exclude_fragile_pronouns: bool = False,
    require_quote_parity: bool = False,
    require_source_match: bool = False,
    apply: bool = False,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    actions = parse_actions(actions_value)
    print("[apply_learned_validation] Starting learned validation apply")
    print(f"[apply_learned_validation] Rule version: {RULE_VERSION}")
    print(f"[apply_learned_validation] Apply: {apply}")
    print(f"[apply_learned_validation] Actions: {', '.join(actions)}")
    print(f"[apply_learned_validation] Min score: {min_score}")
    print(f"[apply_learned_validation] Limit: {limit or 'none'}")
    print(f"[apply_learned_validation] Path filter: {path_like or 'none'}")
    print(f"[apply_learned_validation] Max words: {max_words if max_words is not None else 'none'}")
    print(f"[apply_learned_validation] Exclude audit flags: {exclude_audit_flags}")
    print(
        "[apply_learned_validation] Exclude path filters: "
        f"{', '.join(exclude_path_like) if exclude_path_like else 'none'}"
    )
    print(f"[apply_learned_validation] Exclude fragile pronouns: {exclude_fragile_pronouns}")
    print(f"[apply_learned_validation] Require quote parity: {require_quote_parity}")
    print(f"[apply_learned_validation] Require source match: {require_source_match}")
    print(f"[apply_learned_validation] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_run_id = run_id if run_id is not None else latest_run_id(conn)
        if selected_run_id is None:
            raise RuntimeError("No learned_validation_runs found. Run learned-report first.")
        print(f"[apply_learned_validation] Run id: {selected_run_id}")
        candidates = fetch_candidates(
            conn,
            selected_run_id,
            actions=actions,
            min_score=min_score,
            limit=limit,
            path_like=path_like,
            max_words=max_words,
            exclude_audit_flags=exclude_audit_flags,
            exclude_path_like=exclude_path_like,
            exclude_fragile_pronouns=exclude_fragile_pronouns,
            require_quote_parity=require_quote_parity,
            require_source_match=require_source_match,
        )
        if apply:
            apply_candidates(conn, candidates, reviewer="learned_auto")
            conn.commit()

    elapsed = datetime.now() - started_at
    report_lines = build_report_lines(
        started_at=started_at,
        elapsed=elapsed,
        run_id=selected_run_id,
        actions=actions,
        min_score=min_score,
        limit=limit,
        path_like=path_like,
        max_words=max_words,
        exclude_audit_flags=exclude_audit_flags,
        exclude_path_like=exclude_path_like,
        exclude_fragile_pronouns=exclude_fragile_pronouns,
        require_quote_parity=require_quote_parity,
        require_source_match=require_source_match,
        apply=apply,
        candidates=candidates,
    )
    report_path = db.write_report(settings, "apply_learned_validation", report_lines)
    print(f"[apply_learned_validation] Candidates selected: {len(candidates)}")
    print(f"[apply_learned_validation] Confirmations written: {len(candidates) if apply else 0}")
    print(f"[apply_learned_validation] Report: {report_path}")
    print("[apply_learned_validation] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apply learned validation rows as auto confirmations.")
    parser.add_argument("--run-id", type=int, default=None, help="learned_validation_runs id. Default: latest.")
    parser.add_argument(
        "--actions",
        default=",".join(DEFAULT_ACTIONS),
        help="Comma-separated actions to promote, e.g. auto_safe or auto_safe,auto_safe_audit.",
    )
    parser.add_argument("--min-score", type=float, default=0.95, help="Minimum learned confidence score.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum rows to promote.")
    parser.add_argument("--path-like", default=None, help="Optional SQL LIKE filter for relative_path.")
    parser.add_argument("--max-words", type=int, default=None, help="Only promote rows with word_count at or below this value.")
    parser.add_argument("--exclude-audit-flags", action="store_true", help="Skip rows marked as technical_row or sensitive_path.")
    parser.add_argument(
        "--exclude-path-like",
        action="append",
        default=[],
        help="SQL LIKE path pattern to exclude. May be repeated.",
    )
    parser.add_argument(
        "--exclude-fragile-pronouns",
        action="store_true",
        help="Skip short fragments containing pronoun/object tokens such as GetHerHim or GetHerHis.",
    )
    parser.add_argument(
        "--require-quote-parity",
        action="store_true",
        help="Only promote rows whose English and candidate text have the same double-quote count.",
    )
    parser.add_argument(
        "--require-source-match",
        action="store_true",
        help="Only promote rows where candidate_text, english_text, and spanish_text are identical.",
    )
    parser.add_argument("--apply", action="store_true", help="Write auto_confirmed rows. Default is dry-run.")
    args = parser.parse_args()
    main(
        run_id=args.run_id,
        actions_value=args.actions,
        min_score=args.min_score,
        limit=args.limit,
        path_like=args.path_like,
        max_words=args.max_words,
        exclude_audit_flags=args.exclude_audit_flags,
        exclude_path_like=tuple(args.exclude_path_like or ()),
        exclude_fragile_pronouns=args.exclude_fragile_pronouns,
        require_quote_parity=args.require_quote_parity,
        require_source_match=args.require_source_match,
        apply=args.apply,
    )
