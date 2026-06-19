from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import learning_status
import local_quality_validator
from apply_safe_output_updates import protected_tokens, replace_quoted_text
from title_landed_es_reviewed_repair_dry_run import (
    AGENT_KEY,
    QUEUE_RUN_ID,
    READY as DRY_RUN_READY,
    RULE_VERSION as DRY_RUN_RULE_VERSION,
    canonical,
    stable_hash,
)


RULE_VERSION = "title_landed_es_reviewed_repair_apply_v1"
CONFIRMATION_SOURCE = "title_landed_es_reviewed_repair_production"
CONFIRMATION_LABEL = "landed_title_spanish_es_suffix_repair:queue_150"
CONFIRMATION_LEVEL = "auto_confirmed"
REVIEWER = "production_title_landed_es_reviewed_repair_apply"
FINAL_STATE = "closed_auto_confirmed_title_landed_es_reviewed_repair"
EXPECTED_CURRENT_STATE = "reopen_auto_confirmed_autofix"
STRUCTURAL_BLOCKERS = {
    "missing_output_file",
    "missing_output_line",
    "line_out_of_range",
    "line_without_quoted_value",
    "missing_source_segment",
    "source_not_active",
    "protected_token_signature_mismatch",
    "quality_issue",
}


def sha256_text(value: str | None) -> str | None:
    if value is None:
        return None
    return stable_hash(canonical(value))


def raw_sha256_text(value: str | None) -> str | None:
    if value is None:
        return None
    return stable_hash(value)


def short(value: str | None, limit: int = 160) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def latest_state_run_id(conn) -> int | None:
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
    return int(row["id"]) if row else None


def latest_dry_run_jsonl(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    matches = sorted(reports_dir.glob("*_title_landed_es_reviewed_repair_dry_run_queue_150.jsonl"))
    if not matches:
        raise RuntimeError("No title landed -es repair dry-run JSONL found.")
    return matches[-1]


def load_dry_run_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def report_path(settings: dict[str, Any], *, reaudit: bool = False) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    suffix = "reaudit" if reaudit else "apply"
    return reports_dir / f"{stamp}_title_landed_es_reviewed_repair_{suffix}.txt"


def humanish_or_locked_conflict(conn, *, segment_id: int, corrected_text: str) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    rows = conn.execute(
        """
        SELECT confirmation_level, confirmation_source, confirmation_label, reviewer, confirmed_text, locked
        FROM segment_confirmations
        WHERE segment_id = ?
        """,
        (segment_id,),
    ).fetchall()
    for row in rows:
        confirmed = canonical(row["confirmed_text"])
        haystack = " ".join(
            str(row[key] or "").lower()
            for key in ("confirmation_level", "confirmation_source", "confirmation_label", "reviewer")
        )
        humanish = any(token in haystack for token in ("human", "manual", "gemini"))
        locked = int(row["locked"] or 0) == 1
        if (humanish or locked) and confirmed != corrected_text:
            reasons.append("human_or_locked_confirmation_conflict")
            break
    return bool(reasons), reasons


def quality_blockers(text: str | None) -> list[str]:
    validation = local_quality_validator.validate_text(text)
    issues = validation.get("issues") or []
    return sorted(
        {
            str(issue.get("code") or "quality_issue")
            for issue in issues
            if isinstance(issue, dict)
            and str(issue.get("severity") or "").lower() in {"medium", "high", "error", "critical"}
        }
    )


def fetch_live_row(conn, *, dry_row: dict[str, Any], state_run_id: int | None) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
          decision.id AS decision_id,
          decision.queue_run_id,
          decision.queue_item_id,
          decision.ledger_run_id,
          decision.ledger_item_id,
          decision.segment_id,
          decision.relative_path AS decision_relative_path,
          decision.source_key AS decision_source_key,
          decision.source_line_number AS decision_source_line_number,
          decision.agent_key,
          decision.normalized_decision,
          decision.validation_status,
          decision.valid,
          decision.corrected_text,
          source.relative_path,
          source.source_key,
          source.source_line_number,
          source.is_active,
          output.output_line_number,
          output.portuguese_text AS output_text,
          output.output_raw_line,
          confirmation.id AS confirmation_id,
          confirmation.confirmed_text,
          confirmation.confirmation_level,
          confirmation.confirmation_source,
          confirmation.confirmation_label,
          confirmation.locked,
          confirmation.reviewer AS confirmation_reviewer,
          state.id AS state_item_id,
          state.final_state,
          state.state_group,
          state.review_state,
          state.apply_state,
          state.needs_reopen,
          state.needs_output_apply
        FROM ml_issue_review_decisions decision
        LEFT JOIN source_segments source
          ON source.id = decision.segment_id
        LEFT JOIN output_segments output
          ON output.segment_id = decision.segment_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.id = (
            SELECT sc.id
            FROM segment_confirmations sc
            WHERE sc.segment_id = decision.segment_id
            ORDER BY sc.updated_at DESC, sc.confirmed_at DESC, sc.id DESC
            LIMIT 1
          )
        LEFT JOIN segment_state_items state
          ON state.segment_id = decision.segment_id
         AND state.run_id = ?
        WHERE decision.id = ?
          AND decision.segment_id = ?
        LIMIT 1
        """,
        (state_run_id, int(dry_row["decision_id"]), int(dry_row["segment_id"])),
    ).fetchone()
    return dict(row) if row else None


def evaluate(
    conn,
    *,
    dry_row: dict[str, Any],
    live_row: dict[str, Any] | None,
    output_root: Path,
) -> tuple[str, list[str], str | None]:
    reasons: list[str] = []
    new_line: str | None = None

    if dry_row.get("status") != DRY_RUN_READY:
        return "skipped", ["dry_run_row_not_ready"], None
    if live_row is None:
        return "blocked", ["missing_live_row"], None

    segment_id = int(dry_row["segment_id"])
    corrected = canonical(live_row.get("corrected_text"))
    output = canonical(live_row.get("output_text"))
    confirmed = canonical(live_row.get("confirmed_text"))
    dry_output_hash = dry_row.get("output_hash")
    dry_confirmed_hash = dry_row.get("confirmed_hash")
    dry_corrected_hash = dry_row.get("corrected_hash")

    if int(live_row.get("queue_run_id") or 0) != QUEUE_RUN_ID:
        reasons.append("stale_queue_run_id")
    if live_row.get("agent_key") != AGENT_KEY:
        reasons.append("stale_agent_key")
    if int(live_row.get("valid") or 0) != 1:
        reasons.append("stale_decision_invalid")
    if live_row.get("validation_status") != "accepted":
        reasons.append("stale_decision_not_accepted")
    if live_row.get("normalized_decision") != "needs_repair":
        reasons.append("stale_decision_not_needs_repair")
    if sha256_text(corrected) != dry_corrected_hash:
        reasons.append("stale_corrected_hash")
    if live_row.get("relative_path") is None:
        reasons.append("missing_source_segment")
    if int(live_row.get("is_active") or 0) != 1:
        reasons.append("source_not_active")
    if live_row.get("relative_path") != dry_row.get("relative_path"):
        reasons.append("stale_relative_path")
    if live_row.get("source_key") != dry_row.get("source_key"):
        reasons.append("stale_source_key")
    if int(live_row.get("source_line_number") or 0) != int(dry_row.get("source_line_number") or 0):
        reasons.append("stale_source_line_number")
    if output != corrected and sha256_text(output) != dry_output_hash:
        reasons.append("stale_output_hash")
    if confirmed != corrected and sha256_text(confirmed) != dry_confirmed_hash:
        reasons.append("stale_confirmation_hash")
    if live_row.get("final_state") not in {EXPECTED_CURRENT_STATE, FINAL_STATE}:
        reasons.append("stale_unexpected_segment_state")
    conflict, conflict_reasons = humanish_or_locked_conflict(conn, segment_id=segment_id, corrected_text=corrected)
    if conflict:
        reasons.extend(conflict_reasons)
    quality = quality_blockers(corrected)
    if quality:
        reasons.append("quality_issue:" + ",".join(quality))
    if protected_tokens(output) != protected_tokens(corrected):
        reasons.append("protected_token_signature_mismatch")

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
                new_line = replace_quoted_text(lines[line_index], corrected)
            except ValueError:
                reasons.append("line_without_quoted_value")

    if output == corrected and confirmed == corrected:
        return "already_applied", [], new_line
    if reasons:
        stale = any(reason.startswith("stale_") for reason in reasons)
        structural = any(reason in STRUCTURAL_BLOCKERS or reason.startswith("quality_issue:") for reason in reasons)
        return ("stale" if stale and not structural else "blocked"), reasons, new_line
    return "ready", [], new_line


def make_backup(output_root: Path, backup_root: Path, relative_path: str) -> None:
    source_path = output_root / Path(relative_path)
    backup_path = backup_root / Path(relative_path)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, backup_path)


def promote_confirmation(conn, *, live_row: dict[str, Any], corrected_text: str, now: str) -> bool:
    segment_id = int(live_row["segment_id"])
    existing = conn.execute(
        """
        SELECT
            id,
            confirmation_level,
            confirmed_text,
            confirmation_source,
            confirmation_label,
            locked,
            confidence_score,
            reviewer
        FROM segment_confirmations
        WHERE segment_id = ?
        LIMIT 1
        """,
        (segment_id,),
    ).fetchone()

    already_exact = (
        existing is not None
        and int(existing["locked"] or 0) == 0
        and canonical(existing["confirmed_text"]) == corrected_text
        and existing["confirmation_source"] == CONFIRMATION_SOURCE
        and existing["confirmation_label"] == CONFIRMATION_LABEL
        and existing["confirmation_level"] == CONFIRMATION_LEVEL
    )

    if existing is not None and int(existing["locked"] or 0) == 1:
        # Locked confirmations are blocked by evaluate() when conflicting. Keep this
        # path defensive so a future caller cannot accidentally overwrite one.
        return False

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
            confirmation_level = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmation_level
                ELSE excluded.confirmation_level
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
            locked = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.locked
                ELSE excluded.locked
            END,
            confidence_score = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confidence_score
                ELSE excluded.confidence_score
            END,
            reviewer = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.reviewer
                ELSE excluded.reviewer
            END,
            confirmed_at = COALESCE(segment_confirmations.confirmed_at, excluded.confirmed_at),
            updated_at = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.updated_at
                ELSE excluded.updated_at
            END
        """,
        (segment_id, CONFIRMATION_LEVEL, corrected_text, CONFIRMATION_SOURCE, CONFIRMATION_LABEL, REVIEWER, now, now),
    )
    return not already_exact


def apply_ready(
    conn,
    *,
    output_root: Path,
    backup_root: Path,
    ready_entries: list[tuple[dict[str, Any], str]],
    now: str,
    create_backup: bool,
) -> tuple[int, int, int]:
    by_file: dict[str, list[tuple[dict[str, Any], str]]] = defaultdict(list)
    for live_row, new_line in ready_entries:
        by_file[str(live_row["relative_path"])].append((live_row, new_line))

    confirmation_promoted = 0
    output_written = 0
    files_touched = 0
    for relative_path, entries in sorted(by_file.items()):
        output_path = output_root / Path(relative_path)
        if create_backup:
            make_backup(output_root, backup_root, relative_path)
        lines = output_path.read_text(encoding="utf-8-sig").splitlines()
        touched = False
        for live_row, new_line in sorted(entries, key=lambda item: int(item[0]["output_line_number"])):
            line_index = int(live_row["output_line_number"]) - 1
            corrected = canonical(live_row["corrected_text"])
            lines[line_index] = new_line
            if promote_confirmation(conn, live_row=live_row, corrected_text=corrected, now=now):
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
                (corrected, new_line, raw_sha256_text(corrected), now, int(live_row["segment_id"])),
            )
            output_written += 1
            touched = True
        if touched:
            output_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
            files_touched += 1
    return confirmation_promoted, output_written, files_touched


def write_report(
    settings: dict[str, Any],
    *,
    dry_run_jsonl: Path | None,
    state_run_id: int | None,
    apply: bool,
    reaudit: bool,
    counters: Counter,
    previews: list[tuple[dict[str, Any], dict[str, Any] | None, str, list[str]]],
    backup_root: Path | None,
) -> Path:
    lines = [
        "Landed title -es reviewed repair protected apply report",
        f"Started at: {datetime.now().isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Dry-run rule version: {DRY_RUN_RULE_VERSION}",
        f"Dry-run JSONL: {dry_run_jsonl or 'not used for reaudit'}",
        f"Segment-state run id: {state_run_id}",
        f"Apply: {apply}",
        f"Reaudit: {reaudit}",
        f"Backup root: {backup_root or 'not created'}",
        "",
        "Summary:",
        f"- candidates: {counters['candidates']}",
        f"- dry_run_ready: {counters['dry_run_ready']}",
        f"- ready: {counters['ready']}",
        f"- applied: {counters['applied']}",
        f"- already_applied: {counters['already_applied']}",
        f"- stale: {counters['stale']}",
        f"- blocked: {counters['blocked']}",
        f"- skipped: {counters['skipped']}",
        f"- confirmation_promoted: {counters['confirmation_promoted']}",
        f"- output_written: {counters['output_written']}",
        f"- files_touched: {counters['files_touched']}",
        "",
        "Preview:",
    ]
    for dry_row, live_row, status, reasons in previews[:120]:
        lines.extend(
            [
                f"- segment {dry_row.get('segment_id')} | {dry_row.get('relative_path')}:{dry_row.get('source_line_number')} | {dry_row.get('source_key')} | {status}",
                f"  reasons={', '.join(reasons) if reasons else 'none'}",
                f"  current={short((live_row or dry_row).get('output_text'))}",
                f"  corrected={short((live_row or dry_row).get('corrected_text'))}",
            ]
        )
    path = report_path(settings, reaudit=reaudit)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_reaudit(conn, *, state_run_id: int | None) -> tuple[Counter, list[tuple[dict[str, Any], dict[str, Any] | None, str, list[str]]]]:
    rows = conn.execute(
        """
        SELECT
          state.segment_id,
          state.relative_path,
          state.source_key,
          state.source_line_number,
          state.final_state,
          confirmation.confirmed_text,
          output.portuguese_text AS output_text
        FROM segment_state_items state
        JOIN segment_confirmations confirmation
          ON confirmation.segment_id = state.segment_id
         AND confirmation.confirmation_source = ?
         AND confirmation.confirmation_label = ?
        LEFT JOIN output_segments output
          ON output.segment_id = state.segment_id
        WHERE state.run_id = ?
        ORDER BY state.relative_path, state.source_line_number, state.source_key
        """,
        (CONFIRMATION_SOURCE, CONFIRMATION_LABEL, state_run_id),
    ).fetchall()
    counters: Counter = Counter()
    previews: list[tuple[dict[str, Any], dict[str, Any] | None, str, list[str]]] = []
    counters["candidates"] = len(rows)
    for row in rows:
        row_dict = dict(row)
        status = "closed" if row_dict["final_state"] == FINAL_STATE or canonical(row_dict["confirmed_text"]) == canonical(row_dict["output_text"]) else "not_closed"
        counters[status] += 1
        previews.append((row_dict, row_dict, status, []))
    return counters, previews


def main(
    *,
    dry_run_jsonl: Path | None = None,
    state_run_id: int | None = None,
    apply: bool = False,
    reaudit: bool = False,
    create_backup: bool = False,
) -> dict[str, Any]:
    settings = db.load_settings()
    output_root = db.project_path(settings["output_spanish"])
    now = datetime.now().isoformat(timespec="seconds")
    backup_root = db.project_path("memory/backups") / f"title_landed_es_reviewed_repair_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    learning_safe = bool(learning_status.read_status().get("production_safe"))

    print("[title_landed_es_reviewed_repair_apply] Starting protected apply")
    print(f"[title_landed_es_reviewed_repair_apply] Rule version: {RULE_VERSION}")
    print(f"[title_landed_es_reviewed_repair_apply] Apply: {apply}")
    print(f"[title_landed_es_reviewed_repair_apply] Reaudit: {reaudit}")
    print(f"[title_landed_es_reviewed_repair_apply] Learning production safe: {learning_safe}")

    counters: Counter = Counter()
    previews: list[tuple[dict[str, Any], dict[str, Any] | None, str, list[str]]] = []
    selected_jsonl: Path | None = None

    with db.connect(settings) as conn:
        selected_state_run_id = state_run_id if state_run_id is not None else latest_state_run_id(conn)
        if reaudit:
            counters, previews = run_reaudit(conn, state_run_id=selected_state_run_id)
            report = write_report(
                settings,
                dry_run_jsonl=None,
                state_run_id=selected_state_run_id,
                apply=False,
                reaudit=True,
                counters=counters,
                previews=previews,
                backup_root=None,
            )
        else:
            selected_jsonl = dry_run_jsonl or latest_dry_run_jsonl(settings)
            dry_rows = load_dry_run_rows(selected_jsonl)
            counters["candidates"] = len(dry_rows)
            ready_entries: list[tuple[dict[str, Any], str]] = []

            for dry_row in dry_rows:
                if dry_row.get("status") == DRY_RUN_READY:
                    counters["dry_run_ready"] += 1
                live_row = fetch_live_row(conn, dry_row=dry_row, state_run_id=selected_state_run_id)
                status, reasons, new_line = evaluate(conn, dry_row=dry_row, live_row=live_row, output_root=output_root)
                counters[status] += 1
                previews.append((dry_row, live_row, status, reasons))
                if status == "ready" and live_row is not None and new_line is not None:
                    ready_entries.append((live_row, new_line))

            counters["ready"] = len(ready_entries)
            if apply:
                if not learning_safe:
                    raise RuntimeError("Learning gate is not production_safe; refusing --apply.")
                promoted, written, files_touched = apply_ready(
                    conn,
                    output_root=output_root,
                    backup_root=backup_root,
                    ready_entries=ready_entries,
                    now=now,
                    create_backup=create_backup,
                )
                counters["confirmation_promoted"] = promoted
                counters["output_written"] = written
                counters["applied"] = written
                counters["files_touched"] = files_touched
                conn.commit()
            else:
                conn.rollback()

            report = write_report(
                settings,
                dry_run_jsonl=selected_jsonl,
                state_run_id=selected_state_run_id,
                apply=apply,
                reaudit=False,
                counters=counters,
                previews=previews,
                backup_root=backup_root if apply and create_backup and ready_entries else None,
            )

    print(f"[title_landed_es_reviewed_repair_apply] Dry-run JSONL: {selected_jsonl or 'not used'}")
    print(f"[title_landed_es_reviewed_repair_apply] Segment-state run id: {selected_state_run_id}")
    print(f"[title_landed_es_reviewed_repair_apply] Candidates: {counters['candidates']}")
    print(f"[title_landed_es_reviewed_repair_apply] Dry-run ready: {counters['dry_run_ready']}")
    print(f"[title_landed_es_reviewed_repair_apply] Ready: {counters['ready']}")
    print(f"[title_landed_es_reviewed_repair_apply] Applied: {counters['applied']}")
    print(f"[title_landed_es_reviewed_repair_apply] Already applied: {counters['already_applied']}")
    print(f"[title_landed_es_reviewed_repair_apply] Stale: {counters['stale']}")
    print(f"[title_landed_es_reviewed_repair_apply] Blocked: {counters['blocked']}")
    print(f"[title_landed_es_reviewed_repair_apply] Skipped: {counters['skipped']}")
    print(f"[title_landed_es_reviewed_repair_apply] Confirmation promoted: {counters['confirmation_promoted']}")
    print(f"[title_landed_es_reviewed_repair_apply] Output written: {counters['output_written']}")
    print(f"[title_landed_es_reviewed_repair_apply] Files touched: {counters['files_touched']}")
    print(f"[title_landed_es_reviewed_repair_apply] Report: {report}")
    print("[title_landed_es_reviewed_repair_apply] Done")
    return {
        "apply": apply,
        "reaudit": reaudit,
        "dry_run_jsonl": str(selected_jsonl) if selected_jsonl else "",
        "state_run_id": selected_state_run_id,
        "counts": dict(counters),
        "report_path": str(report),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Protected apply for landed-title Spanish -es suffix repairs.")
    parser.add_argument("--dry-run-jsonl", type=Path, default=None)
    parser.add_argument("--state-run-id", type=int, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reaudit", action="store_true")
    parser.add_argument("--create-backup", action="store_true")
    args = parser.parse_args()
    try:
        main(
            dry_run_jsonl=args.dry_run_jsonl,
            state_run_id=args.state_run_id,
            apply=args.apply,
            reaudit=args.reaudit,
            create_backup=args.create_backup,
        )
    except Exception as exc:
        print(f"[title_landed_es_reviewed_repair_apply] ERROR: {exc}", file=sys.stderr)
        raise
