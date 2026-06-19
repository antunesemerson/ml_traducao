from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator
from apply_segment_state_updates import short, structural_tokens
from segment_token_mismatch_queue import classify_diff
from segment_token_policy import classify_policy


RULE_VERSION = "auto_confirmation_reopen_text_boundary_token_policy_bridge_v1"
BRIDGE_NAME = "weak_auto_boundary_repair_token_policy_bridge_v1"


def sha256_text(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def latest_token_change_repair_queue_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM auto_confirmation_reopen_text_boundary_repair_queue_runs
        WHERE finished_at IS NOT NULL
          AND queue_scope = 'token-change'
          AND token_change_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No complete token-change boundary repair queue found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_auto_confirmation_reopen_text_boundary_token_policy_bridge"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
    )


def parse_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return [value]
    if isinstance(payload, list):
        return [str(item) for item in payload]
    return [str(payload)]


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


def review_labels_for(row: dict[str, Any]) -> list[str]:
    bucket = row["policy_bucket"]
    if row["bridge_status"].startswith("blocked_"):
        return ["reject_repair_candidate", "fix_corrected_text", "needs_subpolicy"]
    if bucket in {"manual_exception_candidate_select_cstring", "manual_exception_candidate_gender_token"}:
        return ["keep_manual_exception_only", "accept_repair_after_token_policy", "needs_subpolicy"]
    if bucket == "review_gender_token_change":
        return ["accept_repair_after_token_policy", "keep_manual_exception_only", "reject_repair_candidate"]
    if bucket in {"review_select_cstring_change", "review_dynamic_scope_change", "review_mixed_token_change"}:
        return ["keep_manual_exception_only", "reject_repair_candidate", "needs_subpolicy"]
    if bucket == "review_concept_simplification":
        return ["accept_repair_after_token_policy", "keep_manual_exception_only", "reject_repair_candidate"]
    return ["keep_manual_exception_only", "reject_repair_candidate", "needs_subpolicy"]


def fetch_rows(conn, *, repair_queue_run_id: int, limit: int | None) -> list[dict[str, Any]]:
    limit_sql = ""
    params: list[Any] = [repair_queue_run_id]
    if limit is not None:
        limit_sql = "LIMIT ?"
        params.append(limit)
    rows = conn.execute(
        f"""
        SELECT
            item.*,
            decision.corrected_text,
            decision.notes,
            confirmation.confirmed_text,
            confirmation.confirmation_level,
            confirmation.confirmation_source,
            confirmation.confirmation_label,
            confirmation.locked,
            source.english_text,
            source.spanish_text,
            source.old_text
        FROM auto_confirmation_reopen_text_boundary_repair_queue_items item
        JOIN auto_confirmation_reopen_text_review_decisions decision
          ON decision.id = item.review_decision_id
        JOIN source_segments source ON source.id = item.segment_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.id = (
              SELECT c.id
              FROM segment_confirmations c
              WHERE c.segment_id = item.segment_id
              ORDER BY c.updated_at DESC, c.id DESC
              LIMIT 1
          )
        WHERE item.run_id = ?
          AND item.repair_route = 'token_policy_repair_review'
        ORDER BY item.queue_rank, item.id
        {limit_sql}
        """,
        tuple(params),
    ).fetchall()
    return [enrich_row(dict(row)) for row in rows]


def token_delta(confirmed_text: str | None, corrected_text: str | None) -> tuple[list[str], list[str]]:
    confirmed_tokens = structural_tokens(confirmed_text)
    corrected_tokens = structural_tokens(corrected_text)
    missing = sorted((confirmed_tokens - corrected_tokens).elements())
    extra = sorted((corrected_tokens - confirmed_tokens).elements())
    return missing, extra


def enrich_row(row: dict[str, Any]) -> dict[str, Any]:
    confirmed_text = row.get("confirmed_text")
    corrected_text = row.get("corrected_text")
    missing_tokens, extra_tokens = token_delta(confirmed_text, corrected_text)
    diff_kind = classify_diff(missing_tokens, extra_tokens)
    review_state = "human_locked" if int(row.get("locked") or 0) else "boundary_repair_candidate"
    policy_input = {
        **row,
        "review_state": review_state,
        "diff_kind": diff_kind,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "output_text": corrected_text,
        "confirmed_text": confirmed_text,
    }
    policy = classify_policy(policy_input)
    validation_issues = blocking_validation_issues(corrected_text)

    if not (corrected_text or "").strip():
        bridge_status = "blocked_missing_correction"
        bridge_action = "hold_for_correction_text"
    elif not missing_tokens and not extra_tokens:
        bridge_status = "same_token_not_structural"
        bridge_action = "route_to_same_token_shadow"
    elif validation_issues:
        bridge_status = "blocked_quality_issue"
        bridge_action = "hold_for_quality_review"
    else:
        bridge_status = "ready_for_token_policy_review"
        bridge_action = "queue_boundary_repair_token_policy_review"

    return {
        **row,
        "review_state": review_state,
        "diff_kind": diff_kind,
        "missing_tokens": missing_tokens,
        "extra_tokens": extra_tokens,
        "policy_bucket": policy.policy_bucket,
        "risk_level": policy.risk_level,
        "recommendation": policy.recommendation,
        "issue_flags": policy.issue_flags,
        "validation_issues": validation_issues,
        "validation_issue_count": len(validation_issues),
        "bridge_status": bridge_status,
        "bridge_action": bridge_action,
        "boundary_reasons": parse_json_list(row.get("reasons_json")),
        "suggested_review_labels": [],
        "current_confirmed_text_hash": sha256_text(confirmed_text),
        "corrected_text_hash": sha256_text(corrected_text),
    }


def insert_run(
    conn,
    *,
    repair_queue_run_id: int,
    rows: list[dict[str, Any]],
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    decisions_template_path: Path,
    started_at: datetime,
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    statuses = Counter(row["bridge_status"] for row in rows)
    risks = Counter(row["risk_level"] for row in rows)
    token_change_count = sum(1 for row in rows if row["missing_tokens"] or row["extra_tokens"])
    cursor = conn.execute(
        """
        INSERT INTO auto_confirmation_reopen_text_boundary_token_policy_bridge_runs (
            rule_version,
            repair_queue_run_id,
            bridge_name,
            bridge_status,
            total_candidates,
            token_change_count,
            same_token_count,
            review_required_count,
            blocked_count,
            critical_count,
            high_count,
            medium_count,
            low_count,
            report_path,
            csv_path,
            jsonl_path,
            decisions_template_path,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            repair_queue_run_id,
            BRIDGE_NAME,
            "ready_for_review" if statuses["ready_for_token_policy_review"] else "blocked_or_empty",
            len(rows),
            token_change_count,
            len(rows) - token_change_count,
            statuses["ready_for_token_policy_review"],
            len(rows) - statuses["ready_for_token_policy_review"],
            risks["critical"],
            risks["high"],
            risks["medium"],
            risks["low"],
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            str(decisions_template_path),
            started_at.isoformat(timespec="seconds"),
            now,
            now,
        ),
    )
    return int(cursor.lastrowid)


def insert_items(conn, *, run_id: int, repair_queue_run_id: int, rows: list[dict[str, Any]]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    for row in rows:
        cursor = conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_text_boundary_token_policy_bridge_items (
                run_id,
                repair_queue_run_id,
                repair_queue_item_id,
                boundary_policy_item_id,
                review_decision_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                boundary_agent_key,
                boundary_policy,
                bridge_status,
                bridge_action,
                diff_kind,
                policy_bucket,
                risk_level,
                recommendation,
                missing_tokens_json,
                extra_tokens_json,
                issue_flags_json,
                validation_issue_count,
                current_confirmed_text_hash,
                corrected_text_hash,
                reasons_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                repair_queue_run_id,
                row["id"],
                row["boundary_policy_item_id"],
                row["review_decision_id"],
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                row["boundary_agent_key"],
                row["boundary_policy"],
                row["bridge_status"],
                row["bridge_action"],
                row["diff_kind"],
                row["policy_bucket"],
                row["risk_level"],
                row["recommendation"],
                json.dumps(row["missing_tokens"], ensure_ascii=False),
                json.dumps(row["extra_tokens"], ensure_ascii=False),
                json.dumps(row["issue_flags"], ensure_ascii=False),
                row["validation_issue_count"],
                row.get("current_confirmed_text_hash"),
                row.get("corrected_text_hash"),
                row.get("reasons_json"),
                now,
            ),
        )
        row["bridge_item_id"] = int(cursor.lastrowid)
        row["suggested_review_labels"] = review_labels_for(row)


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    decisions_template_path: Path,
    run_id: int,
    repair_queue_run_id: int,
    rows: list[dict[str, Any]],
    started_at: datetime,
) -> None:
    fieldnames = [
        "bridge_item_id",
        "repair_queue_item_id",
        "boundary_policy_item_id",
        "review_decision_id",
        "segment_id",
        "relative_path",
        "source_line_number",
        "source_key",
        "boundary_agent_key",
        "boundary_policy",
        "bridge_status",
        "bridge_action",
        "diff_kind",
        "policy_bucket",
        "risk_level",
        "recommendation",
        "missing_tokens",
        "extra_tokens",
        "issue_flags",
        "validation_issues",
        "suggested_review_labels",
        "confirmed_text",
        "corrected_text",
        "english_text",
        "spanish_text",
        "old_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **{field: row.get(field) for field in fieldnames},
                    "repair_queue_item_id": row["id"],
                    "missing_tokens": json.dumps(row["missing_tokens"], ensure_ascii=False),
                    "extra_tokens": json.dumps(row["extra_tokens"], ensure_ascii=False),
                    "issue_flags": json.dumps(row["issue_flags"], ensure_ascii=False),
                    "validation_issues": json.dumps(row["validation_issues"], ensure_ascii=False),
                    "suggested_review_labels": json.dumps(row["suggested_review_labels"], ensure_ascii=False),
                }
            )

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                "bridge_item_id": row["bridge_item_id"],
                "repair_queue_item_id": row["id"],
                "boundary_policy_item_id": row["boundary_policy_item_id"],
                "review_decision_id": row["review_decision_id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_line_number": row.get("source_line_number"),
                "source_key": row["source_key"],
                "boundary_agent_key": row["boundary_agent_key"],
                "boundary_policy": row["boundary_policy"],
                "bridge_status": row["bridge_status"],
                "bridge_action": row["bridge_action"],
                "diff_kind": row["diff_kind"],
                "policy_bucket": row["policy_bucket"],
                "risk_level": row["risk_level"],
                "recommendation": row["recommendation"],
                "missing_tokens": row["missing_tokens"],
                "extra_tokens": row["extra_tokens"],
                "issue_flags": row["issue_flags"],
                "validation_issues": row["validation_issues"],
                "suggested_review_labels": row["suggested_review_labels"],
                "confirmed_text": row.get("confirmed_text"),
                "corrected_text": row.get("corrected_text"),
                "english_text": row.get("english_text"),
                "spanish_text": row.get("spanish_text"),
                "old_text": row.get("old_text"),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    {
                        "bridge_item_id": row["bridge_item_id"],
                        "repair_queue_item_id": row["id"],
                        "segment_id": row["segment_id"],
                        "relative_path": row["relative_path"],
                        "source_key": row["source_key"],
                        "boundary_policy": row["boundary_policy"],
                        "policy_bucket": row["policy_bucket"],
                        "risk_level": row["risk_level"],
                        "decision": "",
                        "allowed_decisions": row["suggested_review_labels"],
                        "corrected_text": row.get("corrected_text"),
                        "notes": "",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )

    statuses = Counter(row["bridge_status"] for row in rows)
    buckets = Counter(row["policy_bucket"] for row in rows)
    risks = Counter(row["risk_level"] for row in rows)
    policies = Counter(row["boundary_policy"] for row in rows)
    diff_kinds = Counter(row["diff_kind"] for row in rows)

    lines = [
        "Auto-confirmation boundary repair token-policy bridge",
        f"Rule version: {RULE_VERSION}",
        f"Bridge name: {BRIDGE_NAME}",
        f"Bridge run id: {run_id}",
        f"Repair queue run id: {repair_queue_run_id}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        *[f"- {key}: {value:,}" for key, value in statuses.most_common()],
        "",
        "Risk:",
        *[f"- {key}: {value:,}" for key, value in risks.most_common()],
        "",
        "Policy buckets:",
        *[f"- {key}: {value:,}" for key, value in buckets.most_common()],
        "",
        "Boundary policies:",
        *[f"- {key}: {value:,}" for key, value in policies.most_common()],
        "",
        "Diff kinds:",
        *[f"- {key}: {value:,}" for key, value in diff_kinds.most_common()],
        "",
        "Priority sample:",
    ]
    for row in rows[:40]:
        lines.extend(
            [
                (
                    f"- item {row['bridge_item_id']} | segment {row['segment_id']} | "
                    f"{row['relative_path']}:{row['source_line_number']} | {row['source_key']} | "
                    f"{row['risk_level']} | {row['policy_bucket']} | {row['boundary_policy']}"
                ),
                f"  MISSING: {json.dumps(row['missing_tokens'], ensure_ascii=False)}",
                f"  EXTRA: {json.dumps(row['extra_tokens'], ensure_ascii=False)}",
                f"  CONFIRMED: {short(row.get('confirmed_text'))}",
                f"  CORRECTED: {short(row.get('corrected_text'))}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Bridge only: no output writes, no confirmation updates, and no production release.",
            "- These rows must still receive token-policy review before any production apply path can use them.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, repair_queue_run_id: int | None = None, limit: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_repair_queue_run_id = repair_queue_run_id or latest_token_change_repair_queue_run_id(conn)
        rows = fetch_rows(conn, repair_queue_run_id=selected_repair_queue_run_id, limit=limit)
        txt_path, csv_path, jsonl_path, decisions_template_path = report_paths(settings)
        run_id = insert_run(
            conn,
            repair_queue_run_id=selected_repair_queue_run_id,
            rows=rows,
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            decisions_template_path=decisions_template_path,
            started_at=started_at,
        )
        insert_items(conn, run_id=run_id, repair_queue_run_id=selected_repair_queue_run_id, rows=rows)
        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            decisions_template_path=decisions_template_path,
            run_id=run_id,
            repair_queue_run_id=selected_repair_queue_run_id,
            rows=rows,
            started_at=started_at,
        )
        conn.commit()

    counts = Counter(row["bridge_status"] for row in rows)
    print("[auto_confirmation_reopen_text_boundary_token_policy_bridge] Bridge generated")
    print(f"[auto_confirmation_reopen_text_boundary_token_policy_bridge] Run id: {run_id}")
    print(f"[auto_confirmation_reopen_text_boundary_token_policy_bridge] Repair queue run id: {selected_repair_queue_run_id}")
    print(f"[auto_confirmation_reopen_text_boundary_token_policy_bridge] Candidates: {len(rows):,}")
    for key, value in counts.most_common():
        print(f"[auto_confirmation_reopen_text_boundary_token_policy_bridge] {key}: {value:,}")
    print(f"[auto_confirmation_reopen_text_boundary_token_policy_bridge] Report: {txt_path}")
    print(f"[auto_confirmation_reopen_text_boundary_token_policy_bridge] CSV: {csv_path}")
    print(f"[auto_confirmation_reopen_text_boundary_token_policy_bridge] JSONL: {jsonl_path}")
    print(f"[auto_confirmation_reopen_text_boundary_token_policy_bridge] Decisions template: {decisions_template_path}")
    return {
        "run_id": run_id,
        "repair_queue_run_id": selected_repair_queue_run_id,
        "total_candidates": len(rows),
        "counts": dict(counts),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bridge boundary repair token-change candidates into token-policy review.")
    parser.add_argument("--repair-queue-run-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    main(repair_queue_run_id=args.repair_queue_run_id, limit=args.limit)
