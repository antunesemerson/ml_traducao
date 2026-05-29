from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "segment_token_policy_decision_rebase_v1"
REVIEWER = "codex_token_policy_rebase"


def utc_now() -> str:
    return db.utc_now()


def sha256_text(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def latest_state_run_id(conn) -> int:
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
    if row is None:
        raise RuntimeError("No complete segment_state_runs snapshot found.")
    return int(row["id"])


def latest_policy_run_id(conn, state_run_id: int | None = None) -> int:
    params: list[Any] = []
    state_sql = ""
    if state_run_id is not None:
        state_sql = "AND state_run_id = ?"
        params.append(state_run_id)
    row = conn.execute(
        f"""
        SELECT id
        FROM segment_token_policy_runs
        WHERE finished_at IS NOT NULL
          AND total_candidates > 0
          {state_sql}
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    if row is None:
        raise RuntimeError("No complete segment_token_policy_runs entry found.")
    return int(row["id"])


def same_token_signature(row: dict[str, Any]) -> bool:
    return (
        (row.get("old_missing_tokens_json") or "") == (row.get("new_missing_tokens_json") or "")
        and (row.get("old_extra_tokens_json") or "") == (row.get("new_extra_tokens_json") or "")
        and (row.get("old_issue_flags_json") or "") == (row.get("new_issue_flags_json") or "")
    )


def same_bucket_and_risk(row: dict[str, Any]) -> bool:
    return (
        row.get("old_policy_bucket") == row.get("new_policy_bucket")
        and row.get("old_risk_level") == row.get("new_risk_level")
    )


def classify(row: dict[str, Any]) -> str:
    if not row.get("old_decision_id"):
        return "no_approved_token_policy_decision"
    if row.get("old_confirmed_text_hash") != sha256_text(row.get("confirmed_text")):
        return "do_not_rebase_confirmed_text_changed"
    if row.get("old_output_text_hash") == sha256_text(row.get("current_output_text") or ""):
        return "already_current"
    if not row.get("new_policy_item_id"):
        return "do_not_rebase_no_latest_policy_item"
    if same_bucket_and_risk(row) and same_token_signature(row):
        return "safe_rebase_same_token_signature"
    if same_bucket_and_risk(row):
        return "review_rebase_bucket_same_signature_changed"
    return "review_rebase_bucket_changed"


def fetch_needs_apply_rows(conn, *, state_run_id: int, policy_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            i.segment_id,
            i.relative_path,
            i.source_key,
            i.source_line_number,
            i.review_state,
            i.confirmation_level,
            i.confirmation_label,
            i.locked,
            i.active_action,
            i.policy_action,
            sc.confirmed_text,
            o.portuguese_text AS current_output_text,
            d.id AS old_decision_id,
            d.run_id AS old_decision_run_id,
            d.policy_run_id AS old_policy_run_id,
            d.policy_item_id AS old_policy_item_id,
            d.decision AS old_decision,
            d.corrected_text AS old_corrected_text,
            d.notes AS old_notes,
            d.reviewer AS old_reviewer,
            d.confirmed_text_hash AS old_confirmed_text_hash,
            d.output_text_hash AS old_output_text_hash,
            oldi.policy_bucket AS old_policy_bucket,
            oldi.risk_level AS old_risk_level,
            oldi.missing_tokens_json AS old_missing_tokens_json,
            oldi.extra_tokens_json AS old_extra_tokens_json,
            oldi.issue_flags_json AS old_issue_flags_json,
            newi.id AS new_policy_item_id,
            newi.run_id AS new_policy_run_id,
            newi.policy_bucket AS new_policy_bucket,
            newi.risk_level AS new_risk_level,
            newi.missing_tokens_json AS new_missing_tokens_json,
            newi.extra_tokens_json AS new_extra_tokens_json,
            newi.issue_flags_json AS new_issue_flags_json
        FROM segment_state_items i
        JOIN segment_confirmations sc ON sc.segment_id = i.segment_id
        LEFT JOIN output_segments o ON o.segment_id = i.segment_id
        LEFT JOIN segment_token_policy_items newi
          ON newi.segment_id = i.segment_id
         AND newi.run_id = ?
        LEFT JOIN segment_token_policy_decisions d
          ON d.segment_id = i.segment_id
         AND d.approved_for_apply = 1
        LEFT JOIN segment_token_policy_items oldi
          ON oldi.id = d.policy_item_id
        WHERE i.run_id = ?
          AND i.apply_state = 'needs_apply'
        ORDER BY
          i.segment_id,
          d.updated_at DESC,
          d.id DESC
        """,
        (policy_run_id, state_run_id),
    ).fetchall()

    deduped: list[dict[str, Any]] = []
    seen_segments: set[int] = set()
    for row in rows:
        segment_id = int(row["segment_id"])
        if segment_id in seen_segments:
            continue
        seen_segments.add(segment_id)
        payload = dict(row)
        payload["rebase_status"] = classify(payload)
        deduped.append(payload)
    return deduped


def insert_decision_run(
    conn,
    *,
    policy_run_id: int,
    report_path: Path,
    started_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO segment_token_policy_decision_runs (
            rule_version,
            policy_run_id,
            source_report,
            decisions_path,
            started_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (RULE_VERSION, policy_run_id, "token_policy_decision_rebase", str(report_path), started_at, started_at),
    )
    return int(cur.lastrowid)


def upsert_rebased_decision(
    conn,
    *,
    run_id: int,
    row: dict[str, Any],
    reviewer: str,
    now: str,
) -> None:
    reasons_json = json.dumps(
        {
            "rule_version": RULE_VERSION,
            "rebase_status": row["rebase_status"],
            "old_decision_id": row["old_decision_id"],
            "old_decision_run_id": row["old_decision_run_id"],
            "old_policy_run_id": row["old_policy_run_id"],
            "old_policy_item_id": row["old_policy_item_id"],
            "new_policy_run_id": row["new_policy_run_id"],
            "new_policy_item_id": row["new_policy_item_id"],
            "old_policy_bucket": row["old_policy_bucket"],
            "new_policy_bucket": row["new_policy_bucket"],
            "old_risk_level": row["old_risk_level"],
            "new_risk_level": row["new_risk_level"],
            "token_signature_guard": "same_bucket_risk_missing_extra_issue_flags",
        },
        ensure_ascii=False,
    )
    notes = (
        f"Rebased approved token-policy decision {row['old_decision_id']} "
        f"from policy run {row['old_policy_run_id']} to {row['new_policy_run_id']} "
        "after output/source refresh; token signature unchanged."
    )
    conn.execute(
        """
        INSERT INTO segment_token_policy_decisions (
            run_id,
            policy_run_id,
            policy_item_id,
            segment_id,
            relative_path,
            source_key,
            policy_bucket,
            risk_level,
            decision,
            approved_for_apply,
            corrected_text,
            notes,
            reviewer,
            confirmed_text_hash,
            output_text_hash,
            reasons_json,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(policy_run_id, policy_item_id) DO UPDATE SET
            run_id = excluded.run_id,
            decision = excluded.decision,
            approved_for_apply = excluded.approved_for_apply,
            corrected_text = excluded.corrected_text,
            notes = excluded.notes,
            reviewer = excluded.reviewer,
            confirmed_text_hash = excluded.confirmed_text_hash,
            output_text_hash = excluded.output_text_hash,
            reasons_json = excluded.reasons_json,
            updated_at = excluded.updated_at
        """,
        (
            run_id,
            row["new_policy_run_id"],
            row["new_policy_item_id"],
            row["segment_id"],
            row["relative_path"],
            row["source_key"],
            row["new_policy_bucket"],
            row["new_risk_level"],
            row["old_decision"],
            row.get("old_corrected_text"),
            notes,
            reviewer,
            sha256_text(row.get("confirmed_text")),
            sha256_text(row.get("current_output_text") or ""),
            reasons_json,
            now,
            now,
        ),
    )


def update_decision_run(
    conn,
    *,
    run_id: int,
    total: int,
    applied: int,
    skipped: int,
    report_path: Path,
    finished_at: str,
) -> None:
    conn.execute(
        """
        UPDATE segment_token_policy_decision_runs
        SET
            total_decisions = ?,
            approved_count = ?,
            rejected_count = 0,
            fix_count = 0,
            skipped_count = ?,
            report_path = ?,
            finished_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (total, applied, skipped, str(report_path), finished_at, finished_at, run_id),
    )


def write_reports(
    settings: dict[str, Any],
    *,
    started_at: datetime,
    state_run_id: int,
    policy_run_id: int,
    rows: list[dict[str, Any]],
    apply: bool,
    applied: int,
    decision_run_id: int | None,
) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    txt_path = reports_dir / f"{timestamp}_segment_token_policy_decision_rebase.txt"
    jsonl_path = reports_dir / f"{timestamp}_segment_token_policy_decision_rebase.jsonl"

    counts = Counter(row["rebase_status"] for row in rows)
    by_bucket: Counter[tuple[str, str, str]] = Counter(
        (
            row["rebase_status"],
            row.get("new_policy_bucket") or row.get("old_policy_bucket") or "<none>",
            row.get("new_risk_level") or row.get("old_risk_level") or "<none>",
        )
        for row in rows
    )
    by_label: Counter[tuple[str, str]] = Counter(
        (row["rebase_status"], row.get("confirmation_label") or "<none>") for row in rows
    )

    lines = [
        "Segment token policy decision rebase",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"State run id: {state_run_id}",
        f"Policy run id: {policy_run_id}",
        f"Apply: {apply}",
        f"Decision run id: {decision_run_id or 'not created'}",
        "",
        "Summary:",
        f"- Needs-apply rows inspected: {len(rows)}",
        f"- Safe rebases: {counts['safe_rebase_same_token_signature']}",
        f"- Applied rebases: {applied}",
        f"- No approved prior decision: {counts['no_approved_token_policy_decision']}",
        f"- Confirmed text changed after old decision: {counts['do_not_rebase_confirmed_text_changed']}",
        f"- Already current: {counts['already_current']}",
        f"- Needs manual rebase review: {counts['review_rebase_bucket_same_signature_changed'] + counts['review_rebase_bucket_changed']}",
        "",
        "Status counts:",
    ]
    lines.extend(f"- {key}: {value}" for key, value in counts.most_common())
    lines.extend(["", "Top buckets:"])
    for (status, bucket, risk), value in by_bucket.most_common(25):
        lines.append(f"- {status} | {bucket} | {risk}: {value}")
    lines.extend(["", "Top labels:"])
    for (status, label), value in by_label.most_common(25):
        lines.append(f"- {status} | {label}: {value}")
    lines.extend(["", "Sample:"])
    for row in rows[:120]:
        lines.append(
            "- {status} | segment={segment_id} | {relative_path}:{line} | {key} | "
            "{old_bucket}->{new_bucket} | {old_risk}->{new_risk} | label={label}".format(
                status=row["rebase_status"],
                segment_id=row["segment_id"],
                relative_path=row["relative_path"],
                line=row["source_line_number"],
                key=row["source_key"],
                old_bucket=row.get("old_policy_bucket") or "<none>",
                new_bucket=row.get("new_policy_bucket") or "<none>",
                old_risk=row.get("old_risk_level") or "<none>",
                new_risk=row.get("new_risk_level") or "<none>",
                label=row.get("confirmation_label") or "<none>",
            )
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    return txt_path, jsonl_path


def main(
    *,
    state_run_id: int | None = None,
    policy_run_id: int | None = None,
    apply: bool = False,
    reviewer: str = REVIEWER,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    applied = 0
    decision_run_id: int | None = None

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_state_run_id = state_run_id or latest_state_run_id(conn)
        selected_policy_run_id = policy_run_id or latest_policy_run_id(conn, selected_state_run_id)
        rows = fetch_needs_apply_rows(
            conn,
            state_run_id=selected_state_run_id,
            policy_run_id=selected_policy_run_id,
        )
        report_path, _jsonl_path = write_reports(
            settings,
            started_at=started_at,
            state_run_id=selected_state_run_id,
            policy_run_id=selected_policy_run_id,
            rows=rows,
            apply=apply,
            applied=0,
            decision_run_id=None,
        )

        safe_rows = [row for row in rows if row["rebase_status"] == "safe_rebase_same_token_signature"]
        if apply and safe_rows:
            now = utc_now()
            decision_run_id = insert_decision_run(
                conn,
                policy_run_id=selected_policy_run_id,
                report_path=report_path,
                started_at=now,
            )
            for row in safe_rows:
                upsert_rebased_decision(conn, run_id=decision_run_id, row=row, reviewer=reviewer, now=now)
            applied = len(safe_rows)
            update_decision_run(
                conn,
                run_id=decision_run_id,
                total=len(safe_rows),
                applied=applied,
                skipped=max(0, len(rows) - applied),
                report_path=report_path,
                finished_at=utc_now(),
            )
            conn.commit()
            report_path, _jsonl_path = write_reports(
                settings,
                started_at=started_at,
                state_run_id=selected_state_run_id,
                policy_run_id=selected_policy_run_id,
                rows=rows,
                apply=apply,
                applied=applied,
                decision_run_id=decision_run_id,
            )
            update_decision_run(
                conn,
                run_id=decision_run_id,
                total=len(safe_rows),
                applied=applied,
                skipped=max(0, len(rows) - applied),
                report_path=report_path,
                finished_at=utc_now(),
            )
            conn.commit()

    counts = Counter(row["rebase_status"] for row in rows)
    print("[segment_token_policy_decision_rebase] Rebase audit complete")
    print(f"[segment_token_policy_decision_rebase] Rule version: {RULE_VERSION}")
    print(f"[segment_token_policy_decision_rebase] State run id: {selected_state_run_id}")
    print(f"[segment_token_policy_decision_rebase] Policy run id: {selected_policy_run_id}")
    print(f"[segment_token_policy_decision_rebase] Needs-apply rows inspected: {len(rows)}")
    print(f"[segment_token_policy_decision_rebase] Safe rebases: {counts['safe_rebase_same_token_signature']}")
    print(f"[segment_token_policy_decision_rebase] Applied rebases: {applied}")
    print(f"[segment_token_policy_decision_rebase] No approved prior decision: {counts['no_approved_token_policy_decision']}")
    print(
        "[segment_token_policy_decision_rebase] Confirmed text changed: "
        f"{counts['do_not_rebase_confirmed_text_changed']}"
    )
    print(f"[segment_token_policy_decision_rebase] Report: {report_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Safely rebase approved token-policy decisions to the latest output hashes.")
    parser.add_argument("--state-run-id", type=int, default=None)
    parser.add_argument("--policy-run-id", type=int, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reviewer", default=REVIEWER)
    args = parser.parse_args()
    main(
        state_run_id=args.state_run_id,
        policy_run_id=args.policy_run_id,
        apply=args.apply,
        reviewer=args.reviewer,
    )
