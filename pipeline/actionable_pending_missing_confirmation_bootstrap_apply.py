from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from actionable_pending_missing_confirmation_bootstrap_dry_run import (
    CONFIRMATION_LABEL,
    CONFIRMATION_SOURCE,
    RULE_VERSION as DRY_RUN_RULE_VERSION,
    TARGET_SEGMENT_ID,
    blocking_validation_issues,
    canonical_article,
    sha256_text,
    short,
)
from apply_safe_output_updates import replace_quoted_text


RULE_VERSION = "actionable_pending_missing_confirmation_bootstrap_apply_v1"
REVIEWER = "production_actionable_pending_missing_confirmation_bootstrap_apply"


def latest_bootstrap_jsonl(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    matches = sorted(reports_dir.glob("*_actionable_pending_missing_confirmation_bootstrap_dry_run.jsonl"))
    if not matches:
        raise RuntimeError("No missing-confirmation bootstrap dry-run JSONL found.")
    return matches[-1]


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


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
          decision.valid,
          decision.reviewer,
          decision.normalized_decision,
          decision.evidence_label,
          state.final_state,
          state.review_state,
          state.apply_state,
          confirmation.id AS confirmation_id,
          confirmation.locked,
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
          AND decision.queue_run_id = 40
          AND decision.valid = 1
          AND decision.reviewer = 'codex_learning_front'
          AND decision.normalized_decision = 'needs_repair'
          AND decision.evidence_label = 'repair_required'
        ORDER BY decision.id
        LIMIT 1
        """,
        (state_run_id, segment_id),
    ).fetchone()
    return dict(row) if row else None


def report_path(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    return reports_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_actionable_pending_missing_confirmation_bootstrap_apply.txt"


def make_backup(output_root: Path, backup_root: Path, relative_path: str) -> None:
    source_path = output_root / Path(relative_path)
    backup_path = backup_root / Path(relative_path)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, backup_path)


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
    if live_row.get("final_state") != "pending_unknown_open":
        reasons.append("not_pending_unknown_open")
    if live_row.get("review_state") != "unreviewed":
        reasons.append("not_unreviewed")
    if live_row.get("apply_state") != "needs_review":
        reasons.append("not_needs_review")
    if live_row.get("confirmation_id") is not None:
        reasons.append("confirmation_already_exists")
    if int(live_row.get("locked") or 0) == 1:
        reasons.append("locked_confirmation_exists")
    if sha256_text(live_row.get("output_text")) != dry_row.get("output_text_hash"):
        reasons.append("stale_output_hash")
    if sha256_text(corrected_text) != dry_row.get("corrected_text_hash"):
        reasons.append("stale_corrected_hash")
    if canonical_article(live_row.get("output_text")) != dry_row.get("canonical_output_article"):
        reasons.append("stale_output_article")
    if canonical_article(corrected_text) != dry_row.get("canonical_corrected_article"):
        reasons.append("stale_corrected_article")
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

    if reasons:
        stale = any(reason.startswith("stale_") for reason in reasons)
        return ("stale" if stale else "blocked"), reasons, new_line
    return "ready", [], new_line


def apply_ready(
    conn,
    *,
    output_root: Path,
    backup_root: Path,
    dry_row: dict[str, Any],
    live_row: dict[str, Any],
    new_line: str,
    now: str,
    apply: bool,
) -> tuple[int, int, int]:
    if not apply:
        return 0, 0, 0
    output_path = output_root / Path(str(live_row["relative_path"]))
    make_backup(output_root, backup_root, str(live_row["relative_path"]))
    lines = output_path.read_text(encoding="utf-8-sig").splitlines()
    line_index = int(live_row["output_line_number"]) - 1
    corrected_text = live_row["corrected_text"] or ""
    lines[line_index] = new_line
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
        (int(live_row["segment_id"]), corrected_text, CONFIRMATION_SOURCE, CONFIRMATION_LABEL, REVIEWER, now, now),
    )
    conn.execute(
        """
        UPDATE output_segments
        SET portuguese_text = ?,
            output_raw_line = ?,
            portuguese_hash = ?,
            last_indexed_at = ?
        WHERE segment_id = ?
        """,
        (corrected_text, new_line, sha256_text(corrected_text), now, int(live_row["segment_id"])),
    )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return 1, 1, 1


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
        "Actionable pending missing-confirmation bootstrap apply report",
        f"Started at: {datetime.now().isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Dry-run rule version: {DRY_RUN_RULE_VERSION}",
        f"Dry-run JSONL: {dry_run_jsonl}",
        f"Segment-state run id: {state_run_id}",
        f"Apply: {apply}",
        f"Backup root: {backup_root if apply else 'not created'}",
        "",
        "Summary:",
        f"- candidates: {counters['candidates']}",
        f"- ready: {counters['ready']}",
        f"- applied: {counters['applied']}",
        f"- stale: {counters['stale']}",
        f"- blocked: {counters['blocked']}",
        f"- confirmation_created: {counters['confirmation_created']}",
        f"- output_written: {counters['output_written']}",
        f"- files_touched: {counters['files_touched']}",
        "",
        "Preview:",
    ]
    for dry_row, live_row, status, reasons in previews:
        lines.extend(
            [
                f"- segment {dry_row.get('segment_id')} | {dry_row.get('relative_path')}:{dry_row.get('source_line_number')} | {dry_row.get('source_key')} | {status}",
                f"  reasons={', '.join(reasons) if reasons else 'none'}",
                f"  expected_confirmation={CONFIRMATION_SOURCE} / {CONFIRMATION_LABEL}",
                f"  output={short(live_row.get('output_text') if live_row else dry_row.get('output_text'))}",
                f"  corrected={short(live_row.get('corrected_text') if live_row else dry_row.get('corrected_text'))}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This bootstrap creates an initial confirmation only for a ready dry-run row.",
            "- source/ is never written by this script.",
        ]
    )
    path = report_path(settings)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main(*, dry_run_jsonl: Path | None = None, state_run_id: int = 148, apply: bool = False) -> dict[str, Any]:
    settings = db.load_settings()
    output_root = db.project_path(settings["output_spanish"])
    selected_jsonl = dry_run_jsonl or latest_bootstrap_jsonl(settings)
    rows = load_rows(selected_jsonl)
    now = datetime.now().isoformat(timespec="seconds")
    backup_root = db.project_path("memory/backups") / f"actionable_pending_missing_confirmation_bootstrap_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    counters: Counter = Counter(candidates=len(rows))
    previews: list[tuple[dict[str, Any], dict[str, Any] | None, str, list[str]]] = []
    selected_ready: tuple[dict[str, Any], dict[str, Any], str] | None = None

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        for dry_row in rows:
            live_row = fetch_live_row(conn, segment_id=int(dry_row.get("segment_id") or TARGET_SEGMENT_ID), state_run_id=state_run_id)
            status, reasons, new_line = evaluate(dry_row=dry_row, live_row=live_row, output_root=output_root)
            counters[status] += 1
            previews.append((dry_row, live_row, status, reasons))
            if status == "ready" and live_row is not None and new_line is not None:
                selected_ready = (dry_row, live_row, new_line)

        if selected_ready is not None:
            created, written, touched = apply_ready(
                conn,
                output_root=output_root,
                backup_root=backup_root,
                dry_row=selected_ready[0],
                live_row=selected_ready[1],
                new_line=selected_ready[2],
                now=now,
                apply=apply,
            )
            counters["confirmation_created"] = created
            counters["output_written"] = written
            counters["files_touched"] = touched
            counters["applied"] = written
        if apply:
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
    print("[actionable_pending_missing_confirmation_bootstrap_apply] Completed")
    print(f"[actionable_pending_missing_confirmation_bootstrap_apply] Apply: {apply}")
    print(f"[actionable_pending_missing_confirmation_bootstrap_apply] Dry-run JSONL: {selected_jsonl}")
    print(f"[actionable_pending_missing_confirmation_bootstrap_apply] Candidates: {counters['candidates']}")
    print(f"[actionable_pending_missing_confirmation_bootstrap_apply] Ready: {counters['ready']}")
    print(f"[actionable_pending_missing_confirmation_bootstrap_apply] Applied: {counters['applied']}")
    print(f"[actionable_pending_missing_confirmation_bootstrap_apply] Stale: {counters['stale']}")
    print(f"[actionable_pending_missing_confirmation_bootstrap_apply] Blocked: {counters['blocked']}")
    print(f"[actionable_pending_missing_confirmation_bootstrap_apply] Report: {path}")
    return {
        "apply": apply,
        "ready": counters["ready"],
        "applied": counters["applied"],
        "stale": counters["stale"],
        "blocked": counters["blocked"],
        "report_path": str(path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apply ready missing-confirmation bootstrap repair.")
    parser.add_argument("--dry-run-jsonl", type=Path, default=None)
    parser.add_argument("--state-run-id", type=int, default=148)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    main(dry_run_jsonl=args.dry_run_jsonl, state_run_id=args.state_run_id, apply=args.apply)
