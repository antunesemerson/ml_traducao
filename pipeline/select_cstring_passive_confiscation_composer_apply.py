from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import replace_quoted_text
from local_quality_validator import validate_text


RULE_VERSION = "select_cstring_passive_confiscation_composer_apply_v1"
DEFAULT_CHECKPOINT_RUN_ID = 1
EXPECTED_SEGMENT_ID = 71709
EXPECTED_RELATIVE_PATH = "dlc/tgp/dlc_tgp_japan_wars_l_spanish.yml"
EXPECTED_SOURCE_KEY = "replace_ceremonial_regent_faction_war_victory_desc"
EXPECTED_DECISION = "ready_passive_confiscation_sentence_composer"
CONFIRMATION_LEVEL = "auto_confirmed"
CONFIRMATION_SOURCE = "select_cstring_passive_confiscation_composer_production"
CONFIRMATION_LABEL = "passive_confiscation_sentence_composer:71709"
REVIEWER = "production_select_cstring_passive_confiscation_composer_apply"
APPROVED_TEXT = (
    "[defender.GetShortUIName|U] ser\u00e1 encarcerad[defender.Custom('ES_OA')] por "
    "[Select_CString( attacker.IsLocalPlayer, 'voc\u00ea', attacker.GetShortUIName )] "
    "e todas as suas terras ser\u00e3o confiscadas."
)


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def canonical(value: str | None) -> str:
    return " ".join((value or "").split())


def sha256_text(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def normalize_known_checkpoint_mojibake(value: str) -> str:
    return value.replace("voc\u0119", "voc\u00ea").replace("ser\u0103o", "ser\u00e3o")


def report_paths(settings: dict[str, Any], *, apply: bool, checkpoint_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    mode = "apply" if apply else "apply_dry_run"
    base = reports_dir / f"{stamp}_select_cstring_passive_confiscation_composer_{mode}_checkpoint_{checkpoint_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_state_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE total_segments > 1000
          AND finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    return int(row["id"]) if row else 0


def latest_confirmation(conn, segment_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM segment_confirmations
        WHERE segment_id = ?
        ORDER BY updated_at DESC, confirmed_at DESC, id DESC
        LIMIT 1
        """,
        (segment_id,),
    ).fetchone()
    return dict(row) if row else None


def file_text_at(output_root: Path, row: dict[str, Any]) -> tuple[str, str, int]:
    output_path = output_root / Path(as_text(row["relative_path"]))
    if not output_path.exists():
        raise RuntimeError(f"Output file does not exist: {output_path}")
    lines = output_path.read_text(encoding="utf-8-sig").splitlines()
    line_index = int(row["output_line_number"]) - 1
    if line_index < 0 or line_index >= len(lines):
        raise RuntimeError(f"Output line out of range for segment {row['segment_id']}")
    raw_line = lines[line_index]
    stripped = raw_line.lstrip()
    if not stripped.startswith(f"{EXPECTED_SOURCE_KEY}:"):
        raise RuntimeError(f"Output line key mismatch for segment {row['segment_id']}")
    first_quote = raw_line.find('"')
    last_quote = raw_line.rfind('"')
    if first_quote < 0 or last_quote <= first_quote:
        raise RuntimeError(f"Line without quoted value for segment {row['segment_id']}")
    return raw_line[first_quote + 1 : last_quote], raw_line, line_index


def validation_has_high_block(value: str) -> tuple[bool, list[dict[str, Any]]]:
    result = validate_text(value)
    issues = result.get("issues") or []
    high_or_blocking = [
        issue
        for issue in issues
        if as_text(issue.get("severity")).lower() in {"high", "error", "critical"}
        or "mojibake" in as_text(issue.get("code")).lower()
        or "spanish_residue" in as_text(issue.get("code")).lower()
    ]
    return bool(high_or_blocking), issues


def validate_checkpoint(conn, checkpoint_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_select_cstring_passive_confiscation_composer_checkpoint_runs
        WHERE id = ?
        LIMIT 1
        """,
        (checkpoint_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Checkpoint run {checkpoint_run_id} not found.")
    checkpoint = dict(row)
    expected = {
        "rule_version": "issue_select_cstring_passive_confiscation_composer_checkpoint_v1",
        "composer_name": "select_cstring_passive_confiscation_sentence_composer_v1",
        "composer_status": "shadow_learning_only",
        "source_partial_run_id": 5,
        "candidate_count": 1,
        "checkpoint_allowed_count": 1,
        "blocked_count": 0,
        "production_release_allowed": 0,
    }
    for key, value in expected.items():
        if checkpoint.get(key) != value:
            raise RuntimeError(f"Checkpoint {key} mismatch: expected {value!r}, got {checkpoint.get(key)!r}")
    return checkpoint


def fetch_items(conn, checkpoint_run_id: int, state_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.id AS checkpoint_item_id,
            item.run_id,
            item.source_partial_run_id,
            item.source_partial_item_id,
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.decision,
            item.checkpoint_allowed,
            item.segment_closure_candidate,
            item.current_text,
            item.corrected_text,
            item.block_reason,
            item.rationale,
            item.validation_issues_json,
            item.production_release_allowed,
            source.is_active,
            source.source_line_number,
            output.output_line_number,
            output.portuguese_text AS live_output_text,
            confirmation.confirmation_level,
            confirmation.confirmation_source,
            confirmation.confirmation_label,
            confirmation.confirmed_text,
            COALESCE(confirmation.locked, 0) AS confirmation_locked,
            state.final_state AS live_final_state,
            COALESCE(state.needs_output_apply, 0) AS live_needs_output_apply
        FROM ml_issue_select_cstring_passive_confiscation_composer_checkpoint_items item
        JOIN source_segments source
          ON source.id = item.segment_id
        JOIN output_segments output
          ON output.segment_id = item.segment_id
        JOIN segment_confirmations confirmation
          ON confirmation.segment_id = item.segment_id
        LEFT JOIN segment_state_items state
          ON state.segment_id = item.segment_id
         AND state.run_id = ?
        WHERE item.run_id = ?
        ORDER BY item.segment_id
        """,
        (state_run_id, checkpoint_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def classify_row(output_root: Path, row: dict[str, Any], checkpoint_run_id: int) -> dict[str, Any]:
    result = dict(row)
    result["status"] = "ready"
    result["block_reason_detail"] = ""
    result["approved_text"] = APPROVED_TEXT

    def mark(status: str, reason: str = "") -> dict[str, Any]:
        result["status"] = status
        result["block_reason_detail"] = reason
        return result

    if int(row.get("run_id") or 0) != checkpoint_run_id:
        return mark("blocked", "wrong_checkpoint_run")
    if int(row.get("segment_id") or 0) != EXPECTED_SEGMENT_ID:
        return mark("blocked", "unexpected_segment_id")
    if as_text(row.get("relative_path")) != EXPECTED_RELATIVE_PATH:
        return mark("blocked", "unexpected_relative_path")
    if as_text(row.get("source_key")) != EXPECTED_SOURCE_KEY:
        return mark("blocked", "unexpected_source_key")
    if int(row.get("checkpoint_allowed") or 0) != 1:
        return mark("blocked", "checkpoint_not_allowed")
    if int(row.get("segment_closure_candidate") or 0) != 1:
        return mark("blocked", "not_segment_closure_candidate")
    if as_text(row.get("decision")) != EXPECTED_DECISION:
        return mark("blocked", "unexpected_decision")
    if int(row.get("production_release_allowed") or 0) != 0:
        return mark("blocked", "production_release_allowed_not_zero")
    if as_text(row.get("block_reason")).strip():
        return mark("blocked", f"checkpoint_block_reason:{row['block_reason']}")
    if int(row.get("is_active") or 0) != 1:
        return mark("stale", "source_not_active")
    if int(row.get("confirmation_locked") or 0) == 1:
        return mark("blocked", "locked_confirmation")
    if int(row.get("live_needs_output_apply") or 0) != 0:
        return mark("blocked", "needs_output_apply_not_0")
    if normalize_known_checkpoint_mojibake(as_text(row.get("corrected_text"))) != APPROVED_TEXT:
        return mark("blocked", "checkpoint_corrected_text_not_approved_text")
    if canonical(as_text(row.get("live_output_text"))) != canonical(as_text(row.get("current_text"))):
        return mark("stale", "live_output_not_checkpoint_current_text")
    if canonical(as_text(row.get("confirmed_text"))) != canonical(as_text(row.get("current_text"))):
        return mark("stale", "confirmation_not_on_current_text")
    high_block, validation_issues = validation_has_high_block(APPROVED_TEXT)
    result["approved_validation_issues_json"] = json.dumps(validation_issues, ensure_ascii=False)
    if high_block:
        return mark("blocked", "approved_text_validation_high_or_blocking")
    if as_text(row.get("validation_issues_json")).strip() not in {"", "[]"}:
        return mark("blocked", "checkpoint_validation_issues_not_empty")
    try:
        file_text, _, _ = file_text_at(output_root, row)
    except RuntimeError as exc:
        return mark("blocked", str(exc))
    if canonical(file_text) != canonical(as_text(row.get("live_output_text"))):
        return mark("stale", "file_output_db_output_mismatch")
    if canonical(file_text) == canonical(APPROVED_TEXT):
        if row["confirmation_source"] == CONFIRMATION_SOURCE and row["confirmation_label"] == CONFIRMATION_LABEL:
            return mark("already_applied", "")
        return mark("blocked", "approved_text_already_on_disk_without_expected_confirmation")
    return result


def validate_counts(rows: list[dict[str, Any]]) -> Counter:
    counts: Counter = Counter(row["status"] for row in rows)
    if len(rows) != 1:
        raise RuntimeError(f"Expected candidates=1, got {len(rows)}.")
    for key, expected in {
        "ready": 1,
        "stale": 0,
        "blocked": 0,
        "already_applied": 0,
    }.items():
        if counts[key] != expected:
            raise RuntimeError(f"Expected {key}={expected}, got {counts[key]}.")
    return counts


def upsert_confirmation(conn, *, segment_id: int, confirmed_text: str, now: str) -> bool:
    confirmation = latest_confirmation(conn, segment_id)
    if confirmation and int(confirmation.get("locked") or 0) == 1:
        raise RuntimeError(f"Refusing locked confirmation for segment {segment_id}")
    already = (
        confirmation
        and as_text(confirmation.get("confirmation_level")) == CONFIRMATION_LEVEL
        and as_text(confirmation.get("confirmation_source")) == CONFIRMATION_SOURCE
        and as_text(confirmation.get("confirmation_label")) == CONFIRMATION_LABEL
        and as_text(confirmation.get("confirmed_text")) == confirmed_text
    )
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
            reviewer,
            confirmed_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, 0, 1.0, ?, ?, ?)
        ON CONFLICT(segment_id) DO UPDATE SET
            confirmation_level = CASE WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmation_level ELSE excluded.confirmation_level END,
            confirmed_text = CASE WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmed_text ELSE excluded.confirmed_text END,
            confirmation_source = CASE WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmation_source ELSE excluded.confirmation_source END,
            confirmation_label = CASE WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmation_label ELSE excluded.confirmation_label END,
            locked = CASE WHEN segment_confirmations.locked = 1 THEN segment_confirmations.locked ELSE excluded.locked END,
            confidence_score = CASE WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confidence_score ELSE excluded.confidence_score END,
            reviewer = CASE WHEN segment_confirmations.locked = 1 THEN segment_confirmations.reviewer ELSE excluded.reviewer END,
            confirmed_at = COALESCE(segment_confirmations.confirmed_at, excluded.confirmed_at),
            updated_at = CASE WHEN segment_confirmations.locked = 1 THEN segment_confirmations.updated_at ELSE excluded.updated_at END
        """,
        (segment_id, CONFIRMATION_LEVEL, confirmed_text, CONFIRMATION_SOURCE, CONFIRMATION_LABEL, REVIEWER, now, now),
    )
    return not bool(already)


def apply_ready(conn, *, output_root: Path, row: dict[str, Any], now: str) -> dict[str, int]:
    output_path = output_root / Path(as_text(row["relative_path"]))
    lines = output_path.read_text(encoding="utf-8-sig").splitlines()
    file_text, raw_line, line_index = file_text_at(output_root, row)
    if canonical(file_text) != canonical(as_text(row["live_output_text"])):
        raise RuntimeError(f"Live file output drift for segment {row['segment_id']}")
    lines[line_index] = replace_quoted_text(raw_line, APPROVED_TEXT)
    promoted = upsert_confirmation(conn, segment_id=EXPECTED_SEGMENT_ID, confirmed_text=APPROVED_TEXT, now=now)
    conn.execute(
        """
        UPDATE output_segments
        SET portuguese_text = ?,
            output_raw_line = ?,
            portuguese_hash = ?,
            last_indexed_at = ?
        WHERE segment_id = ?
        """,
        (APPROVED_TEXT, lines[line_index], sha256_text(APPROVED_TEXT), now, EXPECTED_SEGMENT_ID),
    )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return {
        "applied": 1,
        "confirmation_promoted": 1 if promoted else 0,
        "output_written": 1,
        "files_touched": 1,
    }


def write_reports(
    settings: dict[str, Any],
    *,
    rows: list[dict[str, Any]],
    counts: Counter,
    apply: bool,
    checkpoint_run_id: int,
    apply_counts: dict[str, int] | None = None,
) -> tuple[Path, Path, Path]:
    txt_path, csv_path, jsonl_path = report_paths(settings, apply=apply, checkpoint_run_id=checkpoint_run_id)
    fields = [
        "checkpoint_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "output_line_number",
        "decision",
        "status",
        "block_reason",
        "block_reason_detail",
        "current_text",
        "corrected_text",
        "approved_text",
        "validation_issues_json",
        "approved_validation_issues_json",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    apply_counts = apply_counts or {}
    lines = [
        "Select_CString passive confiscation composer protected apply" if apply else "Select_CString passive confiscation composer protected apply dry-run",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Apply: {apply}",
        "",
        "Counts:",
        f"- candidates: {len(rows)}",
        f"- ready: {counts['ready']}",
        f"- stale: {counts['stale']}",
        f"- blocked: {counts['blocked']}",
        f"- already_applied: {counts['already_applied']}",
        f"- applied: {apply_counts.get('applied', 0)}",
        f"- confirmation_promoted: {apply_counts.get('confirmation_promoted', 0)}",
        f"- output_written: {apply_counts.get('output_written', 0)}",
        f"- files_touched: {apply_counts.get('files_touched', 0)}",
        "",
        "Segments:",
    ]
    for row in rows:
        lines.append(f"- {row['segment_id']} | {row['source_key']} | status={row['status']}")
        if row["block_reason_detail"]:
            lines.append(f"  block_reason={row['block_reason_detail']}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path


def run(*, checkpoint_run_id: int, apply: bool) -> tuple[Counter, dict[str, int], tuple[Path, Path, Path]]:
    settings = db.load_settings()
    output_root = db.project_path(settings["output_spanish"])
    apply_counts = {"applied": 0, "confirmation_promoted": 0, "output_written": 0, "files_touched": 0}
    with db.connect(settings) as conn:
        validate_checkpoint(conn, checkpoint_run_id)
        state_run_id = latest_state_run_id(conn)
        rows = [classify_row(output_root, row, checkpoint_run_id) for row in fetch_items(conn, checkpoint_run_id, state_run_id)]
        counts = validate_counts(rows)
        if apply:
            now = db.utc_now()
            apply_counts.update(apply_ready(conn, output_root=output_root, row=rows[0], now=now))
            conn.commit()
        paths = write_reports(
            settings,
            rows=rows,
            counts=counts,
            apply=apply,
            checkpoint_run_id=checkpoint_run_id,
            apply_counts=apply_counts,
        )
    return counts, apply_counts, paths


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint-run-id", type=int, default=DEFAULT_CHECKPOINT_RUN_ID)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        counts, apply_counts, paths = run(checkpoint_run_id=args.checkpoint_run_id, apply=args.apply)
    except Exception as exc:
        print(f"[select_cstring_passive_confiscation_composer_apply] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print("[select_cstring_passive_confiscation_composer_apply] Completed")
    print(f"[select_cstring_passive_confiscation_composer_apply] Apply: {args.apply}")
    print(f"[select_cstring_passive_confiscation_composer_apply] Candidates: 1")
    print(f"[select_cstring_passive_confiscation_composer_apply] Ready: {counts['ready']}")
    print(f"[select_cstring_passive_confiscation_composer_apply] Stale: {counts['stale']}")
    print(f"[select_cstring_passive_confiscation_composer_apply] Blocked: {counts['blocked']}")
    print(f"[select_cstring_passive_confiscation_composer_apply] Already applied: {counts['already_applied']}")
    print(f"[select_cstring_passive_confiscation_composer_apply] Applied: {apply_counts.get('applied', 0)}")
    print(f"[select_cstring_passive_confiscation_composer_apply] Confirmation promoted: {apply_counts.get('confirmation_promoted', 0)}")
    print(f"[select_cstring_passive_confiscation_composer_apply] Output written: {apply_counts.get('output_written', 0)}")
    print(f"[select_cstring_passive_confiscation_composer_apply] Files touched: {apply_counts.get('files_touched', 0)}")
    print(f"[select_cstring_passive_confiscation_composer_apply] Report: {paths[0]}")
    print(f"[select_cstring_passive_confiscation_composer_apply] CSV: {paths[1]}")
    print(f"[select_cstring_passive_confiscation_composer_apply] JSONL: {paths[2]}")


if __name__ == "__main__":
    main()
