from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator
from apply_safe_output_updates import protected_tokens


RULE_VERSION = "title_landed_es_reviewed_repair_dry_run_v1"
QUEUE_RUN_ID = 150
AGENT_KEY = "micro_landed_title_spanish_es_suffix_repair"
READY = "ready_confirmation_and_output"
ALREADY_ALIGNED = "already_aligned"
BLOCKED_STATE = "blocked_state"
BLOCKED_QUALITY = "blocked_quality"
BLOCKED_TOKEN_OR_STRUCTURE = "blocked_token_or_structure"
BLOCKED_STALE_DECISION = "blocked_stale_decision"
EXPECTED_FINAL_STATE = "reopen_auto_confirmed_autofix"


def canonical(value: str | None) -> str:
    text = unicodedata.normalize("NFC", value or "").strip()
    return re.sub(r"\s+", " ", text)


def stable_hash(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


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
        raise RuntimeError("No finished segment-state run found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any], queue_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_title_landed_es_reviewed_repair_dry_run_queue_{queue_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def subpolicy_for(row: dict[str, Any]) -> str:
    notes = str(row.get("notes") or "")
    reviewer = str(row.get("reviewer") or "")
    if "stem_overlap" in notes:
        return "source_stem_overlap"
    if "base_same_semantic" in notes:
        return "base_same_semantic"
    if "localized_base_strict" in notes:
        return "localized_base_strict"
    if "high_tier_strict" in notes:
        return "high_tier_strict"
    if "direct_semantic" in notes:
        return "legacy_direct_semantic"
    if "cross_tier" in notes:
        return "legacy_cross_tier"
    if "final_exception" in notes:
        return "final_exception"
    prefix = "codex_landed_title_spanish_es_"
    if reviewer.startswith(prefix):
        return f"legacy_{reviewer.removeprefix(prefix)}"
    return "unknown"


def latest_confirmations(conn) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM segment_confirmations
        ORDER BY segment_id, updated_at DESC, confirmed_at DESC, id DESC
        """
    ).fetchall()
    latest: dict[int, dict[str, Any]] = {}
    for row in rows:
        segment_id = int(row["segment_id"])
        if segment_id not in latest:
            latest[segment_id] = dict(row)
    return latest


def locked_conflicts(conn) -> set[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT segment_id
        FROM segment_confirmations
        WHERE COALESCE(locked, 0) = 1
        """
    ).fetchall()
    return {int(row["segment_id"]) for row in rows}


def fetch_rows(conn, *, queue_run_id: int, state_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
          decision.id AS decision_id,
          decision.run_id AS decision_run_id,
          decision.queue_run_id,
          decision.queue_item_id,
          decision.ledger_run_id,
          decision.ledger_item_id,
          decision.segment_id,
          decision.relative_path AS decision_relative_path,
          decision.source_key AS decision_source_key,
          decision.source_line_number AS decision_source_line_number,
          decision.agent_key,
          decision.issue_family,
          decision.issue_kind,
          decision.queue_bucket,
          decision.normalized_decision,
          decision.evidence_label,
          decision.corrected_text,
          decision.notes,
          decision.reviewer,
          decision.valid,
          decision.validation_status,
          decision.reasons_json,
          decision.created_at AS decision_created_at,
          source.relative_path,
          source.source_key,
          source.source_line_number,
          source.is_active,
          source.spanish_text,
          source.english_text,
          source.old_text,
          output.portuguese_text AS output_text,
          state.final_state,
          state.state_group,
          state.output_state,
          state.review_state,
          state.apply_state,
          state.locked AS state_locked,
          state.needs_output_apply,
          state.needs_reopen,
          state.is_closed
        FROM ml_issue_review_decisions decision
        LEFT JOIN source_segments source
          ON source.id = decision.segment_id
        LEFT JOIN output_segments output
          ON output.segment_id = decision.segment_id
        LEFT JOIN segment_state_items state
          ON state.segment_id = decision.segment_id
         AND state.run_id = ?
        WHERE decision.queue_run_id = ?
          AND decision.agent_key = ?
          AND decision.valid = 1
          AND decision.validation_status = 'accepted'
          AND decision.normalized_decision = 'needs_repair'
          AND COALESCE(TRIM(decision.corrected_text), '') <> ''
        ORDER BY decision.source_key, decision.id
        """,
        (state_run_id, queue_run_id, AGENT_KEY),
    ).fetchall()
    return [dict(row) for row in rows]


def quality_blockers(text: str | None) -> list[str]:
    validation = local_quality_validator.validate_text(text)
    issues = validation.get("issues") or []
    blockers: list[str] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        if str(issue.get("severity") or "").lower() in {"medium", "high", "error", "critical"}:
            blockers.append(str(issue.get("code") or "unknown_quality_issue"))
    return sorted(set(blockers))


def stale_decision_reasons(row: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if int(row.get("valid") or 0) != 1:
        reasons.append("decision_not_valid")
    if row.get("validation_status") != "accepted":
        reasons.append("decision_not_accepted")
    if row.get("normalized_decision") != "needs_repair":
        reasons.append("decision_not_needs_repair")
    if row.get("agent_key") != AGENT_KEY:
        reasons.append("wrong_agent_key")
    if not canonical(row.get("corrected_text")):
        reasons.append("missing_corrected_text")
    if row.get("relative_path") and row.get("decision_relative_path"):
        if row["relative_path"] != row["decision_relative_path"]:
            reasons.append("source_path_changed_since_decision")
    if row.get("source_key") and row.get("decision_source_key"):
        if row["source_key"] != row["decision_source_key"]:
            reasons.append("source_key_changed_since_decision")
    return reasons


def evaluate(
    row: dict[str, Any],
    *,
    confirmations: dict[int, dict[str, Any]],
    locked_segment_ids: set[int],
) -> dict[str, Any]:
    segment_id = int(row["segment_id"])
    corrected = canonical(row.get("corrected_text"))
    output = canonical(row.get("output_text"))
    confirmation = confirmations.get(segment_id)
    confirmed = canonical(None if confirmation is None else confirmation.get("confirmed_text"))
    output_tokens = protected_tokens(output)
    corrected_tokens = protected_tokens(corrected)
    confirmation_locked = int((confirmation or {}).get("locked") or 0)

    reasons: list[str] = []
    state_reasons: list[str] = []
    stale_reasons = stale_decision_reasons(row)
    quality_reasons = quality_blockers(corrected)
    token_structure_reasons: list[str] = []

    if row.get("relative_path") is None or row.get("source_key") is None:
        state_reasons.append("missing_source_segment")
    if int(row.get("is_active") or 0) != 1:
        state_reasons.append("source_not_active")
    if row.get("final_state") != EXPECTED_FINAL_STATE:
        state_reasons.append("unexpected_segment_state")
    if row.get("output_text") is None:
        token_structure_reasons.append("missing_output_text")
    if output_tokens != corrected_tokens:
        token_structure_reasons.append("protected_token_signature_mismatch")
    if segment_id in locked_segment_ids and confirmed != corrected:
        token_structure_reasons.append("locked_confirmation_conflicts_with_corrected_text")
    if confirmation_locked and confirmed != corrected:
        token_structure_reasons.append("latest_confirmation_locked_conflict")

    output_matches = bool(corrected and output == corrected)
    confirmation_matches = bool(corrected and confirmed == corrected)

    if output_matches and confirmation_matches:
        status = ALREADY_ALIGNED
    elif stale_reasons:
        status = BLOCKED_STALE_DECISION
        reasons.extend(stale_reasons)
    elif state_reasons:
        status = BLOCKED_STATE
        reasons.extend(state_reasons)
    elif quality_reasons:
        status = BLOCKED_QUALITY
        reasons.extend(quality_reasons)
    elif token_structure_reasons:
        status = BLOCKED_TOKEN_OR_STRUCTURE
        reasons.extend(token_structure_reasons)
    elif corrected != output or not confirmation_matches:
        status = READY
    else:
        status = BLOCKED_STALE_DECISION
        reasons.append("no_actionable_delta_detected")

    return {
        **row,
        "status": status,
        "block_reasons": ";".join(reasons),
        "subpolicy": subpolicy_for(row),
        "confirmed_text": confirmed,
        "confirmation_id": None if confirmation is None else confirmation.get("id"),
        "confirmation_level": "" if confirmation is None else confirmation.get("confirmation_level") or "",
        "confirmation_source": "" if confirmation is None else confirmation.get("confirmation_source") or "",
        "confirmation_label": "" if confirmation is None else confirmation.get("confirmation_label") or "",
        "confirmation_locked": confirmation_locked,
        "corrected_hash": stable_hash(corrected),
        "output_hash": stable_hash(output),
        "confirmed_hash": stable_hash(confirmed),
        "output_matches_corrected": int(output_matches),
        "confirmation_matches_corrected": int(confirmation_matches),
        "protected_tokens_match": int(output_tokens == corrected_tokens),
        "output_token_signature": json.dumps(dict(output_tokens), ensure_ascii=False, sort_keys=True),
        "corrected_token_signature": json.dumps(dict(corrected_tokens), ensure_ascii=False, sort_keys=True),
        "quality_blockers": ",".join(quality_reasons),
        "corrected_sample": short(corrected),
        "output_sample": short(output),
        "confirmed_sample": short(confirmed),
    }


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    rows: list[dict[str, Any]],
    queue_run_id: int,
    state_run_id: int,
) -> None:
    status_counts = Counter(row["status"] for row in rows)
    blocked_counts = Counter(row["block_reasons"] or row["status"] for row in rows if row["status"].startswith("blocked"))
    subpolicy_counts = Counter(row["subpolicy"] for row in rows)
    subpolicy_status_counts = Counter((row["subpolicy"], row["status"]) for row in rows)
    reviewer_counts = Counter(row.get("reviewer") or "" for row in rows)
    state_counts = Counter(row.get("final_state") or "missing_state" for row in rows)

    fieldnames = [
        "status",
        "block_reasons",
        "subpolicy",
        "decision_id",
        "decision_run_id",
        "queue_run_id",
        "queue_item_id",
        "ledger_run_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_line_number",
        "source_key",
        "reviewer",
        "queue_bucket",
        "issue_family",
        "issue_kind",
        "normalized_decision",
        "evidence_label",
        "final_state",
        "state_group",
        "review_state",
        "apply_state",
        "is_active",
        "corrected_text",
        "output_text",
        "confirmed_text",
        "output_matches_corrected",
        "confirmation_matches_corrected",
        "protected_tokens_match",
        "confirmation_id",
        "confirmation_level",
        "confirmation_source",
        "confirmation_label",
        "confirmation_locked",
        "quality_blockers",
        "corrected_hash",
        "output_hash",
        "confirmed_hash",
        "spanish_text",
        "english_text",
        "old_text",
        "notes",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    ready_rows = [row for row in rows if row["status"] == READY][:30]
    blocked_rows = [row for row in rows if row["status"].startswith("blocked")][:30]
    lines = [
        "Landed title -es suffix reviewed repair dry-run",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Queue run id: {queue_run_id}",
        f"Segment-state run id: {state_run_id}",
        f"Candidates inspected: {len(rows):,}",
        f"Ready confirmation + output: {status_counts.get(READY, 0):,}",
        f"Already aligned: {status_counts.get(ALREADY_ALIGNED, 0):,}",
        f"Blocked total: {sum(value for key, value in status_counts.items() if key.startswith('blocked')):,}",
        f"TXT: {txt_path}",
        f"CSV: {csv_path}",
        f"JSONL: {jsonl_path}",
        "",
        "Status distribution:",
        *[f"- {key}: {value:,}" for key, value in status_counts.most_common()],
        "",
        "Blocked reasons:",
        *[f"- {key}: {value:,}" for key, value in blocked_counts.most_common()],
        "",
        "Subpolicy distribution:",
        *[f"- {key}: {value:,}" for key, value in subpolicy_counts.most_common()],
        "",
        "Subpolicy x status:",
        *[f"- {subpolicy} / {status}: {value:,}" for (subpolicy, status), value in sorted(subpolicy_status_counts.items())],
        "",
        "Reviewer distribution:",
        *[f"- {key}: {value:,}" for key, value in reviewer_counts.most_common()],
        "",
        "Current segment-state distribution:",
        *[f"- {key}: {value:,}" for key, value in state_counts.most_common()],
        "",
        "Ready samples:",
    ]
    for row in ready_rows:
        lines.append(
            f"- segment={row['segment_id']} | {row['relative_path']}:{row['source_line_number']} | "
            f"{row['source_key']} | {row['subpolicy']} | {row['output_sample']!r} -> {row['corrected_sample']!r}"
        )
    lines.append("")
    lines.append("Blocked samples:")
    for row in blocked_rows:
        lines.append(
            f"- {row['status']} | {row['block_reasons']} | segment={row['segment_id']} | "
            f"{row.get('relative_path')}:{row.get('source_line_number')} | {row.get('source_key')}"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This dry-run is read-only.",
            "- It does not create confirmations.",
            "- It does not write output.",
            "- It does not reindex source files.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, queue_run_id: int = QUEUE_RUN_ID, state_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        selected_state_run_id = state_run_id if state_run_id is not None else latest_state_run_id(conn)
        confirmations = latest_confirmations(conn)
        locked_segment_ids = locked_conflicts(conn)
        raw_rows = fetch_rows(conn, queue_run_id=queue_run_id, state_run_id=selected_state_run_id)
        rows = [
            evaluate(row, confirmations=confirmations, locked_segment_ids=locked_segment_ids)
            for row in raw_rows
        ]

    txt_path, csv_path, jsonl_path = report_paths(settings, queue_run_id)
    write_outputs(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        rows=rows,
        queue_run_id=queue_run_id,
        state_run_id=selected_state_run_id,
    )

    counts = Counter(row["status"] for row in rows)
    print("[title_landed_es_reviewed_repair_dry_run] Dry-run generated")
    print(f"[title_landed_es_reviewed_repair_dry_run] Rule version: {RULE_VERSION}")
    print(f"[title_landed_es_reviewed_repair_dry_run] Queue run id: {queue_run_id}")
    print(f"[title_landed_es_reviewed_repair_dry_run] State run id: {selected_state_run_id}")
    print(f"[title_landed_es_reviewed_repair_dry_run] Candidates inspected: {len(rows):,}")
    for key, value in counts.most_common():
        print(f"[title_landed_es_reviewed_repair_dry_run] {key}: {value:,}")
    print(f"[title_landed_es_reviewed_repair_dry_run] Report: {txt_path}")
    print(f"[title_landed_es_reviewed_repair_dry_run] CSV: {csv_path}")
    print(f"[title_landed_es_reviewed_repair_dry_run] JSONL: {jsonl_path}")
    return {
        "queue_run_id": queue_run_id,
        "state_run_id": selected_state_run_id,
        "candidates": len(rows),
        "counts": dict(counts),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read-only dry-run for reviewed landed-title -es suffix repairs.")
    parser.add_argument("--queue-run-id", type=int, default=QUEUE_RUN_ID)
    parser.add_argument("--state-run-id", type=int, default=None)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id, state_run_id=args.state_run_id)
