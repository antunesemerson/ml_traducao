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


RULE_VERSION = "actionable_pending_reviewed_repair_dry_run_v1"
REVIEWER = "codex_learning_front"
QUEUE_39_SOURCE = "select_cstring_governed_bridge_production"
QUEUE_39_LABEL_PREFIX = "select_cstring_governed_bridge:proposal_"
QUEUE_40_SOURCE = "actionable_pending_reviewed_repair_production"
QUEUE_40_LABEL = "actionable_pending_lifecycle_gap_repair:queue_40"
EXPECTED_QUEUE_RUN_IDS = (39, 40)
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


def short(value: str | None, limit: int = 180) -> str:
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
    base = reports_dir / f"{stamp}_actionable_pending_reviewed_repair_dry_run"
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


def humanish_confirmation(row: dict[str, Any]) -> bool:
    confirmation_level = str(row.get("confirmation_level") or "").lower()
    confirmation_source = str(row.get("confirmation_source") or "").lower()
    confirmation_label = str(row.get("confirmation_label") or "").lower()
    review_state = str(row.get("review_state") or "").lower()
    if any(token in confirmation_level for token in ("human", "manual")):
        return True
    if any(token in review_state for token in ("human", "manual")):
        return True
    return any(token in f"{confirmation_source} {confirmation_label}" for token in ("human", "manual", "gemini"))


def fetch_rows(conn, *, state_run_id: int) -> list[dict[str, Any]]:
    placeholders = ", ".join("?" for _ in EXPECTED_QUEUE_RUN_IDS)
    rows = conn.execute(
        f"""
        WITH issue_summary AS (
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
          decision.normalized_decision,
          decision.evidence_label,
          decision.corrected_text,
          decision.reviewer,
          decision.valid,
          decision.created_at AS decision_created_at,
          state.id AS state_item_id,
          state.final_state,
          state.state_group,
          state.apply_state,
          state.review_state,
          state.confirmed_matches_output,
          state.lifecycle_policy_allowed,
          confirmation.id AS confirmation_id,
          confirmation.confirmed_text,
          confirmation.confirmation_level,
          confirmation.confirmation_source,
          confirmation.confirmation_label,
          confirmation.locked,
          confirmation.updated_at AS confirmation_updated_at,
          output.output_line_number,
          output.portuguese_text AS output_text,
          source.english_text,
          source.spanish_text,
          COALESCE(issue_summary.issue_count, 0) AS issue_count,
          COALESCE(issue_summary.high_issue_count, 0) AS high_issue_count
        FROM ml_issue_review_decisions decision
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
        WHERE decision.queue_run_id IN ({placeholders})
          AND decision.valid = 1
          AND decision.reviewer = ?
          AND decision.normalized_decision = 'needs_repair'
          AND decision.evidence_label = 'repair_required'
        ORDER BY decision.queue_run_id, decision.segment_id
        """,
        (state_run_id, *EXPECTED_QUEUE_RUN_IDS, REVIEWER),
    ).fetchall()
    return [dict(row) for row in rows]


def expected_confirmation(row: dict[str, Any]) -> tuple[str, str]:
    if int(row["queue_run_id"]) == 39:
        return QUEUE_39_SOURCE, f"{QUEUE_39_LABEL_PREFIX}{row['queue_item_id'] or row['decision_id']}"
    return QUEUE_40_SOURCE, QUEUE_40_LABEL


def evaluate_row(row: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    corrected = str(row.get("corrected_text") or "")
    output = row.get("output_text")
    confirmed = row.get("confirmed_text")

    if int(row.get("valid") or 0) != 1:
        reasons.append("decision_not_valid")
    if row.get("reviewer") != REVIEWER:
        reasons.append("wrong_reviewer")
    if not corrected.strip():
        reasons.append("missing_corrected_text")
    if row.get("final_state") is None:
        reasons.append("missing_segment_state")
    if int(row.get("queue_run_id") or 0) == 39 and row.get("final_state") != "reopen_auto_confirmed_autofix":
        reasons.append("unexpected_select_cstring_state")
    if int(row.get("queue_run_id") or 0) == 40 and row.get("final_state") not in {
        "reopen_auto_confirmed",
        "pending_unknown_open",
    }:
        reasons.append("unexpected_actionable_state")
    if row.get("output_line_number") is None:
        reasons.append("missing_output_line")
    if output is None:
        reasons.append("missing_output_text")
    if confirmed is None:
        reasons.append("missing_confirmation")
    if int(row.get("locked") or 0) == 1 or humanish_confirmation(row):
        reasons.append("human_or_locked_confirmation")
    if int(row.get("high_issue_count") or 0) != 0:
        reasons.append("current_high_issue_signal")
    quality_issues = blocking_validation_issues(corrected)
    if quality_issues:
        reasons.append("quality_block:" + ",".join(sorted({str(issue.get("code")) for issue in quality_issues})))
    if corrected == output and corrected == confirmed:
        return "already_applied", reasons
    if reasons:
        return "blocked", reasons
    return "ready", []


def enrich_row(row: dict[str, Any]) -> dict[str, Any]:
    confirmation_source, confirmation_label = expected_confirmation(row)
    status, reasons = evaluate_row(row)
    corrected_text = str(row.get("corrected_text") or "")
    return {
        **row,
        "status": status,
        "block_reasons": ";".join(reasons),
        "expected_confirmation_source": confirmation_source,
        "expected_confirmation_label": confirmation_label,
        "current_output_hash": sha256_text(row.get("output_text")),
        "current_confirmation_hash": sha256_text(row.get("confirmed_text")),
        "corrected_text_hash": sha256_text(corrected_text),
        "output_equals_corrected": int(corrected_text == (row.get("output_text") or "")),
        "confirmation_equals_corrected": int(corrected_text == (row.get("confirmed_text") or "")),
        "confirmed_matches_output_now": int((row.get("confirmed_text") or "") == (row.get("output_text") or "")),
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
        "queue_run_id",
        "decision_id",
        "queue_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "final_state",
        "expected_confirmation_source",
        "expected_confirmation_label",
        "current_output_hash",
        "current_confirmation_hash",
        "corrected_text_hash",
        "output_equals_corrected",
        "confirmation_equals_corrected",
        "confirmed_matches_output_now",
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
    queue_counts = Counter((row["queue_run_id"], row["status"]) for row in rows)
    lines = [
        "Actionable pending reviewed repair dry-run",
        f"Rule version: {RULE_VERSION}",
        f"Segment-state run id: {state_run_id}",
        "",
        "Summary:",
        f"- candidates: {len(rows):,}",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        "",
        "By queue:",
        *[f"- queue {queue}: {status}: {value:,}" for (queue, status), value in sorted(queue_counts.items())],
        "",
        "Candidates:",
    ]
    for row in rows:
        lines.extend(
            [
                f"- {row['status']} | queue={row['queue_run_id']} | segment={row['segment_id']} | {row['relative_path']}:{row['source_line_number']} | {row['source_key']}",
                f"  state={row.get('final_state')}; reasons={row.get('block_reasons') or 'none'}",
                f"  expected_confirmation={row['expected_confirmation_source']} / {row['expected_confirmation_label']}",
                f"  output_hash={row['current_output_hash']}; confirmation_hash={row['current_confirmation_hash']}; corrected_hash={row['corrected_text_hash']}",
                f"  output='{short(row.get('output_text'))}'",
                f"  confirmed='{short(row.get('confirmed_text'))}'",
                f"  corrected='{short(row.get('corrected_text'))}'",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This is a dry-run only.",
            "- It does not write output files and does not alter segment confirmations.",
            "- Real repair apply must be wired into the production flow and approved separately.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, state_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_state_run_id = state_run_id or latest_state_run_id(conn)
        rows = [enrich_row(row) for row in fetch_rows(conn, state_run_id=selected_state_run_id)]
    txt_path, csv_path, jsonl_path = report_paths(settings)
    write_outputs(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        state_run_id=selected_state_run_id,
        rows=rows,
    )
    counts = Counter(row["status"] for row in rows)
    print("[actionable_pending_reviewed_repair_dry_run] Dry-run generated")
    print(f"[actionable_pending_reviewed_repair_dry_run] State run id: {selected_state_run_id}")
    print(f"[actionable_pending_reviewed_repair_dry_run] Candidates: {len(rows)}")
    print(f"[actionable_pending_reviewed_repair_dry_run] Ready: {counts['ready']}")
    print(f"[actionable_pending_reviewed_repair_dry_run] Already applied: {counts['already_applied']}")
    print(f"[actionable_pending_reviewed_repair_dry_run] Blocked: {counts['blocked']}")
    print(f"[actionable_pending_reviewed_repair_dry_run] Report: {txt_path}")
    print(f"[actionable_pending_reviewed_repair_dry_run] CSV: {csv_path}")
    print(f"[actionable_pending_reviewed_repair_dry_run] JSONL: {jsonl_path}")
    return {
        "state_run_id": selected_state_run_id,
        "candidates": len(rows),
        "ready": counts["ready"],
        "already_applied": counts["already_applied"],
        "blocked": counts["blocked"],
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dry-run reviewed repair candidates for actionable pending closure.")
    parser.add_argument("--state-run-id", type=int, default=None)
    args = parser.parse_args()
    main(state_run_id=args.state_run_id)
