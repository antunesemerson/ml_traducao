from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator
from apply_safe_output_updates import replace_quoted_text
from actionable_pending_reviewed_repair_dry_run import (
    QUEUE_39_SOURCE,
    QUEUE_40_LABEL,
    QUEUE_40_SOURCE,
    RULE_VERSION as DRY_RUN_RULE_VERSION,
    blocking_validation_issues,
    sha256_text,
    short,
)


RULE_VERSION = "actionable_pending_reviewed_repair_apply_v1"
REVIEWER = "production_actionable_pending_reviewed_repair_apply"
STRUCTURAL_BLOCKERS = {
    "missing_dry_run_row",
    "dry_run_not_ready",
    "missing_output_file",
    "missing_output_line",
    "line_out_of_range",
    "line_without_quoted_value",
    "stale_output_hash",
    "stale_confirmation_hash",
    "stale_corrected_hash",
    "validation_issue",
}


def latest_dry_run_jsonl(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    matches = sorted(reports_dir.glob("*_actionable_pending_reviewed_repair_dry_run.jsonl"))
    if not matches:
        raise RuntimeError("No actionable pending reviewed repair dry-run JSONL found.")
    return matches[-1]


def load_dry_run_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def report_path(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return reports_dir / f"{stamp}_actionable_pending_reviewed_repair_apply.txt"


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


def fetch_live_row(conn, *, segment_id: int, state_run_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
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
          decision.reviewer AS decision_reviewer,
          decision.valid,
          state.final_state,
          state.review_state,
          confirmation.id AS confirmation_id,
          confirmation.confirmed_text,
          confirmation.confirmation_level,
          confirmation.confirmation_source,
          confirmation.confirmation_label,
          confirmation.locked,
          confirmation.updated_at AS confirmation_updated_at,
          output.output_line_number,
          output.portuguese_text AS output_text,
          output.output_raw_line
        FROM ml_issue_review_decisions decision
        LEFT JOIN segment_state_items state
          ON state.segment_id = decision.segment_id
         AND state.run_id = ?
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.segment_id = decision.segment_id
        LEFT JOIN output_segments output
          ON output.segment_id = decision.segment_id
        WHERE decision.segment_id = ?
          AND decision.valid = 1
          AND decision.normalized_decision = 'needs_repair'
          AND decision.evidence_label = 'repair_required'
        ORDER BY decision.queue_run_id DESC, decision.id DESC
        LIMIT 1
        """,
        (state_run_id, segment_id),
    ).fetchone()
    return dict(row) if row else None


def make_backup(output_root: Path, backup_root: Path, relative_path: str) -> None:
    source_path = output_root / Path(relative_path)
    backup_path = backup_root / Path(relative_path)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, backup_path)


def expected_confirmation(dry_row: dict[str, Any]) -> tuple[str, str]:
    queue_run_id = int(dry_row.get("queue_run_id") or 0)
    if queue_run_id == 39:
        return QUEUE_39_SOURCE, str(dry_row.get("expected_confirmation_label") or "")
    return QUEUE_40_SOURCE, QUEUE_40_LABEL


def evaluate(
    *,
    dry_row: dict[str, Any],
    live_row: dict[str, Any] | None,
    output_root: Path,
) -> tuple[str, list[str], str | None]:
    reasons: list[str] = []
    new_line: str | None = None

    if dry_row.get("status") != "ready":
        reasons.append("dry_run_not_ready")
    if live_row is None:
        return "blocked", ["missing_live_row"], None

    corrected_text = str(live_row.get("corrected_text") or "")
    if int(live_row.get("segment_id") or 0) != int(dry_row.get("segment_id") or 0):
        reasons.append("segment_mismatch")
    if live_row.get("final_state") != dry_row.get("final_state"):
        reasons.append("stale_final_state")
    if sha256_text(live_row.get("output_text")) != dry_row.get("current_output_hash"):
        reasons.append("stale_output_hash")
    if sha256_text(live_row.get("confirmed_text")) != dry_row.get("current_confirmation_hash"):
        reasons.append("stale_confirmation_hash")
    if sha256_text(corrected_text) != dry_row.get("corrected_text_hash"):
        reasons.append("stale_corrected_hash")
    if int(live_row.get("locked") or 0) == 1 or humanish_confirmation(live_row):
        reasons.append("human_or_locked_confirmation")
    if blocking_validation_issues(corrected_text):
        reasons.append("validation_issue")

    output_path = output_root / Path(str(live_row.get("relative_path") or dry_row.get("relative_path") or ""))
    if live_row.get("output_line_number") is None:
        reasons.append("missing_output_line")
    if not output_path.exists():
        reasons.append("missing_output_file")
    elif live_row.get("output_line_number") is not None:
        lines = output_path.read_text(encoding="utf-8-sig").splitlines()
        line_index = int(live_row["output_line_number"]) - 1
        if line_index < 0 or line_index >= len(lines):
            reasons.append("line_out_of_range")
        else:
            try:
                new_line = replace_quoted_text(lines[line_index], corrected_text)
            except ValueError:
                reasons.append("line_without_quoted_value")

    if corrected_text == (live_row.get("output_text") or "") and corrected_text == (live_row.get("confirmed_text") or ""):
        return "already_applied", reasons, new_line
    if reasons:
        stale = any(reason.startswith("stale_") for reason in reasons)
        structural = any(reason in STRUCTURAL_BLOCKERS for reason in reasons)
        return ("stale" if stale and not structural else "blocked"), reasons, new_line
    return "ready", [], new_line


def promote_confirmation(conn, *, live_row: dict[str, Any], dry_row: dict[str, Any], now: str) -> None:
    confirmation_source, confirmation_label = expected_confirmation(dry_row)
    segment_id = int(live_row["segment_id"])
    corrected_text = live_row["corrected_text"] or ""
    cursor = conn.execute(
        """
        UPDATE segment_confirmations
        SET
            confirmation_level = 'auto_confirmed',
            confirmed_text = ?,
            confirmation_source = ?,
            confirmation_label = ?,
            locked = 0,
            confidence_score = 1.0,
            reviewer = ?,
            confirmed_at = ?,
            updated_at = ?
        WHERE segment_id = ?
        """,
        (corrected_text, confirmation_source, confirmation_label, REVIEWER, now, now, segment_id),
    )
    if cursor.rowcount == 0:
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
            VALUES (?, 'auto_confirmed', ?, ?, ?, 0, 1.0, ?, ?, ?)
            """,
            (segment_id, corrected_text, confirmation_source, confirmation_label, REVIEWER, now, now),
        )


def apply_ready(
    conn,
    *,
    output_root: Path,
    backup_root: Path,
    ready_entries: list[tuple[dict[str, Any], dict[str, Any], str]],
    now: str,
    apply: bool,
) -> tuple[int, int, int]:
    if not apply:
        return 0, 0, 0
    by_file: dict[str, list[tuple[dict[str, Any], dict[str, Any], str]]] = defaultdict(list)
    for dry_row, live_row, new_line in ready_entries:
        by_file[str(live_row["relative_path"])].append((dry_row, live_row, new_line))

    confirmation_promoted = 0
    output_written = 0
    files_touched = 0
    for relative_path, entries in sorted(by_file.items()):
        output_path = output_root / Path(relative_path)
        make_backup(output_root, backup_root, relative_path)
        lines = output_path.read_text(encoding="utf-8-sig").splitlines()
        for dry_row, live_row, new_line in sorted(entries, key=lambda item: int(item[1]["output_line_number"])):
            segment_id = int(live_row["segment_id"])
            corrected_text = live_row["corrected_text"] or ""
            line_index = int(live_row["output_line_number"]) - 1
            lines[line_index] = new_line
            promote_confirmation(conn, live_row=live_row, dry_row=dry_row, now=now)
            conn.execute(
                """
                UPDATE output_segments
                SET portuguese_text = ?,
                    output_raw_line = ?,
                    portuguese_hash = ?,
                    last_indexed_at = ?
                WHERE segment_id = ?
                """,
                (corrected_text, new_line, sha256_text(corrected_text), now, segment_id),
            )
            confirmation_promoted += 1
            output_written += 1
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        files_touched += 1
    return confirmation_promoted, output_written, files_touched


def write_report(
    settings: dict[str, Any],
    *,
    dry_run_jsonl: Path,
    state_run_id: int,
    apply: bool,
    counters: Counter,
    previews: list[tuple[dict[str, Any], dict[str, Any] | None, str, list[str]]],
    backup_root: Path | None,
) -> Path:
    lines = [
        "Actionable pending reviewed repair apply report",
        f"Started at: {datetime.now().isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Dry-run rule version: {DRY_RUN_RULE_VERSION}",
        f"Dry-run JSONL: {dry_run_jsonl}",
        f"Segment-state run id: {state_run_id}",
        f"Apply: {apply}",
        f"Backup root: {backup_root if apply else 'not created'}",
        "",
        "Summary:",
        f"- candidates_from_dry_run: {counters['candidates']}",
        f"- dry_run_ready: {counters['dry_run_ready']}",
        f"- ready: {counters['ready']}",
        f"- applied: {counters['applied']}",
        f"- already_applied: {counters['already_applied']}",
        f"- stale: {counters['stale']}",
        f"- blocked: {counters['blocked']}",
        f"- intentionally_skipped_blocked: {counters['skipped_blocked']}",
        f"- confirmation_promoted: {counters['confirmation_promoted']}",
        f"- output_written: {counters['output_written']}",
        f"- files_touched: {counters['files_touched']}",
        "",
        "Preview:",
    ]
    for dry_row, live_row, status, reasons in previews:
        segment_id = dry_row.get("segment_id")
        relative_path = dry_row.get("relative_path")
        source_key = dry_row.get("source_key")
        source_line_number = dry_row.get("source_line_number")
        lines.extend(
            [
                f"- segment {segment_id} | {relative_path}:{source_line_number} | {source_key} | {status}",
                f"  queue={dry_row.get('queue_run_id')}; reasons={', '.join(reasons) if reasons else 'none'}",
                f"  confirmation={dry_row.get('expected_confirmation_source')} / {dry_row.get('expected_confirmation_label')}",
                f"  current={short(live_row.get('output_text') if live_row else dry_row.get('output_text'))}",
                f"  corrected={short(live_row.get('corrected_text') if live_row else dry_row.get('corrected_text'))}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Only dry-run rows with status=ready are eligible.",
            "- Blocked dry-run rows are intentionally skipped.",
            "- source/ is never written by this script.",
        ]
    )
    path = report_path(settings)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main(*, dry_run_jsonl: Path | None = None, state_run_id: int = 145, apply: bool = False) -> dict[str, Any]:
    settings = db.load_settings()
    output_root = db.project_path(settings["output_spanish"])
    selected_jsonl = dry_run_jsonl or latest_dry_run_jsonl(settings)
    dry_rows = load_dry_run_rows(selected_jsonl)
    now = datetime.now().isoformat(timespec="seconds")
    backup_root = db.project_path("memory/backups") / f"actionable_pending_reviewed_repair_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    counters: Counter = Counter()
    counters["candidates"] = len(dry_rows)
    previews: list[tuple[dict[str, Any], dict[str, Any] | None, str, list[str]]] = []
    ready_entries: list[tuple[dict[str, Any], dict[str, Any], str]] = []

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        for dry_row in dry_rows:
            if dry_row.get("status") != "ready":
                counters["skipped_blocked"] += 1
                previews.append((dry_row, None, "skipped_blocked", [str(dry_row.get("block_reasons") or "dry_run_not_ready")]))
                continue
            counters["dry_run_ready"] += 1
            live_row = fetch_live_row(conn, segment_id=int(dry_row["segment_id"]), state_run_id=state_run_id)
            status, reasons, new_line = evaluate(dry_row=dry_row, live_row=live_row, output_root=output_root)
            counters[status] += 1
            previews.append((dry_row, live_row, status, reasons))
            if status == "ready" and live_row is not None and new_line is not None:
                ready_entries.append((dry_row, live_row, new_line))

        promoted, written, touched = apply_ready(
            conn,
            output_root=output_root,
            backup_root=backup_root,
            ready_entries=ready_entries,
            now=now,
            apply=apply,
        )
        counters["confirmation_promoted"] = promoted
        counters["output_written"] = written
        counters["files_touched"] = touched
        if apply:
            counters["applied"] = written
            conn.commit()
        else:
            conn.rollback()

    path = write_report(
        settings,
        dry_run_jsonl=selected_jsonl,
        state_run_id=state_run_id,
        apply=apply,
        counters=counters,
        previews=previews,
        backup_root=backup_root if apply else None,
    )
    print("[actionable_pending_reviewed_repair_apply] Completed")
    print(f"[actionable_pending_reviewed_repair_apply] Apply: {apply}")
    print(f"[actionable_pending_reviewed_repair_apply] Dry-run JSONL: {selected_jsonl}")
    print(f"[actionable_pending_reviewed_repair_apply] Candidates: {counters['candidates']}")
    print(f"[actionable_pending_reviewed_repair_apply] Dry-run ready: {counters['dry_run_ready']}")
    print(f"[actionable_pending_reviewed_repair_apply] Ready: {counters['ready']}")
    print(f"[actionable_pending_reviewed_repair_apply] Applied: {counters['applied']}")
    print(f"[actionable_pending_reviewed_repair_apply] Skipped blocked: {counters['skipped_blocked']}")
    print(f"[actionable_pending_reviewed_repair_apply] Stale: {counters['stale']}")
    print(f"[actionable_pending_reviewed_repair_apply] Blocked: {counters['blocked']}")
    print(f"[actionable_pending_reviewed_repair_apply] Report: {path}")
    return {
        "apply": apply,
        "dry_run_jsonl": str(selected_jsonl),
        "ready": counters["ready"],
        "applied": counters["applied"],
        "skipped_blocked": counters["skipped_blocked"],
        "stale": counters["stale"],
        "blocked": counters["blocked"],
        "report_path": str(path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apply only ready reviewed actionable repair candidates.")
    parser.add_argument("--dry-run-jsonl", type=Path, default=None)
    parser.add_argument("--state-run-id", type=int, default=145)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    main(dry_run_jsonl=args.dry_run_jsonl, state_run_id=args.state_run_id, apply=args.apply)
