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


RULE_VERSION = "auto_confirmation_reopen_text_boundary_repair_checkpoint_v1"
POLICY_NAME = "weak_auto_same_token_boundary_repair_shadow_v1"
CHECKPOINT_NAME = "weak_auto_same_token_boundary_repair_checkpoint_v1"
CHECKPOINT_ACTION = "stage_same_token_boundary_repair_shadow"
NOOP_CHECKPOINT_ACTION = "observe_same_token_noop_shadow"


def latest_repair_shadow_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM auto_confirmation_reopen_text_boundary_repair_shadow_runs
        WHERE finished_at IS NOT NULL
          AND policy_name = ?
          AND shadow_ready_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (POLICY_NAME,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No completed repair shadow run found for {POLICY_NAME!r}.")
    return int(row["id"])


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_auto_confirmation_reopen_text_boundary_repair_checkpoint"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_shadow_run(conn, *, repair_shadow_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM auto_confirmation_reopen_text_boundary_repair_shadow_runs
        WHERE id = ?
        """,
        (repair_shadow_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Repair shadow run not found: {repair_shadow_run_id}")
    return dict(row)


def fetch_rows(conn, *, repair_shadow_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.*,
            decision.corrected_text,
            confirmation.confirmed_text
        FROM auto_confirmation_reopen_text_boundary_repair_shadow_items item
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
        WHERE item.run_id = ?
        ORDER BY item.relative_path, item.source_line_number, item.source_key
        """,
        (repair_shadow_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def evaluate_global(
    shadow_run: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    min_ready_required: int,
    max_blocked_allowed: int,
) -> list[str]:
    reasons: list[str] = []
    if shadow_run.get("policy_name") != POLICY_NAME:
        reasons.append("wrong_policy_name")
    if shadow_run.get("policy_status") != "shadow":
        reasons.append("shadow_run_not_shadow")
    if int(shadow_run.get("total_candidates") or 0) != len(rows):
        reasons.append("shadow_item_count_mismatch")
    if int(shadow_run.get("shadow_ready_count") or 0) < min_ready_required:
        reasons.append("min_ready_not_met")
    if int(shadow_run.get("blocked_count") or 0) > max_blocked_allowed:
        reasons.append("too_many_shadow_blocks")
    if int(shadow_run.get("validation_issue_count") or 0) > max_blocked_allowed:
        reasons.append("too_many_validation_issues")
    return reasons


def row_block_reason(row: dict[str, Any]) -> str:
    if row.get("shadow_status") != "shadow_ready":
        return row.get("block_reason") or row.get("shadow_status") or "shadow_not_ready"
    shadow_action = row.get("shadow_action")
    if shadow_action not in {
        "would_stage_same_token_repair_shadow",
        "would_observe_same_token_noop_shadow",
    }:
        return "wrong_shadow_action"
    if row.get("repair_route") != "same_token_shadow_repair":
        return "wrong_repair_route"
    if row.get("token_status") != "same_structural_tokens":
        return "token_status_not_same_structural"
    if int(row.get("validation_issue_count") or 0) != 0:
        return "validation_issue_present"
    if not row.get("current_confirmed_text_hash") or not row.get("corrected_text_hash"):
        return "missing_text_hash"
    same_hash = row.get("current_confirmed_text_hash") == row.get("corrected_text_hash")
    if shadow_action == "would_stage_same_token_repair_shadow" and same_hash:
        return "no_text_delta"
    if shadow_action == "would_observe_same_token_noop_shadow" and not same_hash:
        return "unexpected_text_delta_for_noop"
    return ""


def checkpoint_action_for(row: dict[str, Any]) -> str:
    if row.get("shadow_action") == "would_observe_same_token_noop_shadow":
        return NOOP_CHECKPOINT_ACTION
    return CHECKPOINT_ACTION


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    shadow_run: dict[str, Any],
    rows: list[dict[str, Any]],
    started_at: datetime,
    global_reasons: list[str],
    checkpoint_status: str,
    promotion_status: str,
) -> None:
    fieldnames = [
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
        "shadow_status",
        "checkpoint_action",
        "checkpoint_allowed",
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

    counts = Counter("checkpoint_allowed" if row["checkpoint_allowed"] else row["block_reason"] for row in rows)
    policies = Counter(row["boundary_policy"] for row in rows if row["checkpoint_allowed"])
    lines = [
        "Auto-confirmation boundary repair checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint name: {CHECKPOINT_NAME}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Checkpoint status: {checkpoint_status}",
        f"Promotion status: {promotion_status}",
        f"Policy name: {POLICY_NAME}",
        f"Repair shadow run id: {shadow_run['id']}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Governance gates:",
        f"- Shadow total: {int(shadow_run['total_candidates'] or 0):,}",
        f"- Shadow ready: {int(shadow_run['shadow_ready_count'] or 0):,}",
        f"- Shadow blocked: {int(shadow_run['blocked_count'] or 0):,}",
        f"- No text delta: {int(shadow_run['no_text_delta_count'] or 0):,}",
        f"- Validation issue rows: {int(shadow_run['validation_issue_count'] or 0):,}",
        "",
        "Checkpoint items:",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        "",
        "Allowed by boundary policy:",
        *[f"- {key}: {value:,}" for key, value in policies.most_common()],
        "",
        "Global blockers:",
        *([f"- {reason}" for reason in global_reasons] or ["- none"]),
        "",
        "Allowed sample:",
    ]
    for row in [item for item in rows if item["checkpoint_allowed"]][:25]:
        lines.extend(
            [
                f"- {row['relative_path']}:{row['source_line_number']}:{row['source_key']} | {row['boundary_policy']}",
                f"  corrected={short(row.get('corrected_text'))}",
            ]
        )
    if not any(item["checkpoint_allowed"] for item in rows):
        lines.append("- none")
    lines.extend(["", "Blocked sample:"])
    for row in [item for item in rows if not item["checkpoint_allowed"]][:25]:
        lines.extend(
            [
                f"- {row['block_reason']} | {row['relative_path']}:{row['source_line_number']}:{row['source_key']}",
                f"  corrected={short(row.get('corrected_text'))}",
            ]
        )
    if all(item["checkpoint_allowed"] for item in rows):
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Checkpoint only: no output writes, no confirmation updates, and no production release.",
            "- Only shadow_ready same-token repair rows may pass this checkpoint.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    repair_shadow_run_id: int | None = None,
    min_ready_required: int = 1,
    max_blocked_allowed: int = 2,
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_shadow_run_id = repair_shadow_run_id or latest_repair_shadow_run_id(conn)
        shadow_run = fetch_shadow_run(conn, repair_shadow_run_id=selected_shadow_run_id)
        rows = fetch_rows(conn, repair_shadow_run_id=selected_shadow_run_id)
        if not rows:
            raise RuntimeError(f"Repair shadow run {selected_shadow_run_id} has no items.")

        global_reasons = evaluate_global(
            shadow_run,
            rows,
            min_ready_required=min_ready_required,
            max_blocked_allowed=max_blocked_allowed,
        )
        for row in rows:
            block_reason = row_block_reason(row)
            if global_reasons and not block_reason:
                block_reason = "global_gate:" + ",".join(global_reasons)
            row["checkpoint_action"] = checkpoint_action_for(row)
            row["checkpoint_allowed"] = 0 if block_reason else 1
            row["block_reason"] = block_reason

        allowed_count = sum(int(row["checkpoint_allowed"]) for row in rows)
        blocked_count = len(rows) - allowed_count
        checkpoint_status = (
            "ready_for_shadow_lifecycle_policy"
            if allowed_count > 0 and not global_reasons
            else "blocked_by_checkpoint_guard"
        )
        promotion_status = "shadow_candidate" if checkpoint_status == "ready_for_shadow_lifecycle_policy" else "blocked"
        txt_path, csv_path, jsonl_path = report_paths(settings)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_text_boundary_repair_checkpoint_runs (
                rule_version,
                repair_shadow_run_id,
                checkpoint_name,
                checkpoint_status,
                policy_name,
                policy_status,
                min_ready_required,
                max_blocked_allowed,
                total_candidates,
                ready_count,
                blocked_count,
                checkpoint_allowed_count,
                checkpoint_blocked_count,
                no_text_delta_count,
                validation_issue_count,
                promotion_status,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_shadow_run_id,
                CHECKPOINT_NAME,
                checkpoint_status,
                POLICY_NAME,
                "shadow",
                min_ready_required,
                max_blocked_allowed,
                len(rows),
                int(shadow_run.get("shadow_ready_count") or 0),
                int(shadow_run.get("blocked_count") or 0),
                allowed_count,
                blocked_count,
                int(shadow_run.get("no_text_delta_count") or 0),
                int(shadow_run.get("validation_issue_count") or 0),
                promotion_status,
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at.isoformat(timespec="seconds"),
                now,
                now,
            ),
        )
        checkpoint_run_id = int(cursor.lastrowid)
        for row in rows:
            item_cursor = conn.execute(
                """
                INSERT INTO auto_confirmation_reopen_text_boundary_repair_checkpoint_items (
                    checkpoint_run_id,
                    repair_shadow_run_id,
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
                    repair_route,
                    shadow_status,
                    checkpoint_action,
                    checkpoint_allowed,
                    block_reason,
                    current_confirmed_text_hash,
                    corrected_text_hash,
                    reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_run_id,
                    selected_shadow_run_id,
                    row["id"],
                    row["repair_queue_item_id"],
                    row["boundary_policy_item_id"],
                    row["review_decision_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    row["boundary_agent_key"],
                    row["boundary_policy"],
                    row["repair_route"],
                    row["shadow_status"],
                    row["checkpoint_action"],
                    int(row["checkpoint_allowed"]),
                    row["block_reason"],
                    row.get("current_confirmed_text_hash"),
                    row.get("corrected_text_hash"),
                    row.get("reasons_json"),
                    now,
                ),
            )
            row["checkpoint_item_id"] = int(item_cursor.lastrowid)

        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            checkpoint_run_id=checkpoint_run_id,
            shadow_run=shadow_run,
            rows=rows,
            started_at=started_at,
            global_reasons=global_reasons,
            checkpoint_status=checkpoint_status,
            promotion_status=promotion_status,
        )
        conn.commit()

    print("[auto_confirmation_reopen_text_boundary_repair_checkpoint] Checkpoint generated")
    print(f"[auto_confirmation_reopen_text_boundary_repair_checkpoint] Checkpoint run id: {checkpoint_run_id}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_checkpoint] Repair shadow run id: {selected_shadow_run_id}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_checkpoint] Status: {checkpoint_status}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_checkpoint] Allowed: {allowed_count:,}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_checkpoint] Blocked: {blocked_count:,}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_checkpoint] Report: {txt_path}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_checkpoint] CSV: {csv_path}")
    print(f"[auto_confirmation_reopen_text_boundary_repair_checkpoint] JSONL: {jsonl_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "repair_shadow_run_id": selected_shadow_run_id,
        "checkpoint_status": checkpoint_status,
        "promotion_status": promotion_status,
        "allowed": allowed_count,
        "blocked": blocked_count,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a governed checkpoint for same-token boundary repair shadow rows.")
    parser.add_argument("--repair-shadow-run-id", type=int, default=None)
    parser.add_argument("--min-ready", type=int, default=1)
    parser.add_argument("--max-blocked", type=int, default=2)
    args = parser.parse_args()
    main(
        repair_shadow_run_id=args.repair_shadow_run_id,
        min_ready_required=args.min_ready,
        max_blocked_allowed=args.max_blocked,
    )
