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


RULE_VERSION = "select_cstring_preterite_transfer_apply_v1"
DEFAULT_CHECKPOINT_RUN_ID = 3
EXPECTED_CHECKPOINT_ITEM_ID = 195
EXPECTED_PARTIAL_RUN_ID = 7
EXPECTED_SEGMENT_ID = 98033
EXPECTED_RELATIVE_PATH = "effects_l_spanish.yml"
EXPECTED_SOURCE_KEY = "transfer_owned_maa_control_global_past"
EXPECTED_TARGET_AGENT = "select_cstring_local_player_preterite_verb_rewrite"
EXPECTED_LEFT_LITERAL = "transferiste"
EXPECTED_RIGHT_LITERAL = "transfiri\u00f3"
EXPECTED_PROPOSED_LITERAL = "transferiu"
EXPECTED_READY_SOURCE = "preterite_ptbr_checkpoint"
CONFIRMATION_LEVEL = "auto_confirmed"
CONFIRMATION_SOURCE = "select_cstring_preterite_transfer_production"
CONFIRMATION_LABEL = "select_cstring_preterite_transfer:98033"
REVIEWER = "production_select_cstring_preterite_transfer_apply"
APPROVED_TEXT = (
    "[TITLE.GetHolder.GetShortUIName|U] "
    "[Select_CString( TITLE.GetHolder.IsLocalPlayer, 'transferiu', 'transferiu' )] "
    "o [army|lE] [TITLE.GetAdjective] para [TARGET_TITLE.GetNameNoTier], "
    "sob o comando de [TARGET_TITLE.GetHolder.GetShortUIName]"
)


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def canonical(value: str | None) -> str:
    return " ".join((value or "").split())


def sha256_text(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def normalize_known_mojibake(value: str) -> str:
    return (
        value.replace("transfiri\u00c3\u00b3", "transfiri\u00f3")
        .replace("Ã³", "\u00f3")
        .replace("Ã§", "\u00e7")
        .replace("Ã£", "\u00e3")
        .replace("Ã¡", "\u00e1")
        .replace("Ã©", "\u00e9")
        .replace("Ãª", "\u00ea")
    )


def report_paths(settings: dict[str, Any], *, apply: bool, checkpoint_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    mode = "apply" if apply else "apply_dry_run"
    base = reports_dir / f"{stamp}_select_cstring_preterite_transfer_{mode}_checkpoint_{checkpoint_run_id}"
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
        or "token" in as_text(issue.get("code")).lower()
    ]
    return bool(high_or_blocking), issues


def validate_checkpoint_run(conn, checkpoint_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_select_cstring_preterite_ptbr_checkpoint_runs
        WHERE id = ?
        LIMIT 1
        """,
        (checkpoint_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Checkpoint run {checkpoint_run_id} not found.")
    checkpoint = dict(row)
    expected = {
        "rule_version": "issue_select_cstring_preterite_ptbr_checkpoint_v1",
        "target_agent": EXPECTED_TARGET_AGENT,
        "source_shadow_run_id": 4,
        "candidate_count": 12,
        "checkpoint_allowed_count": 6,
        "blocked_count": 6,
        "production_release_allowed": 0,
    }
    for key, value in expected.items():
        if checkpoint.get(key) != value:
            raise RuntimeError(f"Checkpoint {key} mismatch: expected {value!r}, got {checkpoint.get(key)!r}")
    return checkpoint


def validate_partial_run(conn) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_select_cstring_partial_composition_checkpoint_runs
        WHERE id = ?
        LIMIT 1
        """,
        (EXPECTED_PARTIAL_RUN_ID,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Partial composition run {EXPECTED_PARTIAL_RUN_ID} not found.")
    partial = dict(row)
    expected = {
        "rule_version": "issue_select_cstring_partial_composition_checkpoint_v1",
        "composition_name": "select_cstring_partial_composition_checkpoint_v1",
        "composition_status": "shadow_learning_only",
        "composition_action": "measure_partial_select_cstring_microagent_coverage",
        "production_release_allowed": 0,
    }
    for key, value in expected.items():
        if partial.get(key) != value:
            raise RuntimeError(f"Partial run {key} mismatch: expected {value!r}, got {partial.get(key)!r}")
    return partial


def fetch_items(conn, checkpoint_run_id: int, state_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.id AS checkpoint_item_id,
            item.run_id,
            item.source_shadow_run_id,
            item.source_shadow_item_id,
            item.segment_id,
            item.target_agent,
            item.left_literal,
            item.right_literal,
            item.proposed_left_literal,
            item.proposed_right_literal,
            item.checkpoint_allowed,
            item.block_reason,
            item.checkpoint_action,
            item.production_release_allowed,
            partial.id AS partial_item_id,
            partial.run_id AS partial_run_id,
            partial.relative_path,
            partial.source_key,
            partial.composition_status,
            partial.segment_closure_candidate,
            partial.ready_sources_json,
            partial.blocked_piece_count,
            partial.uncovered_piece_count,
            partial.current_text,
            partial.production_release_allowed AS partial_production_release_allowed,
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
        FROM ml_issue_select_cstring_preterite_ptbr_checkpoint_items item
        JOIN ml_issue_select_cstring_partial_composition_checkpoint_items partial
          ON partial.segment_id = item.segment_id
         AND partial.run_id = ?
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
          AND item.id = ?
        ORDER BY item.segment_id
        """,
        (EXPECTED_PARTIAL_RUN_ID, state_run_id, checkpoint_run_id, EXPECTED_CHECKPOINT_ITEM_ID),
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

    if int(row.get("checkpoint_item_id") or 0) != EXPECTED_CHECKPOINT_ITEM_ID:
        return mark("blocked", "unexpected_checkpoint_item_id")
    if int(row.get("run_id") or 0) != checkpoint_run_id:
        return mark("blocked", "wrong_checkpoint_run")
    if int(row.get("segment_id") or 0) != EXPECTED_SEGMENT_ID:
        return mark("blocked", "unexpected_segment_id")
    if as_text(row.get("target_agent")) != EXPECTED_TARGET_AGENT:
        return mark("blocked", "unexpected_target_agent")
    if int(row.get("checkpoint_allowed") or 0) != 1:
        return mark("blocked", "checkpoint_not_allowed")
    if as_text(row.get("block_reason")).strip():
        return mark("blocked", f"checkpoint_block_reason:{row['block_reason']}")
    if int(row.get("production_release_allowed") or 0) != 0:
        return mark("blocked", "production_release_allowed_not_zero")
    if normalize_known_mojibake(as_text(row.get("left_literal"))) != EXPECTED_LEFT_LITERAL:
        return mark("blocked", "left_literal_mismatch")
    if normalize_known_mojibake(as_text(row.get("right_literal"))) != EXPECTED_RIGHT_LITERAL:
        return mark("blocked", "right_literal_mismatch")
    if normalize_known_mojibake(as_text(row.get("proposed_left_literal"))) != EXPECTED_PROPOSED_LITERAL:
        return mark("blocked", "proposed_left_literal_mismatch")
    if normalize_known_mojibake(as_text(row.get("proposed_right_literal"))) != EXPECTED_PROPOSED_LITERAL:
        return mark("blocked", "proposed_right_literal_mismatch")
    if int(row.get("partial_run_id") or 0) != EXPECTED_PARTIAL_RUN_ID:
        return mark("blocked", "wrong_partial_run")
    if as_text(row.get("composition_status")) != "segment_composition_ready":
        return mark("blocked", "partial_composition_not_ready")
    if int(row.get("segment_closure_candidate") or 0) != 1:
        return mark("blocked", "not_segment_closure_candidate")
    if int(row.get("blocked_piece_count") or 0) != 0:
        return mark("blocked", "blocked_piece_count_not_zero")
    if int(row.get("uncovered_piece_count") or 0) != 0:
        return mark("blocked", "uncovered_piece_count_not_zero")
    if int(row.get("partial_production_release_allowed") or 0) != 0:
        return mark("blocked", "partial_production_release_allowed_not_zero")
    ready_sources = json.loads(as_text(row.get("ready_sources_json")) or "{}")
    if int(ready_sources.get(EXPECTED_READY_SOURCE) or 0) <= 0:
        return mark("blocked", "partial_ready_source_missing")
    if as_text(row.get("relative_path")) != EXPECTED_RELATIVE_PATH:
        return mark("blocked", "unexpected_relative_path")
    if as_text(row.get("source_key")) != EXPECTED_SOURCE_KEY:
        return mark("blocked", "unexpected_source_key")
    if int(row.get("is_active") or 0) != 1:
        return mark("stale", "source_not_active")
    if int(row.get("confirmation_locked") or 0) == 1:
        return mark("blocked", "locked_confirmation")
    if int(row.get("live_needs_output_apply") or 0) != 0:
        return mark("blocked", "needs_output_apply_not_0")
    if canonical(as_text(row.get("live_output_text"))) != canonical(as_text(row.get("current_text"))):
        return mark("stale", "live_output_not_partial_current_text")
    if canonical(as_text(row.get("confirmed_text"))) != canonical(as_text(row.get("current_text"))):
        return mark("stale", "confirmation_not_on_partial_current_text")
    high_block, validation_issues = validation_has_high_block(APPROVED_TEXT)
    result["approved_validation_issues_json"] = json.dumps(validation_issues, ensure_ascii=False)
    if high_block:
        return mark("blocked", "approved_text_validation_high_or_blocking")
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
        "partial_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "output_line_number",
        "status",
        "block_reason",
        "block_reason_detail",
        "current_text",
        "live_output_text",
        "approved_text",
        "ready_sources_json",
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
        "Select_CString preterite transfer protected apply" if apply else "Select_CString preterite transfer protected apply dry-run",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Partial composition run id: {EXPECTED_PARTIAL_RUN_ID}",
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
        validate_checkpoint_run(conn, checkpoint_run_id)
        validate_partial_run(conn)
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
        print(f"[select_cstring_preterite_transfer_apply] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print("[select_cstring_preterite_transfer_apply] Completed")
    print(f"[select_cstring_preterite_transfer_apply] Apply: {args.apply}")
    print("[select_cstring_preterite_transfer_apply] Candidates: 1")
    print(f"[select_cstring_preterite_transfer_apply] Ready: {counts['ready']}")
    print(f"[select_cstring_preterite_transfer_apply] Stale: {counts['stale']}")
    print(f"[select_cstring_preterite_transfer_apply] Blocked: {counts['blocked']}")
    print(f"[select_cstring_preterite_transfer_apply] Already applied: {counts['already_applied']}")
    print(f"[select_cstring_preterite_transfer_apply] Applied: {apply_counts.get('applied', 0)}")
    print(f"[select_cstring_preterite_transfer_apply] Confirmation promoted: {apply_counts.get('confirmation_promoted', 0)}")
    print(f"[select_cstring_preterite_transfer_apply] Output written: {apply_counts.get('output_written', 0)}")
    print(f"[select_cstring_preterite_transfer_apply] Files touched: {apply_counts.get('files_touched', 0)}")
    print(f"[select_cstring_preterite_transfer_apply] Report: {paths[0]}")
    print(f"[select_cstring_preterite_transfer_apply] CSV: {paths[1]}")
    print(f"[select_cstring_preterite_transfer_apply] JSONL: {paths[2]}")


if __name__ == "__main__":
    main()
