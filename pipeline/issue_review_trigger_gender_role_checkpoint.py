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


RULE_VERSION = "issue_review_trigger_gender_role_checkpoint_v1"
POLICY_NAME = "trigger_gender_role_surface_shadow"
CHECKPOINT_NAME = "trigger_gender_role_surface_observation_checkpoint_v1"
AGENT_KEY = "trigger_gender_role_surface"
READY_STATUS = "shadow_ready_observation"
CHECKPOINT_READY_STATUS = "ready_for_trigger_gender_role_observation"

ALLOWED_SHADOW_ACTIONS = {
    "observe_trigger_kinship_pair_form": "checkpoint_trigger_kinship_pair_form",
    "observe_trigger_kinship_lexical_gender": "checkpoint_trigger_kinship_lexical_gender",
    "observe_trigger_kinship_stem_fragment": "checkpoint_trigger_kinship_stem_fragment",
    "observe_trigger_role_article": "checkpoint_trigger_role_article",
}


def stable_hash(value: str | None) -> str:
    return hashlib.sha1((value or "").encode("utf-8")).hexdigest()


def parse_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return sorted({str(item) for item in payload if str(item).strip()})


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_trigger_gender_role_checkpoint"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_shadow_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_trigger_gender_role_surface_runs
        WHERE finished_at IS NOT NULL
          AND policy_name = ?
          AND policy_status = 'shadow'
          AND agent_key = ?
          AND shadow_ready_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (POLICY_NAME, AGENT_KEY),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No completed trigger gender-role shadow run found for {AGENT_KEY!r}.")
    return int(row["id"])


def fetch_shadow_run(conn, *, shadow_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_trigger_gender_role_surface_runs
        WHERE id = ?
        """,
        (shadow_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Trigger gender-role shadow run not found: {shadow_run_id}")
    return dict(row)


def fetch_rows(conn, *, shadow_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.*,
            confirmation.id AS current_confirmation_id,
            confirmation.confirmed_text AS current_confirmed_text,
            confirmation.locked AS current_confirmation_locked
        FROM ml_issue_trigger_gender_role_surface_items item
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.id = (
              SELECT c.id
              FROM segment_confirmations c
              WHERE c.segment_id = item.segment_id
              ORDER BY c.updated_at DESC, c.id DESC
              LIMIT 1
          )
        WHERE item.run_id = ?
        ORDER BY item.subpolicy_name, item.relative_path, item.source_line_number, item.source_key
        """,
        (shadow_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def global_block_reasons(
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
    if shadow_run.get("agent_key") != AGENT_KEY:
        reasons.append("wrong_agent_key")
    if int(shadow_run.get("candidate_count") or 0) != len(rows):
        reasons.append("candidate_item_count_mismatch")
    if int(shadow_run.get("shadow_ready_count") or 0) < min_ready_required:
        reasons.append("min_ready_not_met")
    if int(shadow_run.get("blocked_count") or 0) > max_blocked_allowed:
        reasons.append("too_many_shadow_blocks")
    if not rows:
        reasons.append("no_checkpoint_items")
    return reasons


def row_block_reason(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    current_hash = stable_hash(row.get("current_confirmed_text"))
    issue_codes = parse_json_list(row.get("issue_codes_json"))
    reasons = {
        "shadow_item_id": int(row["id"]),
        "subpolicy_name": row.get("subpolicy_name") or "",
        "shadow_confirmed_text_hash": row.get("current_confirmed_text_hash") or "",
        "current_confirmed_text_hash": current_hash,
        "current_confirmation_id": row.get("current_confirmation_id"),
        "issue_codes": issue_codes,
    }
    if row.get("shadow_status") != READY_STATUS:
        return "not_shadow_ready_observation", reasons
    if row.get("shadow_action") not in ALLOWED_SHADOW_ACTIONS:
        return "wrong_shadow_action", reasons
    if row.get("block_reason"):
        return "shadow_item_has_block_reason", reasons
    if row.get("normalized_decision") != "needs_new_microagent":
        return "checkpoint_requires_new_microagent_decision", reasons
    if current_hash != (row.get("current_confirmed_text_hash") or ""):
        return "stale_confirmation_hash_changed", reasons
    return "", reasons


def checkpoint_action_for(row: dict[str, Any]) -> str:
    return ALLOWED_SHADOW_ACTIONS.get(row.get("shadow_action") or "", "checkpoint_unknown_trigger_gender_role")


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    shadow_run: dict[str, Any],
    rows: list[dict[str, Any]],
    checkpoint_status: str,
    promotion_status: str,
    global_reasons: list[str],
) -> None:
    fieldnames = [
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
        "checkpoint_allowed",
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
                "reasons": row.get("checkpoint_reasons") or {},
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    counts = Counter("checkpoint_allowed" if row["checkpoint_allowed"] else row["block_reason"] for row in rows)
    by_subpolicy = Counter(row["subpolicy_name"] for row in rows if row["checkpoint_allowed"])
    by_action = Counter(row["checkpoint_action"] for row in rows if row["checkpoint_allowed"])
    lines = [
        "Issue-review trigger gender-role checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint name: {CHECKPOINT_NAME}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Checkpoint status: {checkpoint_status}",
        f"Promotion status: {promotion_status}",
        "Production release allowed: 0",
        f"Shadow run id: {shadow_run['id']}",
        f"Policy name: {shadow_run['policy_name']}",
        f"Agent key: {shadow_run['agent_key']}",
        f"Decision run id: {shadow_run['decision_run_id']}",
        f"Queue run id: {shadow_run['queue_run_id']}",
        f"Candidates: {len(rows):,}",
        f"Checkpoint allowed: {sum(int(row['checkpoint_allowed']) for row in rows):,}",
        f"Checkpoint blocked: {sum(1 for row in rows if not row['checkpoint_allowed']):,}",
        "",
        "Global gate:",
        *(f"- {reason}" for reason in global_reasons),
        *(["- ok"] if not global_reasons else []),
        "",
        "Checkpoint counts:",
        *(f"- {key}: {value:,}" for key, value in counts.most_common()),
        "",
        "Allowed subpolicies:",
        *(f"- {key}: {value:,}" for key, value in by_subpolicy.most_common()),
        "",
        "Allowed actions:",
        *(f"- {key}: {value:,}" for key, value in by_action.most_common()),
        "",
        "Allowed samples:",
    ]
    for row in [item for item in rows if item["checkpoint_allowed"]][:25]:
        lines.append(
            f"- {row['subpolicy_name']} | {row['relative_path']}::{row['source_key']} "
            f"({row['checkpoint_action']})"
        )
    blocked = [item for item in rows if not item["checkpoint_allowed"]]
    if blocked:
        lines.extend(["", "Blocked samples:"])
        for row in blocked[:25]:
            lines.append(f"- {row['block_reason']} | {row['relative_path']}::{row['source_key']}")
    lines.extend(
        [
            "",
            "Safety notes:",
            "- This checkpoint is observational and shadow-only.",
            "- It does not promote a model, does not create confirmations, and does not write source/output files.",
            "- A future lifecycle policy must explicitly consume this checkpoint before any operational effect.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    shadow_run_id: int | None = None,
    min_ready_required: int = 1,
    max_blocked_allowed: int = 0,
) -> dict[str, Any]:
    settings = db.load_settings()
    txt_path, csv_path, jsonl_path = report_paths(settings)
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_shadow_run_id = shadow_run_id or latest_shadow_run_id(conn)
        shadow_run = fetch_shadow_run(conn, shadow_run_id=selected_shadow_run_id)
        rows = fetch_rows(conn, shadow_run_id=selected_shadow_run_id)
        global_reasons = global_block_reasons(
            shadow_run,
            rows,
            min_ready_required=min_ready_required,
            max_blocked_allowed=max_blocked_allowed,
        )
        for row in rows:
            block_reason, reasons = row_block_reason(row)
            if global_reasons and not block_reason:
                block_reason = "global_gate:" + ",".join(global_reasons)
            row["shadow_item_id"] = row["id"]
            row["checkpoint_action"] = checkpoint_action_for(row)
            row["checkpoint_allowed"] = 0 if block_reason else 1
            row["block_reason"] = block_reason
            row["checkpoint_reasons"] = reasons

        allowed_count = sum(int(row["checkpoint_allowed"]) for row in rows)
        blocked_count = len(rows) - allowed_count
        checkpoint_status = (
            CHECKPOINT_READY_STATUS
            if allowed_count == len(rows) and allowed_count > 0 and not global_reasons
            else "blocked_by_checkpoint_guard"
        )
        promotion_status = "trigger_gender_role_observation_candidate" if checkpoint_status == CHECKPOINT_READY_STATUS else "blocked"
        blocker_counts = Counter("checkpoint_allowed" if row["checkpoint_allowed"] else row["block_reason"] for row in rows)
        subpolicy_counts = Counter(row["subpolicy_name"] for row in rows if row["checkpoint_allowed"])
        action_counts = Counter(row["checkpoint_action"] for row in rows if row["checkpoint_allowed"])
        kinship_count = sum(
            1
            for row in rows
            if row["checkpoint_allowed"] and str(row.get("subpolicy_name") or "").startswith("trigger_kinship_")
        )
        role_article_count = sum(
            1
            for row in rows
            if row["checkpoint_allowed"] and row.get("subpolicy_name") == "trigger_role_article_boundary"
        )
        now = datetime.now().isoformat(timespec="seconds")

        cursor = conn.execute(
            """
            INSERT INTO ml_issue_trigger_gender_role_checkpoint_runs (
                rule_version,
                shadow_run_id,
                checkpoint_name,
                checkpoint_status,
                policy_name,
                policy_status,
                agent_key,
                decision_run_id,
                queue_run_id,
                min_ready_required,
                max_blocked_allowed,
                total_candidates,
                checkpoint_allowed_count,
                checkpoint_blocked_count,
                kinship_count,
                role_article_count,
                promotion_status,
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_shadow_run_id,
                CHECKPOINT_NAME,
                checkpoint_status,
                POLICY_NAME,
                "shadow",
                AGENT_KEY,
                shadow_run.get("decision_run_id"),
                shadow_run.get("queue_run_id"),
                min_ready_required,
                max_blocked_allowed,
                len(rows),
                allowed_count,
                blocked_count,
                kinship_count,
                role_article_count,
                promotion_status,
                0,
                json.dumps(dict(blocker_counts), ensure_ascii=False, sort_keys=True),
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
        checkpoint_run_id = int(cursor.lastrowid)
        for row in rows:
            item_cursor = conn.execute(
                """
                INSERT INTO ml_issue_trigger_gender_role_checkpoint_items (
                    checkpoint_run_id,
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
                    shadow_status,
                    shadow_action,
                    checkpoint_action,
                    checkpoint_allowed,
                    block_reason,
                    token_impact,
                    token_status,
                    text_length,
                    token_count,
                    word_count,
                    issue_codes_json,
                    shadow_confirmed_text_hash,
                    current_confirmed_text_hash,
                    reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_run_id,
                    selected_shadow_run_id,
                    row["shadow_item_id"],
                    row["decision_id"],
                    row["queue_item_id"],
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    AGENT_KEY,
                    row["queue_bucket"],
                    row["issue_family"],
                    row["issue_kind"],
                    row["normalized_decision"],
                    row["evidence_label"],
                    row["subpolicy_name"],
                    row["shadow_status"],
                    row["shadow_action"],
                    row["checkpoint_action"],
                    int(row["checkpoint_allowed"]),
                    row["block_reason"],
                    row.get("token_impact"),
                    row.get("token_status"),
                    int(row.get("text_length") or 0),
                    int(row.get("token_count") or 0),
                    int(row.get("word_count") or 0),
                    row.get("issue_codes_json"),
                    row.get("current_confirmed_text_hash"),
                    row["checkpoint_reasons"]["current_confirmed_text_hash"],
                    json.dumps(row["checkpoint_reasons"], ensure_ascii=False, sort_keys=True),
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
            checkpoint_status=checkpoint_status,
            promotion_status=promotion_status,
            global_reasons=global_reasons,
        )
        conn.commit()

    print("[issue_review_trigger_gender_role_checkpoint] Checkpoint generated")
    print(f"[issue_review_trigger_gender_role_checkpoint] Checkpoint run id: {checkpoint_run_id}")
    print(f"[issue_review_trigger_gender_role_checkpoint] Shadow run id: {selected_shadow_run_id}")
    print(f"[issue_review_trigger_gender_role_checkpoint] Status: {checkpoint_status}")
    print(f"[issue_review_trigger_gender_role_checkpoint] Allowed: {allowed_count:,}")
    print(f"[issue_review_trigger_gender_role_checkpoint] Blocked: {blocked_count:,}")
    print(f"[issue_review_trigger_gender_role_checkpoint] Report: {txt_path}")
    print(f"[issue_review_trigger_gender_role_checkpoint] CSV: {csv_path}")
    print(f"[issue_review_trigger_gender_role_checkpoint] JSONL: {jsonl_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "shadow_run_id": selected_shadow_run_id,
        "checkpoint_status": checkpoint_status,
        "promotion_status": promotion_status,
        "allowed": allowed_count,
        "blocked": blocked_count,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create an observational checkpoint for trigger gender-role shadow rows.")
    parser.add_argument("--shadow-run-id", type=int, default=None)
    parser.add_argument("--min-ready", type=int, default=1)
    parser.add_argument("--max-blocked", type=int, default=0)
    args = parser.parse_args()
    main(
        shadow_run_id=args.shadow_run_id,
        min_ready_required=args.min_ready,
        max_blocked_allowed=args.max_blocked,
    )
