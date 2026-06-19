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
from apply_segment_state_updates import short


RULE_VERSION = "issue_review_gender_boundary_lifecycle_policy_v1"
POLICY_NAME = "gender_boundary_shadow_lifecycle_v1"
POLICY_ACTION = "observe_gender_boundary_guard_shadow"
CHECKPOINT_NAME = "gender_subpolicy_boundary_observation_checkpoint_v1"

ALLOWED_CHECKPOINT_ACTIONS = {
    "observe_gender_token_mismatch_boundary",
    "observe_gender_token_extra_prefix_boundary",
    "observe_select_cstring_residual_spanish_boundary",
    "observe_visible_gender_spanish_residual_boundary",
    "observe_gender_surface_boundary",
}


def stable_hash(value: str | None) -> str:
    return hashlib.sha1((value or "").encode("utf-8")).hexdigest()


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_gender_boundary_lifecycle_policy"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_checkpoint_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_gender_boundary_checkpoint_runs
        WHERE finished_at IS NOT NULL
          AND checkpoint_name = ?
          AND checkpoint_status = 'ready_for_boundary_observation'
          AND promotion_status = 'boundary_observation_candidate'
          AND checkpoint_allowed_count > 0
          AND checkpoint_blocked_count = 0
          AND production_release_allowed = 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (CHECKPOINT_NAME,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No ready gender boundary checkpoint found for {CHECKPOINT_NAME!r}.")
    return int(row["id"])


def fetch_checkpoint(conn, *, checkpoint_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_gender_boundary_checkpoint_runs
        WHERE id = ?
        """,
        (checkpoint_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Gender boundary checkpoint not found: {checkpoint_run_id}")
    return dict(row)


def fetch_rows(conn, *, checkpoint_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.*,
            confirmation.confirmed_text AS current_confirmed_text,
            confirmation.locked AS current_confirmation_locked
        FROM ml_issue_gender_boundary_checkpoint_items item
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.id = (
              SELECT c.id
              FROM segment_confirmations c
              WHERE c.segment_id = item.segment_id
              ORDER BY c.updated_at DESC, c.id DESC
              LIMIT 1
          )
        WHERE item.checkpoint_run_id = ?
        ORDER BY item.checkpoint_allowed DESC, item.subpolicy_name, item.relative_path, item.source_line_number, item.source_key
        """,
        (checkpoint_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def global_block_reasons(
    checkpoint: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    policy_status: str,
    expected_total: int | None,
) -> list[str]:
    reasons: list[str] = []
    if policy_status != "shadow":
        reasons.append("policy_status_must_remain_shadow")
    if checkpoint.get("checkpoint_name") != CHECKPOINT_NAME:
        reasons.append("wrong_checkpoint_name")
    if checkpoint.get("checkpoint_status") != "ready_for_boundary_observation":
        reasons.append("checkpoint_not_ready_for_observation")
    if checkpoint.get("promotion_status") != "boundary_observation_candidate":
        reasons.append("checkpoint_not_boundary_observation_candidate")
    if int(checkpoint.get("production_release_allowed") or 0) != 0:
        reasons.append("checkpoint_must_not_allow_production")
    if int(checkpoint.get("checkpoint_blocked_count") or 0) != 0:
        reasons.append("checkpoint_has_blocked_rows")
    if int(checkpoint.get("checkpoint_allowed_count") or 0) <= 0:
        reasons.append("no_allowed_checkpoint_items")
    if int(checkpoint.get("checkpoint_allowed_count") or 0) != sum(
        1 for row in rows if int(row.get("checkpoint_allowed") or 0) == 1
    ):
        reasons.append("checkpoint_allowed_count_mismatch")
    if expected_total is not None and len(rows) != expected_total:
        reasons.append("expected_total_mismatch")
    if len({int(row["id"]) for row in rows}) != len(rows):
        reasons.append("duplicate_checkpoint_items")
    if not rows:
        reasons.append("no_lifecycle_items")
    return reasons


def evaluate_row(row: dict[str, Any], *, global_reasons: list[str]) -> tuple[int, str, dict[str, Any]]:
    current_hash = stable_hash(row.get("current_confirmed_text"))
    reasons = {
        "checkpoint_item_id": int(row["id"]),
        "subpolicy_name": row.get("subpolicy_name") or "",
        "checkpoint_confirmed_text_hash": row.get("current_confirmed_text_hash") or "",
        "current_confirmed_text_hash": current_hash,
        "checkpoint_action": row.get("checkpoint_action") or "",
    }
    if global_reasons:
        return 0, "global_gate:" + ",".join(global_reasons), reasons
    if int(row.get("checkpoint_allowed") or 0) != 1:
        return 0, row.get("block_reason") or "checkpoint_item_not_allowed", reasons
    if row.get("checkpoint_action") not in ALLOWED_CHECKPOINT_ACTIONS:
        return 0, "wrong_checkpoint_action", reasons
    if row.get("block_reason"):
        return 0, "checkpoint_item_has_block_reason", reasons
    if current_hash != (row.get("current_confirmed_text_hash") or ""):
        return 0, "stale_confirmation_hash_changed", reasons
    if int(row.get("current_confirmation_locked") or 0):
        reasons["current_confirmation_locked"] = 1
    return 1, "", reasons


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
        "checkpoint_run_id",
        "checkpoint_item_id",
        "shadow_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "issue_kind",
        "normalized_decision",
        "subpolicy_name",
        "checkpoint_action",
        "policy_action",
        "policy_allowed",
        "block_reason",
        "text_length",
        "token_count",
        "word_count",
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
                "current_confirmed_preview": short(row.get("current_confirmed_text")),
                "reasons": row.get("policy_reasons") or {},
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    counts = Counter("released_shadow" if row["policy_allowed"] else row["block_reason"] for row in rows)
    by_subpolicy = Counter(row["subpolicy_name"] for row in rows if row["policy_allowed"])
    by_action = Counter(row["checkpoint_action"] for row in rows if row["policy_allowed"])
    lines = [
        "Issue-review gender boundary lifecycle policy",
        f"Rule version: {RULE_VERSION}",
        f"Policy name: {POLICY_NAME}",
        f"Policy action: {POLICY_ACTION}",
        f"Policy status: {policy_status}",
        f"Policy run id: {policy_run_id}",
        f"Checkpoint run id: {checkpoint['id']}",
        f"Checkpoint allowed: {int(checkpoint['checkpoint_allowed_count'] or 0):,}",
        f"Checkpoint blocked: {int(checkpoint['checkpoint_blocked_count'] or 0):,}",
        f"Production release allowed: 0",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        f"- By subpolicy: {json.dumps(dict(by_subpolicy), ensure_ascii=False, sort_keys=True)}",
        f"- By action: {json.dumps(dict(by_action), ensure_ascii=False, sort_keys=True)}",
        "",
        "Global blockers:",
        *([f"- {reason}" for reason in global_reasons] or ["- none"]),
        "",
        "Shadow monitored sample:",
    ]
    for row in [item for item in rows if item["policy_allowed"]][:30]:
        lines.append(
            f"- {row['subpolicy_name']} | {row['relative_path']}:{row['source_line_number']}:{row['source_key']}"
        )
    if not any(item["policy_allowed"] for item in rows):
        lines.append("- none")
    blocked = [item for item in rows if not item["policy_allowed"]]
    lines.extend(["", "Blocked sample:"])
    if blocked:
        for row in blocked[:30]:
            lines.append(f"- {row['block_reason']} | {row['relative_path']}:{row['source_line_number']}:{row['source_key']}")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Shadow lifecycle only: no output writes, no confirmation updates, no segment-state closure.",
            "- This records gender-boundary guards as monitored network knowledge.",
            "- Production release remains disabled until a separate production-side policy explicitly consumes this evidence.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    checkpoint_run_id: int | None = None,
    policy_status: str = "shadow",
    expected_total: int | None = None,
) -> dict[str, Any]:
    if policy_status != "shadow":
        raise ValueError("Gender boundary lifecycle is intentionally shadow-only for now.")
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_checkpoint_run_id = checkpoint_run_id or latest_checkpoint_run_id(conn)
        checkpoint = fetch_checkpoint(conn, checkpoint_run_id=selected_checkpoint_run_id)
        rows = fetch_rows(conn, checkpoint_run_id=selected_checkpoint_run_id)
        if not rows:
            raise RuntimeError(f"Checkpoint run {selected_checkpoint_run_id} has no items.")

        reasons = global_block_reasons(
            checkpoint,
            rows,
            policy_status=policy_status,
            expected_total=expected_total,
        )
        for row in rows:
            allowed, block_reason, policy_reasons = evaluate_row(row, global_reasons=reasons)
            row["checkpoint_item_id"] = row["id"]
            row["policy_action"] = POLICY_ACTION
            row["policy_allowed"] = allowed
            row["block_reason"] = block_reason
            row["policy_reasons"] = policy_reasons

        counts = Counter("released_shadow" if row["policy_allowed"] else row["block_reason"] for row in rows)
        subpolicy_counts = Counter(row["subpolicy_name"] for row in rows if row["policy_allowed"])
        action_counts = Counter(row["checkpoint_action"] for row in rows if row["policy_allowed"])
        txt_path, csv_path, jsonl_path = report_paths(settings)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_gender_boundary_lifecycle_runs (
                rule_version,
                checkpoint_run_id,
                policy_name,
                policy_status,
                policy_action,
                candidate_count,
                released_count,
                blocked_count,
                subpolicy_count,
                production_release_allowed,
                blocker_counts_json,
                subpolicy_counts_json,
                action_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                len(subpolicy_counts),
                0,
                json.dumps(dict(counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(subpolicy_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(action_counts), ensure_ascii=False, sort_keys=True),
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
                INSERT INTO ml_issue_gender_boundary_lifecycle_items (
                    run_id,
                    checkpoint_run_id,
                    checkpoint_item_id,
                    shadow_run_id,
                    shadow_item_id,
                    decision_id,
                    queue_item_id,
                    ledger_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    agent_key,
                    queue_bucket,
                    issue_family,
                    issue_kind,
                    normalized_decision,
                    evidence_label,
                    subpolicy_name,
                    checkpoint_action,
                    policy_action,
                    policy_allowed,
                    block_reason,
                    token_impact,
                    token_status,
                    text_length,
                    token_count,
                    word_count,
                    issue_codes_json,
                    checkpoint_confirmed_text_hash,
                    current_confirmed_text_hash,
                    reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    policy_run_id,
                    selected_checkpoint_run_id,
                    row["checkpoint_item_id"],
                    row["shadow_run_id"],
                    row["shadow_item_id"],
                    row["decision_id"],
                    row["queue_item_id"],
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    row["agent_key"],
                    row["queue_bucket"],
                    row["issue_family"],
                    row["issue_kind"],
                    row["normalized_decision"],
                    row["evidence_label"],
                    row["subpolicy_name"],
                    row["checkpoint_action"],
                    row["policy_action"],
                    int(row["policy_allowed"]),
                    row["block_reason"],
                    row.get("token_impact"),
                    row.get("token_status"),
                    int(row.get("text_length") or 0),
                    int(row.get("token_count") or 0),
                    int(row.get("word_count") or 0),
                    row.get("issue_codes_json"),
                    row.get("current_confirmed_text_hash"),
                    row["policy_reasons"]["current_confirmed_text_hash"],
                    json.dumps(row["policy_reasons"], ensure_ascii=False, sort_keys=True),
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

    print("[issue_review_gender_boundary_lifecycle_policy] Policy generated")
    print(f"[issue_review_gender_boundary_lifecycle_policy] Run id: {policy_run_id}")
    print(f"[issue_review_gender_boundary_lifecycle_policy] Checkpoint run id: {selected_checkpoint_run_id}")
    print(f"[issue_review_gender_boundary_lifecycle_policy] Status: {policy_status}")
    print(f"[issue_review_gender_boundary_lifecycle_policy] Released shadow: {counts['released_shadow']:,}")
    print(f"[issue_review_gender_boundary_lifecycle_policy] Blocked: {len(rows) - counts['released_shadow']:,}")
    print(f"[issue_review_gender_boundary_lifecycle_policy] Report: {txt_path}")
    print(f"[issue_review_gender_boundary_lifecycle_policy] CSV: {csv_path}")
    print(f"[issue_review_gender_boundary_lifecycle_policy] JSONL: {jsonl_path}")
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
    parser = argparse.ArgumentParser(description="Create a shadow lifecycle policy for gender boundary guards.")
    parser.add_argument("--checkpoint-run-id", type=int, default=None)
    parser.add_argument("--status", choices=["shadow"], default="shadow")
    parser.add_argument("--expected-total", type=int, default=None)
    args = parser.parse_args()
    main(
        checkpoint_run_id=args.checkpoint_run_id,
        policy_status=args.status,
        expected_total=args.expected_total,
    )
