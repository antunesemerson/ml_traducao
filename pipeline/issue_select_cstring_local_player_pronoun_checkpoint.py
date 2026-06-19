from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator
from apply_segment_state_updates import short
from issue_dynamic_token_literal_repair_checkpoint import dynamic_payload_changes_only


RULE_VERSION = "issue_select_cstring_local_player_pronoun_checkpoint_v1"
POLICY_NAME = "select_cstring_local_player_pronoun_literal_shadow_v1"
POLICY_STATUS = "shadow"
AGENT_KEY = "micro_select_cstring_local_player_pronoun_literal"
SUBPOLICY_NAME = "select_cstring_local_player_pronoun_literal"
CHECKPOINT_ACTION = "stage_select_cstring_local_player_pronoun_literal_shadow"
PRODUCTION_RELEASE_ALLOWED = 0

LOCAL_PLAYER_TU_RE = re.compile(
    r"Select_CString\(\s*([A-Za-z0-9_]+)\.IsLocalPlayer\s*,\s*'tú'\s*,\s*([A-Za-z0-9_]+)\.GetSheHe\s*\)",
    re.IGNORECASE,
)

BLOCKING_VALIDATION_CODES = {
    "spanish_residue",
    "spanish_residue_in_literal",
    "spanish_punctuation",
    "token_breakage",
    "placeholder_breakage",
}


def table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def latest_dynamic_payload_checkpoint_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_select_cstring_dynamic_literal_payload_checkpoint_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise RuntimeError("No completed Select_CString dynamic literal payload checkpoint run found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_local_player_pronoun_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            source_dynamic_payload_checkpoint_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            allowed_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            block_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_local_player_pronoun_checkpoint_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            source_dynamic_payload_checkpoint_run_id INTEGER NOT NULL,
            source_dynamic_payload_item_id INTEGER NOT NULL,
            source_overlay_run_id INTEGER NOT NULL,
            source_overlay_item_id INTEGER NOT NULL,
            source_lifecycle_item_id INTEGER NOT NULL,
            source_dynamic_checkpoint_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            agent_key TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            checkpoint_action TEXT NOT NULL,
            block_reason TEXT,
            token_status TEXT NOT NULL,
            current_text TEXT NOT NULL,
            corrected_text TEXT,
            validation_issues_json TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_select_cstring_local_player_pronoun_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], source_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_select_cstring_local_player_pronoun_checkpoint_dynamic_payload_run_{source_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def blocking_validation_issues(text: str | None) -> list[dict[str, Any]]:
    validation = local_quality_validator.validate_text(text)
    issues = validation.get("issues") or []
    return [
        issue
        for issue in issues
        if issue.get("severity") == "high" or issue.get("code") in BLOCKING_VALIDATION_CODES
    ]


def fetch_rows(conn, *, source_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.id AS source_dynamic_payload_item_id,
            item.source_overlay_run_id,
            item.source_overlay_item_id,
            item.source_lifecycle_item_id,
            item.source_dynamic_checkpoint_item_id,
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.source_line_number,
            item.token_status AS source_token_status,
            item.block_reason AS source_block_reason,
            item.current_text,
            item.corrected_text,
            item.validation_issues_json AS source_validation_issues_json,
            item.reasons_json AS source_reasons_json
        FROM ml_issue_select_cstring_dynamic_literal_payload_checkpoint_items item
        WHERE item.run_id = ?
          AND item.checkpoint_allowed = 0
          AND item.block_reason = 'blocking_validation_issue'
          AND item.validation_issues_json LIKE '%tú%'
        ORDER BY item.segment_id
        """,
        (source_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def propose_repair(text: str) -> tuple[str, list[str]]:
    reasons: list[str] = []

    def replace(match: re.Match[str]) -> str:
        condition_owner = match.group(1)
        pronoun_owner = match.group(2)
        if condition_owner != pronoun_owner:
            return match.group(0)
        reasons.append(f"local_player_pronoun_literal:{condition_owner}.IsLocalPlayer:tú->você")
        return f"Select_CString( {condition_owner}.IsLocalPlayer, 'você', {pronoun_owner}.GetSheHe )"

    repaired = LOCAL_PLAYER_TU_RE.sub(replace, text)
    return repaired, reasons


def classify(row: dict[str, Any]) -> tuple[int, str, str, str, list[dict[str, Any]], list[str]]:
    current = row.get("current_text") or ""
    source_corrected = row.get("corrected_text") or ""
    corrected, reasons = propose_repair(source_corrected)
    source_reasons = json.loads(row.get("source_reasons_json") or "[]")
    reasons = source_reasons + reasons
    validation_issues = blocking_validation_issues(corrected)

    if row.get("source_token_status") != "dynamic_literal_payload_only_with_validation_block":
        return 0, "source_token_status_not_validation_block", "not_applicable", corrected, validation_issues, reasons
    if not current.strip() or not source_corrected.strip():
        return 0, "missing_text", "missing_text", corrected, validation_issues, reasons
    if corrected == source_corrected:
        return 0, "no_local_player_pronoun_repair", "no_text_delta", corrected, validation_issues, reasons
    if not dynamic_payload_changes_only(current, corrected):
        return (
            0,
            "changes_not_limited_to_dynamic_literal_payload",
            "structural_token_change_review_required",
            corrected,
            validation_issues,
            reasons,
        )
    if validation_issues:
        return (
            0,
            "blocking_validation_issue",
            "local_player_pronoun_literal_validation_block",
            corrected,
            validation_issues,
            reasons,
        )
    return 1, "", "local_player_pronoun_literal_validated", corrected, validation_issues, reasons


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    source_run_id: int,
    rows: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    fields = [
        "checkpoint_allowed",
        "block_reason",
        "source_dynamic_payload_item_id",
        "source_lifecycle_item_id",
        "source_dynamic_checkpoint_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "subpolicy_name",
        "token_status",
        "current_text",
        "corrected_text",
        "validation_issues",
        "reasons",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            payload = dict(row)
            payload["validation_issues"] = json.dumps(row["validation_issues"], ensure_ascii=False, sort_keys=True)
            payload["reasons"] = "; ".join(row["reasons"])
            writer.writerow(payload)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Issue Select_CString local-player pronoun literal checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Source dynamic payload checkpoint run id: {source_run_id}",
        f"Policy: {POLICY_NAME}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Allowed shadow: {counts['allowed']:,}",
        f"- Blocked: {counts['blocked']:,}",
        "",
        "Token status:",
        *[f"- {key}: {value:,}" for key, value in counts.items() if key.startswith("token:")],
        "",
        "Blocks:",
        *([f"- {key}: {value:,}" for key, value in counts.items() if key.startswith("block:")] or ["- none"]),
        "",
        "Samples:",
    ]
    for row in rows[:40]:
        lines.extend(
            [
                (
                    f"- allowed={row['checkpoint_allowed']} token={row['token_status']} "
                    f"block={row['block_reason'] or 'none'} segment={row['segment_id']} "
                    f"{row['relative_path']}::{row['source_key']}"
                ),
                f"  current: {short(row['current_text'], 260)}",
                f"  corrected: {short(row['corrected_text'], 260)}",
                f"  validation: {json.dumps(row['validation_issues'], ensure_ascii=False)}",
                f"  reasons: {', '.join(row['reasons'])}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety:",
            "- Shadow-only checkpoint; no source/output files read and no output is written.",
            "- Allows only local-player pronoun literal repair inside Select_CString dynamic payloads.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Checkpoint local-player pronoun literal repairs inside Select_CString.")
    parser.add_argument("--source-dynamic-payload-checkpoint-run-id", type=int, default=None)
    args = parser.parse_args()

    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        source_run_id = args.source_dynamic_payload_checkpoint_run_id or latest_dynamic_payload_checkpoint_run_id(conn)
        txt_path, csv_path, jsonl_path = report_paths(settings, source_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_select_cstring_local_player_pronoun_checkpoint_runs (
                rule_version,
                policy_name,
                policy_status,
                source_dynamic_payload_checkpoint_run_id,
                production_release_allowed,
                started_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (RULE_VERSION, POLICY_NAME, POLICY_STATUS, source_run_id, PRODUCTION_RELEASE_ALLOWED, now, now),
        )
        run_id = int(cursor.lastrowid)

        rows: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        for source_row in fetch_rows(conn, source_run_id=source_run_id):
            allowed, block_reason, token_status, corrected, validation_issues, reasons = classify(source_row)
            row = {
                **source_row,
                "agent_key": AGENT_KEY,
                "subpolicy_name": SUBPOLICY_NAME,
                "checkpoint_allowed": allowed,
                "checkpoint_action": CHECKPOINT_ACTION,
                "block_reason": block_reason,
                "token_status": token_status,
                "corrected_text": corrected,
                "validation_issues": validation_issues,
                "reasons": reasons,
            }
            rows.append(row)
            counts["allowed" if allowed else "blocked"] += 1
            counts[f"token:{token_status}"] += 1
            if block_reason:
                counts[f"block:{block_reason}"] += 1
            conn.execute(
                """
                INSERT INTO ml_issue_select_cstring_local_player_pronoun_checkpoint_items (
                    run_id,
                    source_dynamic_payload_checkpoint_run_id,
                    source_dynamic_payload_item_id,
                    source_overlay_run_id,
                    source_overlay_item_id,
                    source_lifecycle_item_id,
                    source_dynamic_checkpoint_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    agent_key,
                    subpolicy_name,
                    checkpoint_allowed,
                    checkpoint_action,
                    block_reason,
                    token_status,
                    current_text,
                    corrected_text,
                    validation_issues_json,
                    reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    source_run_id,
                    row["source_dynamic_payload_item_id"],
                    row["source_overlay_run_id"],
                    row["source_overlay_item_id"],
                    row["source_lifecycle_item_id"],
                    row["source_dynamic_checkpoint_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["agent_key"],
                    row["subpolicy_name"],
                    row["checkpoint_allowed"],
                    row["checkpoint_action"],
                    row["block_reason"],
                    row["token_status"],
                    row["current_text"],
                    row["corrected_text"],
                    json.dumps(row["validation_issues"], ensure_ascii=False, sort_keys=True),
                    json.dumps(row["reasons"], ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )

        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            source_run_id=source_run_id,
            rows=rows,
            counts=counts,
        )
        finished_at = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            UPDATE ml_issue_select_cstring_local_player_pronoun_checkpoint_runs
            SET candidate_count = ?,
                allowed_count = ?,
                blocked_count = ?,
                block_counts_json = ?,
                report_path = ?,
                csv_path = ?,
                jsonl_path = ?,
                finished_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                len(rows),
                counts["allowed"],
                counts["blocked"],
                json.dumps({k: v for k, v in counts.items() if k.startswith("block:")}, ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                finished_at,
                finished_at,
                run_id,
            ),
        )
        conn.commit()

    payload = {
        "run_id": run_id,
        "source_dynamic_payload_checkpoint_run_id": source_run_id,
        "candidate_count": len(rows),
        "allowed_count": counts["allowed"],
        "blocked_count": counts["blocked"],
        "production_release_allowed": PRODUCTION_RELEASE_ALLOWED,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return payload


if __name__ == "__main__":
    main()
