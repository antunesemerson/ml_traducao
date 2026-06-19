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


RULE_VERSION = "auto_confirmation_reopen_text_boundary_repair_lifecycle_policy_v1"
POLICY_NAME = "weak_auto_same_token_boundary_repair_shadow_lifecycle_v1"
POLICY_ACTION = "observe_same_token_boundary_repair_shadow"
CHECKPOINT_NAME = "weak_auto_same_token_boundary_repair_checkpoint_v1"
ALLOWED_CHECKPOINT_ACTIONS = {
    "stage_same_token_boundary_repair_shadow",
    "observe_same_token_noop_shadow",
}


def latest_checkpoint_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM auto_confirmation_reopen_text_boundary_repair_checkpoint_runs
        WHERE finished_at IS NOT NULL
          AND checkpoint_name = ?
          AND checkpoint_status = 'ready_for_shadow_lifecycle_policy'
          AND promotion_status = 'shadow_candidate'
          AND checkpoint_allowed_count > 0
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
    base = reports_dir / f"{stamp}_auto_confirmation_reopen_text_boundary_repair_lifecycle_policy"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_checkpoint(conn, *, checkpoint_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM auto_confirmation_reopen_text_boundary_repair_checkpoint_runs
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
            decision.corrected_text,
            confirmation.confirmed_text
        FROM auto_confirmation_reopen_text_boundary_repair_checkpoint_items item
        JOIN auto_confirmation_reopen_text_review_decisions decision
          ON decision.id = item.review_decision_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.id = (
              SELECT c.id
              FROM segment_confirmations c
              WHERE c.segment_id = item.segment_id
              ORDER BY c.updated_at DESC, c.id DESC
              LIMIT 1
          )
        WHERE item.checkpoint_run_id = ?
        ORDER BY item.checkpoint_allowed DESC, item.relative_path, item.source_line_number, item.source_key
        """,
        (checkpoint_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def global_block_reasons(checkpoint: dict[str, Any], rows: list[dict[str, Any]], *, policy_status: str) -> list[str]:
    reasons: list[str] = []
    if checkpoint.get("checkpoint_name") != CHECKPOINT_NAME:
        reasons.append("wrong_checkpoint_name")
    if checkpoint.get("checkpoint_status") != "ready_for_shadow_lifecycle_policy":
        reasons.append("checkpoint_not_ready")
    if checkpoint.get("promotion_status") != "shadow_candidate":
        reasons.append("checkpoint_not_shadow_candidate")
    if int(checkpoint.get("checkpoint_allowed_count") or 0) <= 0:
        reasons.append("no_allowed_checkpoint_items")
    if int(checkpoint.get("checkpoint_allowed_count") or 0) != sum(1 for row in rows if int(row.get("checkpoint_allowed") or 0)):
        reasons.append("checkpoint_allowed_count_mismatch")
    if policy_status != "shadow":
        reasons.append("policy_status_must_remain_shadow")
    return reasons


def evaluate_row(row: dict[str, Any], *, global_reasons: list[str]) -> tuple[int, str]:
    if global_reasons:
        return 0, "global_gate:" + ",".join(global_reasons)
    if int(row.get("checkpoint_allowed") or 0) != 1:
        return 0, row.get("block_reason") or "checkpoint_item_not_allowed"
    if row.get("checkpoint_action") not in ALLOWED_CHECKPOINT_ACTIONS:
        return 0, "wrong_checkpoint_action"
    if row.get("shadow_status") != "shadow_ready":
        return 0, "shadow_not_ready"
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
        "repair_shadow_item_id",
        "repair_queue_item_id",
        "boundary_policy_item_id",
        "review_decision_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "boundary_agent_key",
        "boundary_policy",
        "policy_action",
        "policy_allowed",
        "block_reason",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                **{field: row.get(field) for field in fieldnames},
                "confirmed_preview": short(row.get("confirmed_text")),
                "corrected_preview": short(row.get("corrected_text")),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    counts = Counter("released_shadow" if row["policy_allowed"] else row["block_reason"] for row in rows)
    policies = Counter(row["boundary_policy"] for row in rows if row["policy_allowed"])
    lines = [
        "Auto-confirmation boundary repair lifecycle policy",
        f"Rule version: {RULE_VERSION}",
        f"Policy name: {POLICY_NAME}",
        f"Policy action: {POLICY_ACTION}",
        f"Policy status: {policy_status}",
        f"Policy run id: {policy_run_id}",
        f"Checkpoint run id: {checkpoint['id']}",
        f"Checkpoint allowed: {int(checkpoint['checkpoint_allowed_count'] or 0):,}",
        f"Checkpoint blocked: {int(checkpoint['checkpoint_blocked_count'] or 0):,}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        "",
        "Released by boundary policy:",
        *[f"- {key}: {value:,}" for key, value in policies.most_common()],
        "",
        "Global blockers:",
        *([f"- {reason}" for reason in global_reasons] or ["- none"]),
        "",
        "Shadow released sample:",
    ]
    for row in [item for item in rows if item["policy_allowed"]][:25]:
        lines.extend(
            [
                f"- {row['relative_path']}:{row['source_line_number']}:{row['source_key']} | {row['boundary_policy']}",
                f"  corrected={short(row.get('corrected_text'))}",
            ]
        )
    if not any(item["policy_allowed"] for item in rows):
        lines.append("- none")
    lines.extend(["", "Blocked sample:"])
    for row in [item for item in rows if not item["policy_allowed"]][:25]:
        lines.extend(
            [
                f"- {row['block_reason']} | {row['relative_path']}:{row['source_line_number']}:{row['source_key']}",
                f"  corrected={short(row.get('corrected_text'))}",
            ]
        )
    if all(item["policy_allowed"] for item in rows):
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Shadow lifecycle only: no output writes, no confirmation updates, no segment-state closure.",
            "- This records that the repair candidates may be monitored by the learning network.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    checkpoint_run_id: int | None = None,
    policy_status: str = "shadow",
) -> dict[str, Any]:
    if policy_status != "shadow":
        raise ValueError("Boundary repair lifecycle is intentionally shadow-only for now.")
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_checkpoint_run_id = checkpoint_run_id or latest_checkpoint_run_id(conn)
        checkpoint = fetch_checkpoint(conn, checkpoint_run_id=selected_checkpoint_run_id)
        rows = fetch_rows(conn, checkpoint_run_id=selected_checkpoint_run_id)
        if not rows:
            raise RuntimeError(f"Checkpoint run {selected_checkpoint_run_id} has no items.")

        reasons = global_block_reasons(checkpoint, rows, policy_status=policy_status)
        for row in rows:
            allowed, block_reason = evaluate_row(row, global_reasons=reasons)
            row["policy_action"] = POLICY_ACTION
            row["policy_allowed"] = allowed
            row["block_reason"] = block_reason

        counts = Counter("released_shadow" if row["policy_allowed"] else row["block_reason"] for row in rows)
        txt_path, csv_path, jsonl_path = report_paths(settings)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_text_boundary_repair_lifecycle_runs (
                rule_version,
                checkpoint_run_id,
                policy_name,
                policy_status,
                policy_action,
                candidate_count,
                released_count,
                blocked_count,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_checkpoint_run_id,
                POLICY_NAME,
                policy_status,
                POLICY_ACTION,
                len(rows),
                counts["released_shadow"],
                len(rows) - counts["released_shadow"],
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
                INSERT INTO auto_confirmation_reopen_text_boundary_repair_lifecycle_items (
                    run_id,
                    checkpoint_run_id,
                    checkpoint_item_id,
                    repair_shadow_item_id,
                    repair_queue_item_id,
                    boundary_policy_item_id,
                    review_decision_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    boundary_agent_key,
                    boundary_policy,
                    policy_action,
                    policy_allowed,
                    block_reason,
                    current_confirmed_text_hash,
                    corrected_text_hash,
                    reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    policy_run_id,
                    selected_checkpoint_run_id,
                    row["id"],
                    row["repair_shadow_item_id"],
                    row["repair_queue_item_id"],
                    row["boundary_policy_item_id"],
                    row["review_decision_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    row["boundary_agent_key"],
                    row["boundary_policy"],
                    row["policy_action"],
                    int(row["policy_allowed"]),
                    row["block_reason"],
                    row.get("current_confirmed_text_hash"),
                    row.get("corrected_text_hash"),
                    row.get("reasons_json"),
                    now,
                ),
            )
            row["policy_item_id"] = int(item_cursor.lastrowid)

        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            policy_run_id=policy_run_id,
            checkpoint=checkpoint,
            rows=rows,
            started_at=started_at,
            policy_status=policy_status,
            global_reasons=reasons,
        )
        conn.commit()

    print("[auto_confirmation_reopen_text_boundary_repair_lifecycle_policy] Policy generated")
    print(f"[auto_confirmation_reopen_text_boundary_repair_lifecycle_policy] Run id: {policy_run_id}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_lifecycle_policy] Checkpoint run id: {selected_checkpoint_run_id}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_lifecycle_policy] Status: {policy_status}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_lifecycle_policy] Released shadow: {counts['released_shadow']:,}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_lifecycle_policy] Blocked: {len(rows) - counts['released_shadow']:,}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_lifecycle_policy] Report: {txt_path}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_lifecycle_policy] CSV: {csv_path}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_lifecycle_policy] JSONL: {jsonl_path}")
    return {
        "run_id": policy_run_id,
        "checkpoint_run_id": selected_checkpoint_run_id,
        "policy_status": policy_status,
        "released_shadow": counts["released_shadow"],
        "blocked": len(rows) - counts["released_shadow"],
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a shadow lifecycle policy for same-token boundary repair rows.")
    parser.add_argument("--checkpoint-run-id", type=int, default=None)
    parser.add_argument("--status", choices=["shadow"], default="shadow")
    args = parser.parse_args()
    main(checkpoint_run_id=args.checkpoint_run_id, policy_status=args.status)
