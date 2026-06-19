from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import replace_quoted_text
from apply_segment_state_updates import structural_tokens


RULE_VERSION = "short_label_single_issue_residual_repair_apply_v1"
DEFAULT_CHECKPOINT_RUN_ID = 2
EXPECTED_CANDIDATES = 39
EXPECTED_READY = 39
EXPECTED_STALE = 0
EXPECTED_BLOCKED = 0
EXPECTED_ALREADY_APPLIED = 0
CHECKPOINT_ACTION = "stage_short_label_single_issue_residual_repair_shadow"
CONFIRMATION_LEVEL = "auto_confirmed"
CONFIRMATION_SOURCE = "short_label_single_issue_residual_repair_production"
CONFIRMATION_LABEL = "short_label_single_issue_residual_repair:checkpoint_2:decision_run_176:queue_215"
REVIEWER = "production_short_label_single_issue_residual_repair_apply"


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def canonical(value: str | None) -> str:
    return " ".join((value or "").split())


def sha256_text(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def normalize_bold_markup(value: str | None) -> str:
    return re.sub(r"#bol\b", "#bold", value or "", flags=re.IGNORECASE)


def report_paths(settings: dict[str, Any], *, checkpoint_run_id: int, apply: bool) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    mode = "apply" if apply else "apply_dry_run"
    base = reports_dir / f"{stamp}_short_label_single_issue_residual_repair_{mode}_checkpoint_{checkpoint_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_state_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE total_segments > 1000
          AND finished_at IS NOT NULL
        ORDER BY finished_at DESC, id DESC
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


def file_text_at(output_root: Path, row: dict[str, Any]) -> str:
    output_path = output_root / Path(as_text(row["relative_path"]))
    lines = output_path.read_text(encoding="utf-8-sig").splitlines()
    line_index = int(row["output_line_number"]) - 1
    if line_index < 0 or line_index >= len(lines):
        raise RuntimeError(f"Output line out of range for segment {row['segment_id']}")
    raw_line = lines[line_index]
    first_quote = raw_line.find('"')
    last_quote = raw_line.rfind('"')
    if first_quote < 0 or last_quote <= first_quote:
        raise RuntimeError(f"Line without quoted value for segment {row['segment_id']}")
    return raw_line[first_quote + 1 : last_quote]


def validate_checkpoint(conn, checkpoint_run_id: int) -> dict[str, Any]:
    if checkpoint_run_id != DEFAULT_CHECKPOINT_RUN_ID:
        raise RuntimeError("Only checkpoint run 2 is permitted by this protected apply.")
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_single_issue_residual_repair_checkpoint_runs
        WHERE id = ?
        LIMIT 1
        """,
        (checkpoint_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Checkpoint run {checkpoint_run_id} not found.")
    checkpoint = dict(row)
    expected = {
        "rule_version": "issue_short_label_single_issue_residual_repair_checkpoint_v1",
        "policy_status": "shadow",
        "decision_run_id": 176,
        "candidate_count": 39,
        "allowed_count": 39,
        "blocked_count": 0,
    }
    for key, value in expected.items():
        if checkpoint.get(key) != value:
            raise RuntimeError(f"Checkpoint {key} mismatch: expected {value!r}, got {checkpoint.get(key)!r}")
    if checkpoint.get("finished_at") is None:
        raise RuntimeError("Checkpoint run is not finished.")
    return checkpoint


def fetch_items(conn, checkpoint_run_id: int, state_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.id AS checkpoint_item_id,
            item.run_id AS checkpoint_run_id,
            item.decision_run_id,
            item.decision_id,
            item.queue_item_id,
            item.ledger_item_id,
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.source_line_number,
            item.agent_key,
            item.subpolicy_name,
            item.queue_bucket,
            item.checkpoint_allowed,
            item.checkpoint_action,
            item.block_reason,
            item.token_status,
            item.current_text,
            item.corrected_text,
            item.notes,
            source.is_active,
            output.output_line_number,
            output.portuguese_text AS live_output_text,
            confirmation.confirmation_level,
            confirmation.confirmation_source,
            confirmation.confirmation_label,
            confirmation.confirmed_text,
            COALESCE(confirmation.locked, 0) AS confirmation_locked,
            state.final_state AS live_final_state,
            COALESCE(state.needs_output_apply, 0) AS live_needs_output_apply
        FROM ml_issue_short_label_single_issue_residual_repair_checkpoint_items item
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
          AND item.checkpoint_allowed = 1
          AND item.checkpoint_action = ?
        ORDER BY item.segment_id
        """,
        (state_run_id, checkpoint_run_id, CHECKPOINT_ACTION),
    ).fetchall()
    return [dict(row) for row in rows]


def classify_row(output_root: Path, row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["status"] = "ready"
    result["block_reason_detail"] = ""

    def mark(status: str, reason: str = "") -> dict[str, Any]:
        result["status"] = status
        result["block_reason_detail"] = reason
        return result

    current = as_text(row["current_text"])
    corrected = as_text(row["corrected_text"])
    live_output = as_text(row["live_output_text"])
    confirmed = as_text(row["confirmed_text"])

    if int(row.get("checkpoint_run_id") or 0) != DEFAULT_CHECKPOINT_RUN_ID:
        return mark("blocked", "checkpoint_run_not_permitted")
    if int(row.get("checkpoint_allowed") or 0) != 1:
        return mark("blocked", "checkpoint_not_allowed")
    if as_text(row.get("checkpoint_action")) != CHECKPOINT_ACTION:
        return mark("blocked", "checkpoint_action_mismatch")
    if as_text(row.get("block_reason")).strip():
        return mark("blocked", f"checkpoint_block_reason:{row['block_reason']}")
    if int(row.get("is_active") or 0) != 1:
        return mark("stale", "source_not_active")
    if int(row.get("confirmation_locked") or 0) == 1:
        return mark("blocked", "locked_confirmation")
    if int(row.get("live_needs_output_apply") or 0) != 0:
        return mark("blocked", "needs_output_apply_not_0")
    if not current or not corrected:
        return mark("blocked", "missing_current_or_corrected_text")
    if live_output == corrected and row["confirmation_source"] == CONFIRMATION_SOURCE and row["confirmation_label"] == CONFIRMATION_LABEL:
        return mark("already_applied", "")
    if live_output != current:
        return mark("stale", "live_output_not_current_text")
    if confirmed != current:
        return mark("stale", "confirmation_not_exact_current_text")
    if canonical(confirmed) != canonical(current):
        return mark("stale", "confirmation_not_on_current_text")
    if structural_tokens(normalize_bold_markup(current)) != structural_tokens(corrected):
        return mark("blocked", "protected_token_signature_changed")
    try:
        file_text = file_text_at(output_root, row)
    except RuntimeError as exc:
        return mark("blocked", str(exc))
    if file_text != current:
        return mark("stale", "file_output_not_current_text")
    if file_text != live_output:
        return mark("stale", "file_output_db_output_mismatch")
    return result


def validate_counts(rows: list[dict[str, Any]]) -> Counter:
    counts: Counter = Counter(row["status"] for row in rows)
    if len(rows) != EXPECTED_CANDIDATES:
        raise RuntimeError(f"Expected candidates={EXPECTED_CANDIDATES}, got {len(rows)}.")
    if counts["ready"] != EXPECTED_READY:
        raise RuntimeError(f"Expected ready={EXPECTED_READY}, got {counts['ready']}.")
    if counts["stale"] != EXPECTED_STALE:
        raise RuntimeError(f"Expected stale={EXPECTED_STALE}, got {counts['stale']}.")
    if counts["blocked"] != EXPECTED_BLOCKED:
        raise RuntimeError(f"Expected blocked={EXPECTED_BLOCKED}, got {counts['blocked']}.")
    if counts["already_applied"] != EXPECTED_ALREADY_APPLIED:
        raise RuntimeError(f"Expected already_applied={EXPECTED_ALREADY_APPLIED}, got {counts['already_applied']}.")
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


def apply_ready(conn, *, output_root: Path, rows: list[dict[str, Any]], now: str) -> tuple[int, int, int, int]:
    by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_file[as_text(row["relative_path"])].append(row)

    applied = 0
    confirmation_promoted = 0
    output_written = 0
    files_touched = 0
    for relative_path, file_rows in sorted(by_file.items()):
        output_path = output_root / Path(relative_path)
        lines = output_path.read_text(encoding="utf-8-sig").splitlines()
        for row in sorted(file_rows, key=lambda item: int(item["output_line_number"])):
            segment_id = int(row["segment_id"])
            line_index = int(row["output_line_number"]) - 1
            raw_line = lines[line_index]
            if file_text_at(output_root, row) != as_text(row["current_text"]):
                raise RuntimeError(f"Live file output drift for segment {segment_id}")
            corrected = as_text(row["corrected_text"])
            lines[line_index] = replace_quoted_text(raw_line, corrected)
            if upsert_confirmation(conn, segment_id=segment_id, confirmed_text=corrected, now=now):
                confirmation_promoted += 1
            conn.execute(
                """
                UPDATE output_segments
                SET portuguese_text = ?,
                    output_raw_line = ?,
                    portuguese_hash = ?,
                    last_indexed_at = ?
                WHERE segment_id = ?
                """,
                (corrected, lines[line_index], sha256_text(corrected), now, segment_id),
            )
            applied += 1
            output_written += 1
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        files_touched += 1
    return applied, confirmation_promoted, output_written, files_touched


def write_reports(
    settings: dict[str, Any],
    *,
    checkpoint_run_id: int,
    rows: list[dict[str, Any]],
    counts: Counter,
    apply: bool,
    apply_counts: dict[str, int] | None = None,
) -> tuple[Path, Path, Path]:
    txt_path, csv_path, jsonl_path = report_paths(settings, checkpoint_run_id=checkpoint_run_id, apply=apply)
    fields = [
        "checkpoint_item_id",
        "checkpoint_run_id",
        "decision_run_id",
        "decision_id",
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "output_line_number",
        "queue_bucket",
        "token_status",
        "status",
        "block_reason",
        "block_reason_detail",
        "current_text",
        "corrected_text",
        "notes",
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
        "Short label single-issue residual repair protected apply"
        if apply
        else "Short label single-issue residual repair protected apply dry-run",
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
        "Allowed repair segments:",
    ]
    for row in rows:
        lines.append(
            f"- {row['segment_id']} | {row['source_key']} | status={row['status']} | "
            f"path={row['relative_path']}"
        )
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
        rows = [classify_row(output_root, row) for row in fetch_items(conn, checkpoint_run_id, state_run_id)]
        counts = validate_counts(rows)
        if apply:
            now = db.utc_now()
            applied, confirmation_promoted, output_written, files_touched = apply_ready(
                conn,
                output_root=output_root,
                rows=[row for row in rows if row["status"] == "ready"],
                now=now,
            )
            apply_counts.update(
                {
                    "applied": applied,
                    "confirmation_promoted": confirmation_promoted,
                    "output_written": output_written,
                    "files_touched": files_touched,
                }
            )
            conn.commit()
        paths = write_reports(
            settings,
            checkpoint_run_id=checkpoint_run_id,
            rows=rows,
            counts=counts,
            apply=apply,
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
        print(f"[short_label_single_issue_residual_repair_apply] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print("[short_label_single_issue_residual_repair_apply] Completed")
    print(f"[short_label_single_issue_residual_repair_apply] Apply: {args.apply}")
    print(f"[short_label_single_issue_residual_repair_apply] Checkpoint run id: {args.checkpoint_run_id}")
    print(f"[short_label_single_issue_residual_repair_apply] Candidates: {EXPECTED_CANDIDATES}")
    print(f"[short_label_single_issue_residual_repair_apply] Ready: {counts['ready']}")
    print(f"[short_label_single_issue_residual_repair_apply] Stale: {counts['stale']}")
    print(f"[short_label_single_issue_residual_repair_apply] Blocked: {counts['blocked']}")
    print(f"[short_label_single_issue_residual_repair_apply] Already applied: {counts['already_applied']}")
    print(f"[short_label_single_issue_residual_repair_apply] Applied: {apply_counts.get('applied', 0)}")
    print(
        "[short_label_single_issue_residual_repair_apply] Confirmation promoted: "
        f"{apply_counts.get('confirmation_promoted', 0)}"
    )
    print(f"[short_label_single_issue_residual_repair_apply] Output written: {apply_counts.get('output_written', 0)}")
    print(f"[short_label_single_issue_residual_repair_apply] Files touched: {apply_counts.get('files_touched', 0)}")
    print(f"[short_label_single_issue_residual_repair_apply] Report: {paths[0]}")
    print(f"[short_label_single_issue_residual_repair_apply] CSV: {paths[1]}")
    print(f"[short_label_single_issue_residual_repair_apply] JSONL: {paths[2]}")


if __name__ == "__main__":
    main()
