from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator


RULE_VERSION = "actionable_pending_missing_confirmation_bootstrap_dry_run_v1"
REVIEWER = "codex_learning_front"
QUEUE_RUN_ID = 40
TARGET_SEGMENT_ID = 165552
CONFIRMATION_SOURCE = "actionable_pending_missing_confirmation_bootstrap_production"
CONFIRMATION_LABEL = "actionable_pending_missing_confirmation_bootstrap:queue_40"
ALLOWED_CORRECTED_ARTICLES = {"o", "a", "os", "as", "um", "uma"}
ALLOWED_SPANISH_OUTPUT_ARTICLES = {"el", "la", "los", "las", "un", "una"}
BLOCKED_QUALITY_CODES = {
    "spanish_punctuation",
    "mojibake_or_unexpected_script",
    "utf8_mojibake_sequence",
    "replacement_question_mark_mojibake",
    "spanish_residue",
    "spanish_residue_in_literal",
    "gender_token_extra_suffix",
}


def sha256_text(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_article(value: str | None) -> str:
    return " ".join(str(value or "").strip().lower().split())


def short(value: str | None, limit: int = 160) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def latest_state_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL
          AND total_segments > 1000
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No segment-state run found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_actionable_pending_missing_confirmation_bootstrap_dry_run"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def blocking_validation_issues(text: str | None) -> list[dict[str, Any]]:
    validation = local_quality_validator.validate_text(text)
    issues = validation.get("issues") or []
    return [
        issue
        for issue in issues
        if issue.get("severity") in {"high", "error", "critical"}
        or issue.get("code") in BLOCKED_QUALITY_CODES
    ]


def fetch_rows(conn, *, state_run_id: int, segment_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH decision_count AS (
          SELECT segment_id, COUNT(*) AS valid_decision_count
          FROM ml_issue_review_decisions
          WHERE queue_run_id = ?
            AND segment_id = ?
            AND valid = 1
            AND reviewer = ?
            AND normalized_decision = 'needs_repair'
            AND evidence_label = 'repair_required'
          GROUP BY segment_id
        ),
        issue_summary AS (
          SELECT
            segment_id,
            COUNT(*) AS issue_count,
            SUM(CASE WHEN lower(severity) IN ('high', 'error', 'critical') THEN 1 ELSE 0 END) AS high_issue_count
          FROM issues
          GROUP BY segment_id
        )
        SELECT
          decision.id AS decision_id,
          decision.queue_run_id,
          decision.queue_item_id,
          decision.segment_id,
          decision.relative_path,
          decision.source_key,
          decision.source_line_number,
          decision.corrected_text,
          decision.normalized_decision,
          decision.evidence_label,
          decision.reviewer,
          decision.valid,
          decision.created_at AS decision_created_at,
          decision_count.valid_decision_count,
          state.id AS state_item_id,
          state.final_state,
          state.state_group,
          state.review_state,
          state.apply_state,
          confirmation.id AS confirmation_id,
          confirmation.confirmation_level,
          confirmation.confirmation_source,
          confirmation.confirmation_label,
          confirmation.locked,
          output.output_line_number,
          output.portuguese_text AS output_text,
          output.output_raw_line,
          source.spanish_text,
          source.english_text,
          COALESCE(issue_summary.issue_count, 0) AS issue_count,
          COALESCE(issue_summary.high_issue_count, 0) AS high_issue_count
        FROM ml_issue_review_decisions decision
        JOIN decision_count
          ON decision_count.segment_id = decision.segment_id
        JOIN source_segments source
          ON source.id = decision.segment_id
        LEFT JOIN segment_state_items state
          ON state.segment_id = decision.segment_id
         AND state.run_id = ?
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.segment_id = decision.segment_id
        LEFT JOIN output_segments output
          ON output.segment_id = decision.segment_id
        LEFT JOIN issue_summary
          ON issue_summary.segment_id = decision.segment_id
        WHERE decision.queue_run_id = ?
          AND decision.segment_id = ?
          AND decision.valid = 1
          AND decision.reviewer = ?
          AND decision.normalized_decision = 'needs_repair'
          AND decision.evidence_label = 'repair_required'
        ORDER BY decision.id
        """,
        (QUEUE_RUN_ID, segment_id, REVIEWER, state_run_id, QUEUE_RUN_ID, segment_id, REVIEWER),
    ).fetchall()
    return [dict(row) for row in rows]


def evaluate(row: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    corrected = canonical_article(row.get("corrected_text"))
    output = canonical_article(row.get("output_text"))

    if int(row.get("valid_decision_count") or 0) != 1:
        reasons.append("valid_decision_count_not_one")
    if row.get("final_state") != "pending_unknown_open":
        reasons.append("not_pending_unknown_open")
    if row.get("review_state") != "unreviewed":
        reasons.append("not_unreviewed")
    if row.get("apply_state") != "needs_review":
        reasons.append("not_needs_review")
    if row.get("confirmation_id") is not None:
        reasons.append("confirmation_already_exists")
    if int(row.get("locked") or 0) == 1:
        reasons.append("locked_confirmation_exists")
    if corrected not in ALLOWED_CORRECTED_ARTICLES:
        reasons.append("corrected_article_not_allowed")
    if output not in ALLOWED_SPANISH_OUTPUT_ARTICLES:
        reasons.append("output_article_not_allowed")
    if row.get("output_line_number") is None:
        reasons.append("missing_output_line")
    if row.get("output_text") is None:
        reasons.append("missing_output_text")
    if int(row.get("high_issue_count") or 0) != 0:
        reasons.append("current_high_issue_signal")
    quality_issues = blocking_validation_issues(row.get("corrected_text"))
    if quality_issues:
        reasons.append("quality_block:" + ",".join(sorted({str(issue.get("code")) for issue in quality_issues})))
    return ("blocked" if reasons else "ready"), reasons


def enrich(row: dict[str, Any], *, state_run_id: int) -> dict[str, Any]:
    status, reasons = evaluate(row)
    return {
        **row,
        "state_run_id": state_run_id,
        "status": status,
        "block_reasons": ";".join(reasons),
        "expected_confirmation_source": CONFIRMATION_SOURCE,
        "expected_confirmation_label": CONFIRMATION_LABEL,
        "output_text_hash": sha256_text(row.get("output_text")),
        "corrected_text_hash": sha256_text(row.get("corrected_text")),
        "canonical_output_article": canonical_article(row.get("output_text")),
        "canonical_corrected_article": canonical_article(row.get("corrected_text")),
    }


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    state_run_id: int,
    rows: list[dict[str, Any]],
) -> None:
    fieldnames = [
        "status",
        "block_reasons",
        "state_run_id",
        "decision_id",
        "queue_run_id",
        "queue_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "final_state",
        "review_state",
        "apply_state",
        "valid_decision_count",
        "output_line_number",
        "expected_confirmation_source",
        "expected_confirmation_label",
        "output_text_hash",
        "corrected_text_hash",
        "canonical_output_article",
        "canonical_corrected_article",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    counts = Counter(row["status"] for row in rows)
    lines = [
        "Actionable pending missing-confirmation bootstrap dry-run",
        f"Rule version: {RULE_VERSION}",
        f"Validator version: {local_quality_validator.RULE_VERSION}",
        f"Segment-state run id: {state_run_id}",
        "",
        "Summary:",
        f"- candidates: {len(rows):,}",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        "",
        "Candidates:",
    ]
    for row in rows:
        lines.extend(
            [
                f"- {row['status']} | segment={row['segment_id']} | {row['relative_path']}:{row['source_line_number']} | {row['source_key']}",
                f"  state={row.get('final_state')}; review={row.get('review_state')}; apply={row.get('apply_state')}",
                f"  reasons={row.get('block_reasons') or 'none'}",
                f"  decision_id={row.get('decision_id')}; valid_decision_count={row.get('valid_decision_count')}",
                f"  expected_confirmation={CONFIRMATION_SOURCE} / {CONFIRMATION_LABEL}",
                f"  output_hash={row.get('output_text_hash')}; corrected_hash={row.get('corrected_text_hash')}",
                f"  output='{short(row.get('output_text'))}'",
                f"  corrected='{short(row.get('corrected_text'))}'",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This is a dry-run only.",
            "- It does not write output files and does not create segment confirmations.",
            "- Bootstrap apply must be authorized separately and must keep the same hash gates.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, state_run_id: int | None = None, segment_id: int = TARGET_SEGMENT_ID) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_state_run_id = state_run_id or latest_state_run_id(conn)
        rows = [enrich(row, state_run_id=selected_state_run_id) for row in fetch_rows(conn, state_run_id=selected_state_run_id, segment_id=segment_id)]
    txt_path, csv_path, jsonl_path = report_paths(settings)
    write_outputs(txt_path=txt_path, csv_path=csv_path, jsonl_path=jsonl_path, state_run_id=selected_state_run_id, rows=rows)
    counts = Counter(row["status"] for row in rows)
    print("[actionable_pending_missing_confirmation_bootstrap_dry_run] Dry-run generated")
    print(f"[actionable_pending_missing_confirmation_bootstrap_dry_run] State run id: {selected_state_run_id}")
    print(f"[actionable_pending_missing_confirmation_bootstrap_dry_run] Candidates: {len(rows)}")
    print(f"[actionable_pending_missing_confirmation_bootstrap_dry_run] Ready: {counts['ready']}")
    print(f"[actionable_pending_missing_confirmation_bootstrap_dry_run] Blocked: {counts['blocked']}")
    print(f"[actionable_pending_missing_confirmation_bootstrap_dry_run] Report: {txt_path}")
    print(f"[actionable_pending_missing_confirmation_bootstrap_dry_run] CSV: {csv_path}")
    print(f"[actionable_pending_missing_confirmation_bootstrap_dry_run] JSONL: {jsonl_path}")
    return {
        "state_run_id": selected_state_run_id,
        "candidates": len(rows),
        "ready": counts["ready"],
        "blocked": counts["blocked"],
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dry-run missing-confirmation bootstrap for reviewed actionable article repair.")
    parser.add_argument("--state-run-id", type=int, default=None)
    parser.add_argument("--segment-id", type=int, default=TARGET_SEGMENT_ID)
    args = parser.parse_args()
    main(state_run_id=args.state_run_id, segment_id=args.segment_id)
