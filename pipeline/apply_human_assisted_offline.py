from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime

import db
import local_quality_validator
from apply_offline_proposals import latest_run_id, token_status
from offline_residual_proposals import translatable_literal_risk_terms, visible_spanish_risk_terms


RULE_VERSION = "apply_human_assisted_offline_v1"
DEFAULT_LABEL = "human_assisted_offline"
DEFAULT_MIN_SCORE = 0.80

EXTRA_SPANISH_RESIDUE_PATTERN = re.compile(
    r"\b("
    r"invent[oó]|estoy|creaci[oó]n|atrapad[ao]?|"
    r"superar[eé]|llama|sab[ií]a|malnacid[ao]?|"
    r"se trata de mi|mi\b|m[ií]\b|"
    r"sin posesi[oó]n|un mont[oó]n|mucha m[aá]s|ahora mismo|"
    r"lo|la|los|las|una|uno|unos|unas|el|del|al|"
    r"tu|tus|su|sus|eres|es|seguir[aá]s|seguir[aá]|"
    r"vasallo|vasalla|vasallos|vasallas|"
    r"se[ñn]or[ií]o|tama[ñn]o|deudas|tesoro"
    r")\b",
    re.IGNORECASE,
)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def fetch_candidates(conn, run_id: int, min_score: float, limit: int | None) -> list[dict]:
    limit_sql = "LIMIT ?" if limit is not None else ""
    params: list[object] = [run_id, min_score]
    if limit is not None:
        params.append(limit)
    rows = conn.execute(
        f"""
        SELECT
            op.*,
            s.spanish_text,
            sc.segment_id AS existing_confirmation
        FROM offline_proposals op
        JOIN source_segments s ON s.id = op.segment_id
        LEFT JOIN segment_confirmations sc ON sc.segment_id = op.segment_id
        WHERE op.run_id = ?
          AND op.status = 'needs_review'
          AND op.confidence_score >= ?
          AND op.token_status = 'ok'
          AND op.reasons_json = '["punctuation_change_requires_review"]'
          AND op.proposal_source LIKE '%inline_literal%'
          AND sc.segment_id IS NULL
        ORDER BY op.confidence_score DESC, op.segment_id ASC
        {limit_sql}
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def validate_candidate(row: dict) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    proposed = row["proposed_text"] or ""
    status = token_status(row["spanish_text"], proposed)
    if status != "ok":
        reasons.append(f"token_status:{status}")
    quality = local_quality_validator.validate_text(proposed)
    if quality["issue_count"]:
        reasons.append("quality_issues")
    if visible_spanish_risk_terms(proposed):
        reasons.append("visible_spanish_risk_terms")
    if translatable_literal_risk_terms(proposed):
        reasons.append("translatable_literal_risk_terms")
    if EXTRA_SPANISH_RESIDUE_PATTERN.search(proposed):
        reasons.append("extra_spanish_residue")
    if not proposed.strip():
        reasons.append("empty_proposed")
    return not reasons, reasons


def apply_confirmations(conn, accepted: list[dict], reviewer: str) -> None:
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
        VALUES (?, 'human_confirmed', ?, 'human_assisted_offline', ?, 0, ?, NULL, NULL, ?, ?, ?)
        ON CONFLICT(segment_id) DO UPDATE SET
            confirmation_level = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmation_level
                ELSE 'human_confirmed'
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
                row["segment_id"],
                row["proposed_text"],
                DEFAULT_LABEL,
                row["confidence_score"],
                reviewer,
                timestamp,
                timestamp,
                timestamp,
            )
            for row in accepted
        ],
    )
    conn.executemany(
        """
        UPDATE offline_proposals
        SET applied_at = ?,
            apply_result = 'human_assisted_applied',
            updated_at = ?
        WHERE id = ?
        """,
        [(timestamp, timestamp, row["id"]) for row in accepted],
    )


def build_report(
    started_at: datetime,
    run_id: int,
    apply: bool,
    min_score: float,
    limit: int | None,
    accepted: list[dict],
    skipped: list[tuple[dict, list[str]]],
) -> list[str]:
    package_counts = Counter(row["relative_path"] for row in accepted)
    source_counts = Counter(row["proposal_source"] for row in accepted)
    skip_counts = Counter(reason for _, reasons in skipped for reason in reasons)
    lines = [
        "Apply human-assisted offline report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {datetime.now() - started_at}",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Apply: {apply}",
        f"Min score: {min_score}",
        f"Limit: {limit or 'none'}",
        "",
        "Summary:",
        f"- Accepted: {len(accepted)}",
        f"- Confirmations written: {len(accepted) if apply else 0}",
        f"- Skipped: {len(skipped)}",
        "",
        "Accepted sources:",
        *[f"- {source}: {count}" for source, count in source_counts.most_common()],
        "",
        "Skipped reasons:",
        *[f"- {reason}: {count}" for reason, count in skip_counts.most_common()],
        "",
        "Top packages:",
        *[f"- {path}: {count}" for path, count in package_counts.most_common(40)],
        "",
        "Accepted preview:",
    ]
    for row in accepted[:50]:
        sample = (row["proposed_text"] or "").replace("\n", "\\n").replace("\t", "\\t")
        if len(sample) > 220:
            sample = sample[:220] + "..."
        lines.append(f"- segment {row['segment_id']} | {row['relative_path']}::{row['source_key']} | {sample}")
    return lines


def main(
    run_id: int | None = None,
    min_score: float = DEFAULT_MIN_SCORE,
    limit: int | None = None,
    apply: bool = False,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[apply_human_assisted_offline] Starting human-assisted offline apply")
    print(f"[apply_human_assisted_offline] Rule version: {RULE_VERSION}")
    print(f"[apply_human_assisted_offline] Apply: {apply}")
    print(f"[apply_human_assisted_offline] Min score: {min_score}")
    print(f"[apply_human_assisted_offline] Limit: {limit or 'none'}")
    print(f"[apply_human_assisted_offline] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_run_id = run_id if run_id is not None else latest_run_id(conn)
        if selected_run_id is None:
            raise RuntimeError("No offline_proposal_runs found. Run offline-proposals first.")
        rows = fetch_candidates(conn, selected_run_id, min_score, limit)
        accepted: list[dict] = []
        skipped: list[tuple[dict, list[str]]] = []
        for row in rows:
            ok, reasons = validate_candidate(row)
            if ok:
                accepted.append(row)
            else:
                skipped.append((row, reasons))
        if apply:
            apply_confirmations(conn, accepted, reviewer="codex_human_assisted")
            conn.commit()

    report = build_report(
        started_at=started_at,
        run_id=selected_run_id,
        apply=apply,
        min_score=min_score,
        limit=limit,
        accepted=accepted,
        skipped=skipped,
    )
    report_path = db.write_report(settings, "apply_human_assisted_offline", report)
    print(f"[apply_human_assisted_offline] Run id: {selected_run_id}")
    print(f"[apply_human_assisted_offline] Accepted: {len(accepted)}")
    print(f"[apply_human_assisted_offline] Confirmations written: {len(accepted) if apply else 0}")
    print(f"[apply_human_assisted_offline] Skipped: {len(skipped)}")
    print(f"[apply_human_assisted_offline] Report: {report_path}")
    print("[apply_human_assisted_offline] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apply reviewed-looking offline proposals as human-assisted confirmations.")
    parser.add_argument("--run-id", type=int, default=None)
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    main(run_id=args.run_id, min_score=args.min_score, limit=args.limit, apply=args.apply)
