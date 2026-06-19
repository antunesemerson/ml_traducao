from __future__ import annotations

import argparse
import hashlib
import re
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


RULE_VERSION = "select_cstring_governed_bridge_apply_v1"
CONFIRMATION_SOURCE = "select_cstring_governed_bridge_production"
CONFIRMATION_LABEL = "select_cstring_governed_bridge"
REVIEWER = "production_select_cstring_governed_bridge_apply"
EXPECTED_PROPOSAL_STATUS = "ready_for_governed_review"
EXPECTED_ITEM_STATUS = "ready_for_governed_bridge_review"
EXPECTED_MATURITY_STATUS = "bridge_candidate_shadow"
ALLOWED_TOKEN_STATUS = {"same_structural_tokens", "dynamic_literal_payload_only"}
STRUCTURAL_BLOCKERS = {
    "validation_issue",
    "missing_output_file",
    "missing_output_line",
    "line_out_of_range",
    "line_without_quoted_value",
    "checkpoint_table_invalid",
    "checkpoint_reference_missing",
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


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


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


def latest_proposal_run_id(conn) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_select_cstring_governed_bridge_proposal_runs
        WHERE bridge_status = ?
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (EXPECTED_PROPOSAL_STATUS,),
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
    return any(token in haystack for token in ("human", "manual", "reviewed", "gemini", "codex"))


def fetch_run_context(conn, proposal_run_id: int) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    proposal = conn.execute(
        """
        SELECT *
        FROM ml_issue_select_cstring_governed_bridge_proposal_runs
        WHERE id = ?
        """,
        (proposal_run_id,),
    ).fetchone()
    if proposal is None:
        raise RuntimeError(f"Select_CString bridge proposal run {proposal_run_id} not found.")
    proposal_dict = dict(proposal)
    maturity = conn.execute(
        """
        SELECT *
        FROM ml_issue_select_cstring_final_composition_maturity_audit_runs
        WHERE id = ?
        """,
        (proposal_dict.get("source_maturity_audit_run_id"),),
    ).fetchone()
    overlay = conn.execute(
        """
        SELECT *
        FROM ml_issue_select_cstring_final_composition_overlay_runs
        WHERE id = ?
        """,
        (proposal_dict.get("source_final_overlay_run_id"),),
    ).fetchone()
    return proposal_dict, dict(maturity) if maturity else None, dict(overlay) if overlay else None


def run_context_reasons(
    *,
    proposal: dict[str, Any],
    maturity: dict[str, Any] | None,
    overlay: dict[str, Any] | None,
    learning_safe: bool,
) -> list[str]:
    reasons: list[str] = []
    if not learning_safe:
        reasons.append("learning_not_production_safe")
    if proposal.get("bridge_status") != EXPECTED_PROPOSAL_STATUS:
        reasons.append("proposal_not_ready_for_governed_review")
    if int(proposal.get("total_candidates") or 0) != 56 or int(proposal.get("ready_count") or 0) != 56:
        reasons.append("proposal_count_mismatch")
    if int(proposal.get("blocked_count") or 0) != 0:
        reasons.append("proposal_has_blocked_items")
    if int(proposal.get("bridge_candidate") or 0) != 1:
        reasons.append("proposal_not_bridge_candidate")
    if int(proposal.get("production_release_allowed") or 0) != 0:
        reasons.append("proposal_should_remain_shadow_release")
    if maturity is None:
        reasons.append("missing_maturity_audit")
    elif (
        int(maturity.get("bridge_candidate") or 0) != 1
        or maturity.get("maturity_status") != EXPECTED_MATURITY_STATUS
        or int(maturity.get("blocked_count") or 0) != 0
        or int(maturity.get("duplicate_segment_count") or 0) != 0
        or int(maturity.get("missing_source_ref_count") or 0) != 0
        or int(maturity.get("invalid_source_ref_count") or 0) != 0
        or int(maturity.get("unexpected_source_count") or 0) != 0
    ):
        reasons.append("maturity_audit_not_clean")
    if overlay is None:
        reasons.append("missing_final_overlay")
    elif (
        int(overlay.get("candidate_count") or 0) != 56
        or int(overlay.get("composed_released_count") or 0) != 56
        or int(overlay.get("blocked_count") or 0) != 0
    ):
        reasons.append("final_overlay_not_clean")
    return reasons


def fetch_rows(conn, *, proposal_run_id: int, state_run_id: int | None) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.*,
            proposal.bridge_status AS proposal_bridge_status,
            proposal.ready_count AS proposal_ready_count,
            proposal.blocked_count AS proposal_blocked_count,
            proposal.same_token_count AS proposal_same_token_count,
            proposal.dynamic_payload_delta_count AS proposal_dynamic_payload_delta_count,
            proposal.bridge_candidate AS proposal_bridge_candidate,
            proposal.production_release_allowed AS proposal_production_release_allowed,
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
            state.lifecycle_policy_action AS latest_lifecycle_policy_action,
            state.lifecycle_policy_allowed AS latest_lifecycle_policy_allowed,
            state.needs_reopen,
            state.needs_output_apply,
            source.spanish_text
        FROM ml_issue_select_cstring_governed_bridge_proposal_items item
        JOIN ml_issue_select_cstring_governed_bridge_proposal_runs proposal
          ON proposal.id = item.run_id
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
        ORDER BY item.relative_path, item.source_line_number, item.source_key
        """,
        (state_run_id, proposal_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def checkpoint_status(conn, row: dict[str, Any]) -> tuple[bool, list[str]]:
    table = str(row.get("source_checkpoint_table") or "")
    run_id = row.get("source_checkpoint_run_id")
    item_id = row.get("source_checkpoint_item_id")
    reasons: list[str] = []
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        return False, ["checkpoint_table_invalid"]
    if run_id is None or item_id is None:
        return False, ["checkpoint_reference_missing"]
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    ).fetchone()
    if exists is None:
        return False, ["checkpoint_reference_missing"]
    checkpoint = conn.execute(
        f"""
        SELECT *
        FROM {table}
        WHERE id = ?
          AND run_id = ?
        """,
        (item_id, run_id),
    ).fetchone()
    if checkpoint is None:
        return False, ["checkpoint_reference_missing"]
    checkpoint_dict = dict(checkpoint)
    if int(checkpoint_dict.get("checkpoint_allowed") or 0) != 1:
        reasons.append("checkpoint_not_allowed")
    if str(checkpoint_dict.get("block_reason") or "").strip():
        reasons.append("checkpoint_block_reason")
    if checkpoint_dict.get("corrected_text") != row.get("corrected_text"):
        reasons.append("checkpoint_corrected_text_mismatch")
    return not reasons, reasons


def evaluate_row(
    conn,
    row: dict[str, Any],
    *,
    output_root: Path,
    global_reasons: list[str],
) -> tuple[str, str | None, str | None, list[str]]:
    reasons = list(global_reasons)
    corrected_text = row.get("corrected_text")
    confirmed_text = row.get("confirmed_text")
    output_text = row.get("current_output_text")
    corrected_compare = normalize_yml_escaped_text_for_compare(corrected_text)
    confirmed_compare = normalize_yml_escaped_text_for_compare(confirmed_text)
    output_compare = normalize_yml_escaped_text_for_compare(output_text)

    if row.get("bridge_status") != EXPECTED_ITEM_STATUS:
        reasons.append("item_not_ready_for_governed_bridge_review")
    if str(row.get("blocking_reason") or "").strip():
        reasons.append("item_blocking_reason")
    if not str(corrected_text or "").strip():
        reasons.append("missing_corrected_text")
    if row.get("token_status") not in ALLOWED_TOKEN_STATUS:
        reasons.append("unsupported_token_status")
    if sha256_text(output_text) != row.get("current_text_hash"):
        reasons.append("stale_current_text_hash")
    if sha256_text(corrected_text) != row.get("corrected_text_hash"):
        reasons.append("corrected_text_hash_mismatch")
    checkpoint_ok, checkpoint_reasons = checkpoint_status(conn, row)
    if not checkpoint_ok:
        reasons.extend(checkpoint_reasons)
    if int(row.get("locked") or 0) == 1:
        reasons.append("human_locked")
    item_created_at = parse_iso(row.get("created_at"))
    confirmation_updated_at = parse_iso(row.get("confirmation_updated_at"))
    if (
        humanish_confirmation(row)
        and item_created_at
        and confirmation_updated_at
        and confirmation_updated_at > item_created_at
        and corrected_compare != confirmed_compare
    ):
        reasons.append("human_confirmation_newer_than_proposal")
    if row.get("state_item_id") is None:
        reasons.append("missing_latest_segment_state")
    elif row.get("latest_state_group") != "pending" or row.get("latest_final_state") != "reopen_auto_confirmed_autofix":
        if corrected_compare == confirmed_compare == output_compare and row.get("latest_state_group") == "closed":
            pass
        else:
            reasons.append("stale_or_unsupported_segment_state")
    if str(row.get("latest_review_state") or "").lower() in {"manual_review", "human_review", "structural_review"}:
        reasons.append("manual_review_state")
    if blocking_validation_issues(corrected_text):
        reasons.append("validation_issue")

    output_path = output_root / Path(row["relative_path"])
    current_line = None
    new_line = None
    if row.get("output_line_number") is None:
        reasons.append("missing_output_line")
    if not output_path.exists():
        reasons.append("missing_output_file")
    elif row.get("output_line_number") is not None:
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
        stale = any(reason.startswith("stale_") or reason in {"missing_latest_segment_state"} for reason in reasons)
        return ("stale" if stale and not any(reason in STRUCTURAL_BLOCKERS for reason in reasons) else "blocked"), current_line, new_line, reasons
    return "ready", current_line, new_line, []


def make_backup(output_root: Path, backup_root: Path, relative_path: str) -> None:
    source_path = output_root / Path(relative_path)
    backup_path = backup_root / Path(relative_path)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, backup_path)


def promote_confirmation(conn, *, row: dict[str, Any], corrected_text: str, now: str) -> None:
    segment_id = int(row["segment_id"])
    label = f"{CONFIRMATION_LABEL}:proposal_{row.get('run_id')}:item_{row.get('id')}"
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
        (corrected_text, CONFIRMATION_SOURCE, label, REVIEWER, now, now, segment_id),
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
            (segment_id, corrected_text, CONFIRMATION_SOURCE, label, REVIEWER, now, now),
        )


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
            promote_confirmation(conn, row=row, corrected_text=corrected_text, now=now)
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
    proposal_run_id: int,
    maturity_audit_run_id: int | None,
    final_overlay_run_id: int | None,
    state_run_id: int | None,
    apply: bool,
    reaudit: bool,
    counters: Counter,
    previews: list[tuple[dict[str, Any], str, list[str]]],
    backup_root: Path | None,
    proposal: dict[str, Any],
    actual_closed_gain: int = 0,
) -> Path:
    lines = [
        "Select_CString governed bridge apply report",
        f"Started at: {datetime.now().isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Proposal run id: {proposal_run_id}",
        f"Maturity audit run id: {maturity_audit_run_id}",
        f"Final overlay run id: {final_overlay_run_id}",
        f"Segment-state run id: {state_run_id}",
        f"Bridge candidate: {int(proposal.get('bridge_candidate') or 0)}",
        f"Production release allowed: {int(proposal.get('production_release_allowed') or 0)}",
        f"Apply: {apply}",
        f"Reaudit: {reaudit}",
        f"Backup root: {backup_root or 'not created'}",
        "",
        "Summary:",
        f"- candidates: {counters['candidates']}",
        f"- ready: {counters['ready']}",
        f"- applied: {counters['applied']}",
        f"- already_applied: {counters['already_applied']}",
        f"- stale: {counters['stale']}",
        f"- blocked: {counters['blocked']}",
        f"- confirmation_promoted: {counters['confirmation_promoted']}",
        f"- output_written: {counters['output_written']}",
        f"- same_token: {counters['same_token']}",
        f"- dynamic_payload_delta: {counters['dynamic_payload_delta']}",
        f"- estimated_closed_gain: {counters['estimated_closed_gain']}",
        f"- actual_closed_gain_after_reaudit: {actual_closed_gain}",
        "",
        "Preview:",
    ]
    for row, status, reasons in previews[:100]:
        lines.extend(
            [
                f"- segment {row['segment_id']} | {row['relative_path']}:{row['source_line_number']} | {row['source_key']} | {status}",
                f"  BRIDGE_ITEM: {row.get('id')} | CHECKPOINT: {row.get('source_checkpoint_table')}:{row.get('source_checkpoint_run_id')}:{row.get('source_checkpoint_item_id')}",
                f"  TOKEN_STATUS: {row.get('token_status')} | SOURCE: {row.get('composition_source')}",
                f"  REASONS: {', '.join(reasons) if reasons else 'none'}",
                f"  CURRENT: {short(row.get('current_output_text'))}",
                f"  CORRECTED: {short(row.get('corrected_text'))}",
            ]
        )
    if not previews:
        lines.append("- No candidates selected.")
    return db.write_report(settings, "select_cstring_governed_bridge_apply", lines)


def print_summary(
    *,
    proposal_run_id: int,
    maturity_audit_run_id: int | None,
    final_overlay_run_id: int | None,
    proposal: dict[str, Any],
    counters: Counter,
    actual_closed_gain: int,
    report_path: Path,
) -> None:
    prefix = "[select_cstring_governed_bridge_apply]"
    print(f"{prefix} Proposal run id: {proposal_run_id}")
    print(f"{prefix} Maturity audit run id: {maturity_audit_run_id}")
    print(f"{prefix} Final overlay run id: {final_overlay_run_id}")
    print(f"{prefix} Bridge candidate: {int(proposal.get('bridge_candidate') or 0)}")
    print(f"{prefix} Production release allowed: {int(proposal.get('production_release_allowed') or 0)}")
    print(f"{prefix} Candidates: {counters['candidates']}")
    print(f"{prefix} Ready: {counters['ready']}")
    print(f"{prefix} Applied: {counters['applied']}")
    print(f"{prefix} Already applied: {counters['already_applied']}")
    print(f"{prefix} Stale: {counters['stale']}")
    print(f"{prefix} Blocked: {counters['blocked']}")
    print(f"{prefix} Confirmation promoted: {counters['confirmation_promoted']}")
    print(f"{prefix} Output written: {counters['output_written']}")
    print(f"{prefix} Same token: {counters['same_token']}")
    print(f"{prefix} Dynamic payload delta: {counters['dynamic_payload_delta']}")
    print(f"{prefix} Estimated closed gain: {counters['estimated_closed_gain']}")
    print(f"{prefix} Actual closed gain after reaudit: {actual_closed_gain}")
    print(f"{prefix} Report: {report_path}")
    print(f"{prefix} Done")


def main(
    *,
    proposal_run_id: int | None = None,
    apply: bool = False,
    reaudit: bool = False,
    create_backup: bool = True,
) -> None:
    settings = db.load_settings()
    output_root = db.project_path(settings["output_spanish"])
    learning_payload = learning_status.read_status()
    learning_safe = bool(learning_payload.get("production_safe"))
    now = datetime.now().isoformat(timespec="seconds")
    backup_root = db.project_path("memory/backups") / f"select_cstring_governed_bridge_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    print("[select_cstring_governed_bridge_apply] Starting Select_CString governed bridge apply")
    print(f"[select_cstring_governed_bridge_apply] Rule version: {RULE_VERSION}")
    print(f"[select_cstring_governed_bridge_apply] Apply: {apply}")
    print(f"[select_cstring_governed_bridge_apply] Reaudit: {reaudit}")
    print(f"[select_cstring_governed_bridge_apply] Learning production safe: {learning_safe}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_proposal_run_id = proposal_run_id or latest_proposal_run_id(conn)
        if selected_proposal_run_id is None:
            raise RuntimeError("No ready Select_CString governed bridge proposal run found.")
        proposal, maturity, overlay = fetch_run_context(conn, selected_proposal_run_id)
        maturity_id = int(proposal.get("source_maturity_audit_run_id") or 0) or None
        overlay_id = int(proposal.get("source_final_overlay_run_id") or 0) or None
        state_run_id = latest_state_run_id(conn)
        global_reasons = run_context_reasons(
            proposal=proposal,
            maturity=maturity,
            overlay=overlay,
            learning_safe=learning_safe,
        )
        rows = fetch_rows(conn, proposal_run_id=selected_proposal_run_id, state_run_id=state_run_id)
        counters: Counter = Counter()
        counters["candidates"] = len(rows)
        counters["same_token"] = sum(1 for row in rows if row.get("token_status") == "same_structural_tokens")
        counters["dynamic_payload_delta"] = sum(1 for row in rows if row.get("token_status") == "dynamic_literal_payload_only")
        counters["estimated_closed_gain"] = len(rows)

        if reaudit:
            actual_closed_gain = sum(
                1
                for row in rows
                if row.get("latest_state_group") == "closed"
                and row.get("latest_final_state") != "reopen_auto_confirmed_autofix"
            )
            report_path = write_report(
                settings,
                proposal_run_id=selected_proposal_run_id,
                maturity_audit_run_id=maturity_id,
                final_overlay_run_id=overlay_id,
                state_run_id=state_run_id,
                apply=False,
                reaudit=True,
                counters=counters,
                previews=[(row, "closed" if row.get("latest_state_group") == "closed" else "not_closed", []) for row in rows],
                backup_root=None,
                proposal=proposal,
                actual_closed_gain=actual_closed_gain,
            )
            print_summary(
                proposal_run_id=selected_proposal_run_id,
                maturity_audit_run_id=maturity_id,
                final_overlay_run_id=overlay_id,
                proposal=proposal,
                counters=counters,
                actual_closed_gain=actual_closed_gain,
                report_path=report_path,
            )
            return

        evaluated: list[tuple[dict[str, Any], str, str | None, list[str]]] = []
        for row in rows:
            status, _current_line, new_line, reasons = evaluate_row(
                conn,
                row,
                output_root=output_root,
                global_reasons=global_reasons,
            )
            counters[status] += 1
            evaluated.append((row, status, new_line, reasons))

        ready_entries = [(row, new_line or "") for row, status, new_line, _reasons in evaluated if status == "ready" and new_line is not None]
        counters["ready"] = len(ready_entries)
        structural_errors = sum(
            1
            for _row, _status, _new_line, reasons in evaluated
            if any(reason in STRUCTURAL_BLOCKERS for reason in reasons)
        )
        if apply and structural_errors:
            report_path = write_report(
                settings,
                proposal_run_id=selected_proposal_run_id,
                maturity_audit_run_id=maturity_id,
                final_overlay_run_id=overlay_id,
                state_run_id=state_run_id,
                apply=apply,
                reaudit=False,
                counters=counters,
                previews=[(row, status, reasons) for row, status, _new_line, reasons in evaluated],
                backup_root=None,
                proposal=proposal,
            )
            print(f"[select_cstring_governed_bridge_apply] Structural errors: {structural_errors}")
            print(f"[select_cstring_governed_bridge_apply] Report: {report_path}")
            raise RuntimeError("Select_CString governed bridge write aborted because structural validation failed.")

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
            proposal_run_id=selected_proposal_run_id,
            maturity_audit_run_id=maturity_id,
            final_overlay_run_id=overlay_id,
            state_run_id=state_run_id,
            apply=apply,
            reaudit=False,
            counters=counters,
            previews=[(row, status, reasons) for row, status, _new_line, reasons in evaluated],
            backup_root=backup_root if apply and ready_entries and create_backup else None,
            proposal=proposal,
        )

    print_summary(
        proposal_run_id=selected_proposal_run_id,
        maturity_audit_run_id=maturity_id,
        final_overlay_run_id=overlay_id,
        proposal=proposal,
        counters=counters,
        actual_closed_gain=0,
        report_path=report_path,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Governed production bridge for Select_CString shadow proposals.")
    parser.add_argument("--proposal-run-id", type=int, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reaudit", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    try:
        main(
            proposal_run_id=args.proposal_run_id,
            apply=args.apply,
            reaudit=args.reaudit,
            create_backup=not args.no_backup,
        )
    except Exception as exc:
        print(f"[select_cstring_governed_bridge_apply] ERROR: {exc}", file=sys.stderr)
        raise
