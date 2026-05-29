from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator
from apply_segment_state_updates import short, structural_tokens


RULE_VERSION = "segment_token_policy_confirmation_fixes_v1"
FIX_DECISIONS = {"fix_confirmed_text", "encoding_cleanup_required", "manual_token_rewrite_required"}
LABEL = "token_policy_confirmed_text_fixed"
READY_STATUSES = {"ready", "ready_manual_token_restore_to_output"}


def sha256_text(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def latest_policy_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_token_policy_runs
        WHERE finished_at IS NOT NULL
          AND total_candidates > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No complete segment_token_policy_runs entry found.")
    return int(row["id"])


def fetch_rows(conn, *, policy_run_id: int, limit: int | None) -> list[dict[str, Any]]:
    params: list[Any] = [policy_run_id, *sorted(FIX_DECISIONS)]
    placeholders = ", ".join("?" for _ in FIX_DECISIONS)
    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT ?"
        params.append(limit)
    rows = conn.execute(
        f"""
        SELECT
            d.id AS decision_id,
            d.policy_run_id,
            d.policy_item_id,
            d.segment_id,
            d.decision,
            d.corrected_text,
            d.confirmed_text_hash AS reviewed_confirmed_text_hash,
            d.notes,
            sc.id AS confirmation_id,
            sc.confirmed_text,
            sc.confirmation_label,
            o.portuguese_text AS output_text,
            i.relative_path,
            i.source_key,
            i.policy_bucket,
            i.risk_level
        FROM segment_token_policy_decisions d
        JOIN segment_confirmations sc ON sc.segment_id = d.segment_id
        JOIN segment_token_policy_items i ON i.id = d.policy_item_id
        LEFT JOIN output_segments o ON o.segment_id = d.segment_id
        WHERE d.policy_run_id = ?
          AND d.decision IN ({placeholders})
          AND d.corrected_text IS NOT NULL
          AND trim(d.corrected_text) <> ''
        ORDER BY d.policy_item_id
        {limit_sql}
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def classify(row: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    current = row["confirmed_text"] or ""
    corrected = row["corrected_text"] or ""
    if current == corrected:
        return "already_matches", ["current_confirmed_text_already_corrected"]
    if row["reviewed_confirmed_text_hash"] != sha256_text(current):
        return "stale_reviewed_confirmed_hash", ["current_confirmed_text_changed_since_review"]
    if structural_tokens(current) != structural_tokens(corrected):
        output_tokens = structural_tokens(row.get("output_text"))
        corrected_tokens = structural_tokens(corrected)
        if row["decision"] == "manual_token_rewrite_required" and corrected_tokens == output_tokens:
            reasons.append("manual_token_rewrite_restores_output_tokens")
        else:
            return "blocked_token_change", ["corrected_text_changes_structural_tokens"]
    issues = local_quality_validator.validate_text(corrected)["issues"]
    issue_codes = {str(issue.get("code")) for issue in issues}
    if "replacement_question_mark_mojibake" in issue_codes:
        return "blocked_still_mojibake", ["validator_still_flags_question_mark_mojibake"]
    if reasons:
        return "ready_manual_token_restore_to_output", reasons
    return "ready", reasons


def append_label(label: str | None) -> str:
    parts = [part for part in (label or "").split(";") if part]
    if LABEL not in parts:
        parts.append(LABEL)
    return ";".join(parts)


def apply_rows(conn, rows: list[dict[str, Any]]) -> int:
    applied = 0
    now = db.utc_now()
    for row in rows:
        if row["fix_status"] not in READY_STATUSES:
            continue
        conn.execute(
            """
            UPDATE segment_confirmations
            SET confirmed_text = ?,
                confirmation_label = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                row["corrected_text"],
                append_label(row["confirmation_label"]),
                now,
                row["confirmation_id"],
            ),
        )
        applied += 1
    return applied


def write_report(settings: dict, *, rows: list[dict[str, Any]], apply: bool, applied: int, policy_run_id: int) -> Path:
    counts = Counter(row["fix_status"] for row in rows)
    report_lines = [
        "Segment token policy confirmation fix report",
        f"Started at: {datetime.now().isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Policy run id: {policy_run_id}",
        f"Apply: {apply}",
        "",
        "Summary:",
        f"- Rows inspected: {len(rows)}",
        f"- Ready: {sum(counts[status] for status in READY_STATUSES)}",
        f"- Applied: {applied}",
        "",
        "Status counts:",
        *[f"- {key}: {value}" for key, value in counts.most_common()],
        "",
        "Preview:",
    ]
    for row in rows[:80]:
        report_lines.extend(
            [
                f"- decision {row['decision_id']} | item {row['policy_item_id']} | segment {row['segment_id']} | {row['fix_status']} | {row['source_key']}",
                f"  current: {short(row['confirmed_text'], 240)}",
                f"  corrected: {short(row['corrected_text'], 240)}",
            ]
        )
        if row["fix_reasons"]:
            report_lines.append(f"  reasons: {json.dumps(row['fix_reasons'], ensure_ascii=False)}")
    return db.write_report(settings, "segment_token_policy_confirmation_fixes", report_lines)


def main(*, policy_run_id: int | None = None, limit: int | None = None, apply: bool = False) -> None:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_policy_run_id = policy_run_id or latest_policy_run_id(conn)
        rows = fetch_rows(conn, policy_run_id=selected_policy_run_id, limit=limit)
        analyzed: list[dict[str, Any]] = []
        for row in rows:
            status, reasons = classify(row)
            analyzed.append({**row, "fix_status": status, "fix_reasons": reasons})
        applied = apply_rows(conn, analyzed) if apply else 0
        report_path = write_report(
            settings,
            rows=analyzed,
            apply=apply,
            applied=applied,
            policy_run_id=selected_policy_run_id,
        )
        conn.commit()

    print("[segment_token_policy_confirmation_fixes] Confirmation fixes inspected")
    print(f"[segment_token_policy_confirmation_fixes] Rule version: {RULE_VERSION}")
    print(f"[segment_token_policy_confirmation_fixes] Policy run id: {selected_policy_run_id}")
    print(f"[segment_token_policy_confirmation_fixes] Rows inspected: {len(analyzed)}")
    print(f"[segment_token_policy_confirmation_fixes] Ready: {sum(1 for row in analyzed if row['fix_status'] in READY_STATUSES)}")
    print(f"[segment_token_policy_confirmation_fixes] Applied: {applied}")
    print(f"[segment_token_policy_confirmation_fixes] Report: {report_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apply corrected confirmation text from token policy decisions.")
    parser.add_argument("--policy-run-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    main(policy_run_id=args.policy_run_id, limit=args.limit, apply=args.apply)
