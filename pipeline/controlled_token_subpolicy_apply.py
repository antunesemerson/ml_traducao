from __future__ import annotations

import argparse
import hashlib
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
from apply_safe_output_updates import replace_quoted_text


RULE_VERSION = "controlled_token_subpolicy_apply_v1"
AUDIT_NAME = "boundary_token_subpolicy_controlled_production_readiness_v1"
AUDIT_STATUS = "ready_for_controlled_production_design"
CONFIRMATION_SOURCE = "controlled_token_subpolicy_production"
CONFIRMATION_LABEL = "boundary_token_subpolicy_controlled_production"
REVIEWER = "production_controlled_token_subpolicy_apply"
STRUCTURAL_BLOCKERS = {
    "validation_issue",
    "missing_output_file",
    "missing_output_line",
    "line_out_of_range",
    "line_without_quoted_value",
}


def sha256_text(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_yml_escaped_text_for_compare(value: str | None) -> str | None:
    if value is None:
        return None
    return value.replace('\\"', '"')


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


def latest_audit_run_id(conn) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit_runs
        WHERE audit_name = ?
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (AUDIT_NAME,),
    ).fetchone()
    return int(row["id"]) if row else None


def blocking_validation_issues(text: str | None) -> list[dict[str, Any]]:
    validation = local_quality_validator.validate_text(text)
    issues = validation.get("issues") or []
    blocked_codes = {
        "spanish_punctuation",
        "mojibake_or_unexpected_script",
        "utf8_mojibake_sequence",
        "replacement_question_mark_mojibake",
        "spanish_residue",
        "spanish_residue_in_literal",
        "gender_token_extra_suffix",
    }
    return [
        issue
        for issue in issues
        if issue.get("severity") == "high" or issue.get("code") in blocked_codes
    ]


def humanish_confirmation(row: dict[str, Any]) -> bool:
    haystack = " ".join(
        str(row.get(key) or "").lower()
        for key in ("confirmation_level", "confirmation_source", "confirmation_label", "reviewer")
    )
    return any(token in haystack for token in ("human", "manual", "reviewed", "gemini"))


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def fetch_rows(conn, *, audit_run_id: int, state_run_id: int | None) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            run.audit_status,
            item.id AS audit_item_id,
            item.created_at AS audit_item_created_at,
            item.run_id AS audit_run_id,
            item.lifecycle_item_id,
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.source_line_number,
            item.subpolicy_name,
            item.policy_bucket,
            item.boundary_policy,
            item.current_final_state AS audit_final_state,
            item.current_apply_state AS audit_apply_state,
            item.current_state_group AS audit_state_group,
            item.current_review_state AS audit_review_state,
            item.eligible_controlled_production,
            item.estimated_closed_gain,
            item.block_reason,
            item.current_confirmed_text_hash AS audit_confirmed_text_hash,
            item.corrected_text_hash AS audit_corrected_text_hash,
            item.output_text_hash AS audit_output_text_hash,
            life.policy_allowed AS lifecycle_policy_allowed,
            decision.corrected_text,
            confirmation.id AS confirmation_id,
            confirmation.confirmed_text,
            confirmation.confirmation_level,
            confirmation.confirmation_source,
            confirmation.confirmation_label,
            confirmation.locked,
            confirmation.reviewer,
            confirmation.updated_at AS confirmation_updated_at,
            output.output_line_number,
            output.portuguese_text AS current_output_text,
            output.output_raw_line,
            state.id AS state_item_id,
            state.final_state AS latest_final_state,
            state.apply_state AS latest_apply_state,
            state.state_group AS latest_state_group,
            state.review_state AS latest_review_state,
            state.lifecycle_policy_allowed AS latest_lifecycle_policy_allowed,
            state.needs_reopen,
            state.needs_output_apply,
            source.spanish_text
        FROM auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit_items item
        JOIN auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit_runs run
          ON run.id = item.run_id
        JOIN auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_items life
          ON life.id = item.lifecycle_item_id
        JOIN auto_confirmation_reopen_text_review_decisions decision
          ON decision.id = life.review_decision_id
        JOIN source_segments source
          ON source.id = item.segment_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.segment_id = item.segment_id
        LEFT JOIN output_segments output
          ON output.segment_id = item.segment_id
        LEFT JOIN segment_state_items state
          ON state.segment_id = item.segment_id
         AND state.run_id = ?
        WHERE item.run_id = ?
          AND item.eligible_controlled_production = 1
          AND item.estimated_closed_gain = 1
          AND (item.block_reason IS NULL OR TRIM(item.block_reason) = '')
        ORDER BY item.policy_bucket, item.relative_path, item.source_line_number, item.source_key
        """,
        (state_run_id, audit_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def evaluate_row(row: dict[str, Any], *, output_root: Path, learning_safe: bool) -> tuple[str, str | None, str | None, list[str]]:
    reasons: list[str] = []
    corrected_text = row.get("corrected_text")
    confirmed_text = row.get("confirmed_text")
    output_text = row.get("current_output_text")
    corrected_compare = normalize_yml_escaped_text_for_compare(corrected_text)
    confirmed_compare = normalize_yml_escaped_text_for_compare(confirmed_text)
    output_compare = normalize_yml_escaped_text_for_compare(output_text)

    if not learning_safe:
        reasons.append("learning_not_production_safe")
    if row.get("audit_status") != AUDIT_STATUS:
        reasons.append("audit_not_ready")
    if int(row.get("eligible_controlled_production") or 0) != 1:
        reasons.append("audit_item_not_eligible")
    if str(row.get("block_reason") or "").strip():
        reasons.append("audit_item_blocked")
    if int(row.get("estimated_closed_gain") or 0) != 1:
        reasons.append("no_estimated_closed_gain")
    if int(row.get("lifecycle_policy_allowed") or 0) != 1:
        reasons.append("lifecycle_item_not_allowed")
    if sha256_text(output_text) != row.get("audit_output_text_hash"):
        reasons.append("stale_output_hash")
    if sha256_text(confirmed_text) != row.get("audit_confirmed_text_hash"):
        reasons.append("stale_confirmed_hash")
    if confirmed_compare != output_compare:
        reasons.append("current_confirmation_output_mismatch")
    if sha256_text(corrected_text) != row.get("audit_corrected_text_hash"):
        reasons.append("corrected_text_hash_mismatch")
    if int(row.get("locked") or 0) == 1:
        reasons.append("human_locked")
    audit_created_at = parse_iso(row.get("audit_item_created_at"))
    confirmation_updated_at = parse_iso(row.get("confirmation_updated_at"))
    if humanish_confirmation(row) and audit_created_at and confirmation_updated_at and confirmation_updated_at > audit_created_at:
        reasons.append("human_confirmation_newer_than_audit")
    if row.get("state_item_id") is None:
        reasons.append("missing_latest_segment_state")
    if row.get("latest_final_state") != row.get("audit_final_state"):
        reasons.append("stale_segment_state")
    if row.get("latest_state_group") != "pending" or row.get("latest_final_state") != "reopen_auto_confirmed_autofix":
        reasons.append("not_current_reopen_auto_confirmed_autofix")
    validation_issues = blocking_validation_issues(corrected_text)
    if validation_issues:
        reasons.append("validation_issue")
    if row.get("output_line_number") is None:
        reasons.append("missing_output_line")

    output_path = output_root / Path(row["relative_path"])
    if not output_path.exists():
        reasons.append("missing_output_file")
        return "blocked", None, None, reasons

    current_line = None
    new_line = None
    if row.get("output_line_number") is not None:
        lines = output_path.read_text(encoding="utf-8-sig").splitlines()
        line_index = int(row["output_line_number"]) - 1
        if line_index < 0 or line_index >= len(lines):
            reasons.append("line_out_of_range")
        else:
            current_line = lines[line_index]
            try:
                new_line = replace_quoted_text(current_line, corrected_text or "")
            except ValueError:
                reasons.append("line_without_quoted_value")

    if not reasons and corrected_compare == confirmed_compare and corrected_compare == output_compare:
        return "already_applied", current_line, new_line, ["already_applied"]
    if reasons:
        stale = any(reason.startswith("stale_") or reason in {"not_current_reopen_auto_confirmed_autofix", "missing_latest_segment_state"} for reason in reasons)
        return ("stale" if stale and not any(reason in STRUCTURAL_BLOCKERS for reason in reasons) else "blocked"), current_line, new_line, reasons
    return "ready", current_line, new_line, []


def make_backup(output_root: Path, backup_root: Path, relative_path: str) -> None:
    source_path = output_root / Path(relative_path)
    backup_path = backup_root / Path(relative_path)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, backup_path)


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
    for row, new_line in ready_entries:
        by_file[row["relative_path"]].append((row, new_line))

    confirmation_promoted = 0
    output_written = 0
    files_touched = 0
    for relative_path, entries in sorted(by_file.items()):
        output_path = output_root / Path(relative_path)
        if create_backup:
            make_backup(output_root, backup_root, relative_path)
        lines = output_path.read_text(encoding="utf-8-sig").splitlines()
        for row, new_line in sorted(entries, key=lambda item: int(item[0]["output_line_number"])):
            segment_id = int(row["segment_id"])
            line_index = int(row["output_line_number"]) - 1
            corrected_text = row["corrected_text"] or ""
            lines[line_index] = new_line
            conn.execute(
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
                (corrected_text, CONFIRMATION_SOURCE, CONFIRMATION_LABEL, REVIEWER, now, now, segment_id),
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
    audit_run_id: int,
    apply: bool,
    reaudit: bool,
    counters: Counter,
    previews: list[tuple[dict[str, Any], str, list[str]]],
    backup_root: Path | None,
    actual_closed_gain: int = 0,
) -> Path:
    lines = [
        "Controlled token subpolicy apply report",
        f"Started at: {datetime.now().isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Audit run id: {audit_run_id}",
        f"Apply: {apply}",
        f"Reaudit: {reaudit}",
        f"Backup root: {backup_root or 'not created'}",
        "",
        "Summary:",
        f"- candidates: {counters['candidates']}",
        f"- eligible: {counters['eligible']}",
        f"- ready: {counters['ready']}",
        f"- applied: {counters['applied']}",
        f"- stale: {counters['stale']}",
        f"- blocked: {counters['blocked']}",
        f"- confirmation_promoted: {counters['confirmation_promoted']}",
        f"- output_written: {counters['output_written']}",
        f"- estimated_closed_gain: {counters['estimated_closed_gain']}",
        f"- actual_closed_gain_after_reaudit: {actual_closed_gain}",
        "",
        "Preview:",
    ]
    for row, status, reasons in previews[:80]:
        lines.extend(
            [
                f"- segment {row['segment_id']} | {row['relative_path']}:{row['source_line_number']} | {row['source_key']} | {status}",
                f"  SUBPOLICY: {row.get('subpolicy_name')} | {row.get('policy_bucket')}",
                f"  REASONS: {', '.join(reasons) if reasons else 'none'}",
                f"  CURRENT: {short(row.get('current_output_text'))}",
                f"  CORRECTED: {short(row.get('corrected_text'))}",
            ]
        )
    if not previews:
        lines.append("- No candidates selected.")
    return db.write_report(settings, "controlled_token_subpolicy_apply", lines)


def main(
    *,
    audit_run_id: int | None = None,
    apply: bool = False,
    reaudit: bool = False,
    create_backup: bool = True,
) -> None:
    settings = db.load_settings()
    output_root = db.project_path(settings["output_spanish"])
    learning_payload = learning_status.read_status()
    learning_safe = bool(learning_payload.get("production_safe"))
    now = datetime.now().isoformat(timespec="seconds")
    backup_root = db.project_path("memory/backups") / f"controlled_token_subpolicy_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    print("[controlled_token_subpolicy_apply] Starting controlled token subpolicy apply")
    print(f"[controlled_token_subpolicy_apply] Rule version: {RULE_VERSION}")
    print(f"[controlled_token_subpolicy_apply] Apply: {apply}")
    print(f"[controlled_token_subpolicy_apply] Reaudit: {reaudit}")
    print(f"[controlled_token_subpolicy_apply] Learning production safe: {learning_safe}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_audit_run_id = audit_run_id or latest_audit_run_id(conn)
        if selected_audit_run_id is None:
            raise RuntimeError("No controlled token subpolicy production audit run found.")
        state_run_id = latest_state_run_id(conn)
        rows = fetch_rows(conn, audit_run_id=selected_audit_run_id, state_run_id=state_run_id)
        counters: Counter = Counter()
        counters["candidates"] = len(rows)
        counters["eligible"] = sum(1 for row in rows if int(row.get("eligible_controlled_production") or 0) == 1)
        counters["estimated_closed_gain"] = sum(1 for row in rows if int(row.get("estimated_closed_gain") or 0) == 1)

        if reaudit:
            actual_closed_gain = sum(
                1
                for row in rows
                if row.get("latest_state_group") == "closed"
                and row.get("latest_final_state") != "reopen_auto_confirmed_autofix"
            )
            counters["ready"] = 0
            counters["applied"] = 0
            report_path = write_report(
                settings,
                audit_run_id=selected_audit_run_id,
                apply=False,
                reaudit=True,
                counters=counters,
                previews=[(row, "closed" if row.get("latest_state_group") == "closed" else "not_closed", []) for row in rows],
                backup_root=None,
                actual_closed_gain=actual_closed_gain,
            )
            print(f"[controlled_token_subpolicy_apply] Audit run id: {selected_audit_run_id}")
            print(f"[controlled_token_subpolicy_apply] Candidates: {counters['candidates']}")
            print(f"[controlled_token_subpolicy_apply] Eligible: {counters['eligible']}")
            print(f"[controlled_token_subpolicy_apply] Ready: {counters['ready']}")
            print(f"[controlled_token_subpolicy_apply] Applied: {counters['applied']}")
            print(f"[controlled_token_subpolicy_apply] Stale: {counters['stale']}")
            print(f"[controlled_token_subpolicy_apply] Blocked: {counters['blocked']}")
            print(f"[controlled_token_subpolicy_apply] Confirmation promoted: {counters['confirmation_promoted']}")
            print(f"[controlled_token_subpolicy_apply] Output written: {counters['output_written']}")
            print(f"[controlled_token_subpolicy_apply] Estimated closed gain: {counters['estimated_closed_gain']}")
            print(f"[controlled_token_subpolicy_apply] Actual closed gain after reaudit: {actual_closed_gain}")
            print(f"[controlled_token_subpolicy_apply] Report: {report_path}")
            print("[controlled_token_subpolicy_apply] Done")
            return

        evaluated: list[tuple[dict[str, Any], str, str | None, list[str]]] = []
        for row in rows:
            status, _current_line, new_line, reasons = evaluate_row(row, output_root=output_root, learning_safe=learning_safe)
            counters[status] += 1
            evaluated.append((row, status, new_line, reasons))

        structural_errors = sum(
            1
            for _row, _status, _new_line, reasons in evaluated
            if any(reason in STRUCTURAL_BLOCKERS for reason in reasons)
        )
        ready_entries = [(row, new_line or "") for row, status, new_line, _reasons in evaluated if status == "ready" and new_line is not None]
        counters["ready"] = len(ready_entries)
        if apply and structural_errors:
            report_path = write_report(
                settings,
                audit_run_id=selected_audit_run_id,
                apply=apply,
                reaudit=False,
                counters=counters,
                previews=[(row, status, reasons) for row, status, _new_line, reasons in evaluated],
                backup_root=None,
            )
            print(f"[controlled_token_subpolicy_apply] Structural errors: {structural_errors}")
            print(f"[controlled_token_subpolicy_apply] Report: {report_path}")
            raise RuntimeError("Controlled token subpolicy write aborted because structural validation failed.")

        if apply and ready_entries:
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

        report_path = write_report(
            settings,
            audit_run_id=selected_audit_run_id,
            apply=apply,
            reaudit=False,
            counters=counters,
            previews=[(row, status, reasons) for row, status, _new_line, reasons in evaluated],
            backup_root=backup_root if apply and ready_entries and create_backup else None,
        )

    print(f"[controlled_token_subpolicy_apply] Audit run id: {selected_audit_run_id}")
    print(f"[controlled_token_subpolicy_apply] Candidates: {counters['candidates']}")
    print(f"[controlled_token_subpolicy_apply] Eligible: {counters['eligible']}")
    print(f"[controlled_token_subpolicy_apply] Ready: {counters['ready']}")
    print(f"[controlled_token_subpolicy_apply] Applied: {counters['applied']}")
    print(f"[controlled_token_subpolicy_apply] Stale: {counters['stale']}")
    print(f"[controlled_token_subpolicy_apply] Blocked: {counters['blocked']}")
    print(f"[controlled_token_subpolicy_apply] Confirmation promoted: {counters['confirmation_promoted']}")
    print(f"[controlled_token_subpolicy_apply] Output written: {counters['output_written']}")
    print(f"[controlled_token_subpolicy_apply] Estimated closed gain: {counters['estimated_closed_gain']}")
    print("[controlled_token_subpolicy_apply] Actual closed gain after reaudit: 0")
    print(f"[controlled_token_subpolicy_apply] Report: {report_path}")
    print("[controlled_token_subpolicy_apply] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Controlled production apply for token subpolicy repairs.")
    parser.add_argument("--audit-run-id", type=int, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reaudit", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    try:
        main(
            audit_run_id=args.audit_run_id,
            apply=args.apply,
            reaudit=args.reaudit,
            create_backup=not args.no_backup,
        )
    except Exception as exc:
        print(f"[controlled_token_subpolicy_apply] ERROR: {exc}", file=sys.stderr)
        raise
