from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "auto_confirmation_reopen_text_lifecycle_policy_v1"
POLICY_NAME = "guarded_static_token_only_reopen_closure"
POLICY_ACTION = "close_reopen_static_token_only"
LABEL_FAMILY = "weak_auto_static_token_only"
CHECKPOINT_NAME = "weak_auto_static_token_only_guarded_checkpoint_v1"


def latest_checkpoint_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM auto_confirmation_reopen_text_policy_checkpoint_runs
        WHERE finished_at IS NOT NULL
          AND checkpoint_name = ?
          AND checkpoint_status = 'ready_for_guarded_lifecycle_policy'
          AND promotion_status = 'guarded_candidate'
          AND checkpoint_allowed_count > 0
          AND checkpoint_blocked_count = 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (CHECKPOINT_NAME,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No ready checkpoint found for {CHECKPOINT_NAME!r}.")
    return int(row["id"])


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_auto_confirmation_reopen_text_lifecycle_policy_static_token_only"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_checkpoint(conn, *, checkpoint_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM auto_confirmation_reopen_text_policy_checkpoint_runs
        WHERE id = ?
        """,
        (checkpoint_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Checkpoint run not found: {checkpoint_run_id}")
    return dict(row)


def fetch_rows(conn, *, checkpoint_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.*,
            source.english_text,
            source.spanish_text,
            output.portuguese_text,
            confirmation.confirmation_label,
            confirmation.confirmed_text
        FROM auto_confirmation_reopen_text_policy_checkpoint_items item
        JOIN source_segments source ON source.id = item.segment_id
        LEFT JOIN output_segments output ON output.segment_id = item.segment_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.id = (
              SELECT c.id
              FROM segment_confirmations c
              WHERE c.segment_id = item.segment_id
              ORDER BY c.updated_at DESC, c.id DESC
              LIMIT 1
          )
        WHERE item.checkpoint_run_id = ?
        ORDER BY item.relative_path, item.source_line_number, item.source_key
        """,
        (checkpoint_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def evaluate_checkpoint(checkpoint: dict[str, Any], rows: list[dict[str, Any]], *, policy_status: str) -> list[str]:
    reasons: list[str] = []
    if checkpoint.get("checkpoint_name") != CHECKPOINT_NAME:
        reasons.append("wrong_checkpoint_name")
    if checkpoint.get("checkpoint_status") != "ready_for_guarded_lifecycle_policy":
        reasons.append("checkpoint_not_ready")
    if checkpoint.get("promotion_status") != "guarded_candidate":
        reasons.append("checkpoint_not_guarded_candidate")
    if int(checkpoint.get("checkpoint_blocked_count") or 0) != 0:
        reasons.append("checkpoint_has_blocked_rows")
    if int(checkpoint.get("negative_evidence_count") or 0) != 0:
        reasons.append("checkpoint_has_negative_evidence")
    if int(checkpoint.get("checkpoint_allowed_count") or 0) != len(rows):
        reasons.append("checkpoint_allowed_count_mismatch")
    if policy_status != "active":
        reasons.append("policy_status_not_active")
    return reasons


def evaluate_row(row: dict[str, Any], *, global_reasons: list[str]) -> tuple[int, str]:
    if global_reasons:
        return 0, "global_gate:" + ",".join(global_reasons)
    if int(row.get("checkpoint_allowed") or 0) != 1:
        return 0, row.get("block_reason") or "checkpoint_item_not_allowed"
    if row.get("checkpoint_action") != "guarded_lifecycle_candidate_static_token_only":
        return 0, "wrong_checkpoint_action"
    if row.get("shadow_status") != "shadow_ready":
        return 0, "shadow_not_ready"
    if int(row.get("positive_evidence") or 0) != 1:
        return 0, "missing_positive_evidence"
    if int(row.get("negative_evidence") or 0) != 0:
        return 0, "negative_evidence"
    if int(row.get("issue_count") or 0) != 0:
        return 0, "issue_signal_present"
    return 1, ""


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    policy_run_id: int,
    checkpoint: dict[str, Any],
    rows: list[dict[str, Any]],
    started_at: datetime,
    policy_status: str,
    global_reasons: list[str],
) -> None:
    fieldnames = [
        "policy_item_id",
        "checkpoint_item_id",
        "shadow_policy_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "label_family",
        "confirmation_label",
        "policy_action",
        "policy_allowed",
        "block_reason",
        "issue_count",
        "positive_evidence",
        "negative_evidence",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                **{field: row.get(field) for field in fieldnames},
                "english_preview": short(row.get("english_text")),
                "spanish_preview": short(row.get("spanish_text")),
                "output_preview": short(row.get("portuguese_text")),
                "confirmed_preview": short(row.get("confirmed_text")),
                "checkpoint_run_id": checkpoint["id"],
                "shadow_policy_run_id": checkpoint["shadow_policy_run_id"],
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    counts = Counter("released" if row["policy_allowed"] else row["block_reason"] for row in rows)
    lines = [
        "Auto-confirmation text lifecycle policy",
        f"Rule version: {RULE_VERSION}",
        f"Policy name: {POLICY_NAME}",
        f"Policy action: {POLICY_ACTION}",
        f"Policy status: {policy_status}",
        f"Policy run id: {policy_run_id}",
        f"Checkpoint run id: {checkpoint['id']}",
        f"Checkpoint name: {checkpoint['checkpoint_name']}",
        f"Checkpoint status: {checkpoint['checkpoint_status']}",
        f"Checkpoint allowed: {int(checkpoint['checkpoint_allowed_count'] or 0):,}",
        f"Checkpoint blocked: {int(checkpoint['checkpoint_blocked_count'] or 0):,}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        "",
        "Global blockers:",
        *([f"- {reason}" for reason in global_reasons] or ["- none"]),
        "",
        "Released sample:",
    ]
    released = [row for row in rows if row["policy_allowed"]]
    for row in released[:20]:
        lines.extend(
            [
                f"- {row['relative_path']}:{row['source_line_number']}:{row['source_key']}",
                f"  action={row['policy_action']}; checkpoint_item={row['checkpoint_item_id']}",
            ]
        )
    if not released:
        lines.append("- none")
    lines.extend(["", "Blocked sample:"])
    blocked = [row for row in rows if not row["policy_allowed"]]
    for row in blocked[:30]:
        lines.extend(
            [
                f"- {row['block_reason']} | {row['relative_path']}:{row['source_line_number']}:{row['source_key']}",
                f"  checkpoint_allowed={row.get('checkpoint_allowed')}; shadow={row.get('shadow_status')}",
            ]
        )
    if not blocked:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- This policy only authorizes segment-state lifecycle closure for the strict static-token-only checkpoint.",
            "- It does not write output files and does not alter segment confirmations.",
            "- It does not cover embedded literal tokens, source-visible semantic deltas, or ES custom-localization helpers.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    checkpoint_run_id: int | None = None,
    policy_status: str = "active",
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_checkpoint_run_id = checkpoint_run_id or latest_checkpoint_run_id(conn)
        checkpoint = fetch_checkpoint(conn, checkpoint_run_id=selected_checkpoint_run_id)
        rows = fetch_rows(conn, checkpoint_run_id=selected_checkpoint_run_id)
        if not rows:
            raise RuntimeError(f"Checkpoint run {selected_checkpoint_run_id} has no items.")

        global_reasons = evaluate_checkpoint(checkpoint, rows, policy_status=policy_status)
        for row in rows:
            allowed, block_reason = evaluate_row(row, global_reasons=global_reasons)
            row["policy_action"] = POLICY_ACTION
            row["policy_allowed"] = allowed
            row["block_reason"] = block_reason
            row["label_family"] = LABEL_FAMILY
            row["checkpoint_item_id"] = row["id"]

        counts = Counter("released" if row["policy_allowed"] else row["block_reason"] for row in rows)
        txt_path, csv_path, jsonl_path = report_paths(settings)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_lifecycle_policy_runs (
                rule_version,
                queue_run_id,
                audit_run_id,
                policy_name,
                label_family,
                policy_status,
                candidate_count,
                released_count,
                blocked_count,
                manual_boundary_count,
                invalid_count,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                None,
                None,
                POLICY_NAME,
                LABEL_FAMILY,
                policy_status,
                len(rows),
                counts["released"],
                len(rows) - counts["released"],
                0,
                sum(value for key, value in counts.items() if key != "released"),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at.isoformat(timespec="seconds"),
                now,
                now,
            ),
        )
        policy_run_id = int(cursor.lastrowid)
        for row in rows:
            item_cursor = conn.execute(
                """
                INSERT INTO auto_confirmation_reopen_lifecycle_policy_items (
                    run_id,
                    queue_run_id,
                    queue_item_id,
                    audit_run_id,
                    audit_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    label_family,
                    confirmation_label,
                    policy_action,
                    policy_allowed,
                    block_reason,
                    output_match_kind,
                    token_status,
                    issue_count,
                    high_issue_count,
                    model_safe_probability,
                    review_priority,
                    reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    policy_run_id,
                    None,
                    None,
                    None,
                    None,
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    LABEL_FAMILY,
                    row.get("confirmation_label"),
                    row["policy_action"],
                    int(row["policy_allowed"]),
                    row["block_reason"],
                    "checkpoint_static_token_only",
                    "ok",
                    int(row.get("issue_count") or 0),
                    0,
                    None,
                    0.0,
                    json.dumps(
                        {
                            "checkpoint_run_id": selected_checkpoint_run_id,
                            "checkpoint_item_id": row["checkpoint_item_id"],
                            "shadow_policy_run_id": checkpoint["shadow_policy_run_id"],
                            "shadow_policy_item_id": row["shadow_policy_item_id"],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            row["policy_item_id"] = int(item_cursor.lastrowid)

        if policy_status == "active" and counts["released"] == len(rows) and not global_reasons:
            conn.execute(
                """
                UPDATE auto_confirmation_reopen_text_policy_checkpoint_runs
                SET production_release_allowed = 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, selected_checkpoint_run_id),
            )

        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            policy_run_id=policy_run_id,
            checkpoint=checkpoint,
            rows=rows,
            started_at=started_at,
            policy_status=policy_status,
            global_reasons=global_reasons,
        )
        conn.commit()

    print("[auto_confirmation_reopen_text_lifecycle_policy] Policy generated")
    print(f"[auto_confirmation_reopen_text_lifecycle_policy] Run id: {policy_run_id}")
    print(f"[auto_confirmation_reopen_text_lifecycle_policy] Checkpoint run id: {selected_checkpoint_run_id}")
    print(f"[auto_confirmation_reopen_text_lifecycle_policy] Status: {policy_status}")
    print(f"[auto_confirmation_reopen_text_lifecycle_policy] Released: {counts['released']:,}")
    print(f"[auto_confirmation_reopen_text_lifecycle_policy] Blocked: {len(rows) - counts['released']:,}")
    print(f"[auto_confirmation_reopen_text_lifecycle_policy] Report: {txt_path}")
    print(f"[auto_confirmation_reopen_text_lifecycle_policy] CSV: {csv_path}")
    print(f"[auto_confirmation_reopen_text_lifecycle_policy] JSONL: {jsonl_path}")
    return {
        "run_id": policy_run_id,
        "checkpoint_run_id": selected_checkpoint_run_id,
        "policy_status": policy_status,
        "released": counts["released"],
        "blocked": len(rows) - counts["released"],
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create guarded lifecycle policy from the static-token-only checkpoint.")
    parser.add_argument("--checkpoint-run-id", type=int, default=None)
    parser.add_argument("--status", choices=["shadow", "active"], default="active")
    args = parser.parse_args()
    main(checkpoint_run_id=args.checkpoint_run_id, policy_status=args.status)
