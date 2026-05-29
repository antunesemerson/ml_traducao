from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from ml_composite_guarded_overlay_checkpoint import TARGET_ACTIONS
from segment_token_overlay_review_queue import DEFAULT_ACTIVE_GATE_KEY


RULE_VERSION = "ml_composite_guarded_overlay_promote_v1"
COORDINATOR_KEY = "coordinator_ensemble_v1"
POLICY_VERSION = "composite_gate_guarded_subpolicy_overlay_v1"
PROMOTION_KIND = "guarded_subpolicy_overlay"
PROMOTED_STATE = "operational"
ALLOWED_CHECKPOINT_STATUSES = {"ready_for_shadow_validation", "ready_for_guarded_gate_candidate"}
ALLOWED_WARNINGS = {
    "guarded_releases_have_text_hygiene_markers",
    "guarded_release_rate_above_20_percent_review_before_promotion",
}
STRUCTURAL_ACCEPT_DECISIONS = {"accept_policy_candidate", "encoding_cleanup_required"}
TEXT_CLEANUP_DECISION = "encoding_cleanup_required"
TEXT_CLEANUP_SAFE_NOTE = "token_release_safe_but_text_hygiene_blocks_apply"


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


def parse_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def latest_checkpoint_id(conn, gate_key: str) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_composite_guarded_overlay_checkpoints
        WHERE gate_key = ?
          AND finished_at IS NOT NULL
          AND promotion_status IN ('ready_for_shadow_validation', 'ready_for_guarded_gate_candidate')
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (gate_key,),
    ).fetchone()
    if row is None:
        raise RuntimeError("No complete guarded overlay checkpoint is ready for promotion review.")
    return int(row["id"])


def fetch_checkpoint(conn, checkpoint_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_composite_guarded_overlay_checkpoints
        WHERE id = ?
        """,
        (checkpoint_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Guarded overlay checkpoint {checkpoint_id} was not found.")
    return dict(row)


def fetch_active_gate(conn, gate_key: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_composite_gate_registry
        WHERE gate_key = ?
        """,
        (gate_key,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Active composite gate {gate_key!r} was not found.")
    return dict(row)


def fetch_overlay(conn, overlay_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM segment_token_policy_overlay_runs
        WHERE id = ?
        """,
        (overlay_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Overlay run {overlay_run_id} was not found.")
    return dict(row)


def fetch_release_rows(conn, *, checkpoint: dict[str, Any]) -> list[dict[str, Any]]:
    placeholders = ", ".join("?" for _ in TARGET_ACTIONS)
    rows = conn.execute(
        f"""
        SELECT
            oi.source_policy_item_id,
            oi.segment_id,
            oi.relative_path,
            oi.source_line_number,
            oi.source_key,
            NULL AS suggested_route,
            oi.overlay_policy_bucket,
            oi.overlay_risk_level,
            oi.overlay_action,
            oi.overlay_agent_key,
            oi.apply_allowed,
            oi.rule_key,
            d.decision AS review_decision,
            d.approved_for_apply,
            d.notes,
            d.corrected_text,
            d.reviewer,
            d.updated_at AS reviewed_at
        FROM segment_token_policy_overlay_items oi
        LEFT JOIN segment_token_policy_decisions d
          ON d.policy_run_id = ?
         AND d.policy_item_id = oi.source_policy_item_id
        WHERE oi.run_id = ?
          AND oi.overlay_action IN ({placeholders})
        ORDER BY oi.relative_path, oi.source_line_number, oi.source_key
        """,
        (
            checkpoint["source_policy_run_id"],
            checkpoint["overlay_run_id"],
            *sorted(TARGET_ACTIONS),
        ),
    ).fetchall()
    return [dict(row) for row in rows]


def validate_release_rows(rows: list[dict[str, Any]]) -> tuple[list[str], list[str], dict[str, Any]]:
    blockers: list[str] = []
    warnings: list[str] = []
    decision_counts: Counter[str] = Counter()
    reviewers: Counter[str] = Counter()
    missing_review: list[int] = []
    rejected_review: list[int] = []
    unsafe_text_cleanup_review: list[int] = []
    text_cleanup_apply_approved: list[int] = []

    for row in rows:
        decision = row.get("review_decision")
        if not decision:
            missing_review.append(int(row["source_policy_item_id"]))
            continue
        decision_counts[str(decision)] += 1
        if row.get("reviewer"):
            reviewers[str(row["reviewer"])] += 1
        if decision not in STRUCTURAL_ACCEPT_DECISIONS:
            rejected_review.append(int(row["source_policy_item_id"]))
            continue
        if decision == TEXT_CLEANUP_DECISION:
            note = row.get("notes") or ""
            if TEXT_CLEANUP_SAFE_NOTE not in note:
                unsafe_text_cleanup_review.append(int(row["source_policy_item_id"]))
            if int(row.get("approved_for_apply") or 0) != 0:
                text_cleanup_apply_approved.append(int(row["source_policy_item_id"]))

    if missing_review:
        blockers.append("guarded_release_rows_missing_shadow_review")
    if rejected_review:
        blockers.append("guarded_release_rows_have_non_accept_review_decisions")
    if unsafe_text_cleanup_review:
        blockers.append("text_cleanup_reviews_are_not_marked_structurally_safe")
    if text_cleanup_apply_approved:
        blockers.append("text_cleanup_rows_must_not_be_apply_approved")

    if decision_counts[TEXT_CLEANUP_DECISION] > 0:
        warnings.append("structural_gate_contains_text_cleanup_blocked_rows")

    metrics = {
        "release_rows": len(rows),
        "decision_counts": dict(decision_counts),
        "reviewers": dict(reviewers),
        "missing_review_sample": missing_review[:30],
        "non_accept_review_sample": rejected_review[:30],
        "unsafe_text_cleanup_sample": unsafe_text_cleanup_review[:30],
        "text_cleanup_apply_approved_sample": text_cleanup_apply_approved[:30],
        "structural_accept_count": sum(decision_counts[name] for name in STRUCTURAL_ACCEPT_DECISIONS),
        "text_cleanup_block_count": decision_counts[TEXT_CLEANUP_DECISION],
    }
    return blockers, warnings, metrics


def validate_promotion(
    *,
    checkpoint: dict[str, Any],
    overlay: dict[str, Any],
    active_gate: dict[str, Any],
    release_metrics: dict[str, Any],
) -> tuple[bool, list[str], list[str], dict[str, Any]]:
    blockers = parse_json_list(checkpoint.get("blockers_json"))
    warnings = parse_json_list(checkpoint.get("warnings_json"))
    metrics = parse_json_dict(checkpoint.get("metrics_json"))

    if checkpoint.get("promotion_status") not in ALLOWED_CHECKPOINT_STATUSES:
        blockers.append("guarded_checkpoint_status_not_promotable")
    unexpected_warnings = sorted(set(warnings) - ALLOWED_WARNINGS)
    if unexpected_warnings:
        blockers.append("guarded_checkpoint_has_unaccepted_warnings")
    if int(checkpoint.get("invalid_release_count") or 0) != 0:
        blockers.append("guarded_checkpoint_has_invalid_release_rows")
    if int(checkpoint.get("apply_allowed_count") or 0) != 0:
        blockers.append("guarded_overlay_has_apply_allowed_rows")
    if int(checkpoint.get("active_gate_unchanged") or 0) != 1:
        blockers.append("checkpoint_parent_gate_was_not_unchanged")
    if int(metrics.get("ready_pending_items") or 0) != 0:
        blockers.append("ready_rules_still_have_pending_review_items")
    if int(release_metrics["release_rows"]) != int(checkpoint.get("guarded_release_count") or 0):
        blockers.append("release_row_count_mismatch_checkpoint")
    if int(release_metrics["structural_accept_count"]) != int(release_metrics["release_rows"]):
        blockers.append("not_all_release_rows_are_structural_accepts")
    if int(active_gate.get("auto_apply_allowed") or 0) != 0:
        blockers.append("active_gate_auto_apply_must_remain_disabled")
    if int(active_gate["active_overlay_run_id"]) != int(checkpoint["active_gate_overlay_run_id"]):
        blockers.append("active_gate_no_longer_matches_checkpoint_parent")
    if int(active_gate["active_policy_run_id"]) != int(checkpoint["source_policy_run_id"]):
        blockers.append("active_gate_policy_changed_since_checkpoint")
    if int(active_gate["active_overlay_run_id"]) == int(checkpoint["overlay_run_id"]):
        blockers.append("guarded_overlay_is_already_active")
    if int(overlay["source_policy_run_id"]) != int(checkpoint["source_policy_run_id"]):
        blockers.append("overlay_source_policy_mismatch")
    if overlay.get("finished_at") is None:
        blockers.append("guarded_overlay_run_is_not_finished")

    if release_metrics["text_cleanup_block_count"]:
        warnings.append("text_cleanup_rows_remain_blocked_from_apply")

    combined_metrics = {
        "checkpoint_metrics": metrics,
        "release_review_metrics": release_metrics,
        "previous_active_checkpoint_id": active_gate.get("active_checkpoint_id"),
        "previous_active_guarded_checkpoint_id": active_gate.get("active_guarded_checkpoint_id"),
        "previous_active_overlay_run_id": active_gate.get("active_overlay_run_id"),
        "new_active_overlay_run_id": checkpoint.get("overlay_run_id"),
    }
    return not blockers, sorted(set(blockers)), sorted(set(warnings)), combined_metrics


def write_report(
    settings: dict[str, Any],
    *,
    checkpoint: dict[str, Any],
    active_gate: dict[str, Any],
    decision: str,
    blockers: list[str],
    warnings: list[str],
    metrics: dict[str, Any],
    started_at: datetime,
) -> Path:
    decision_counts = metrics["release_review_metrics"]["decision_counts"]
    decision_lines = [f"- {key}: {value}" for key, value in sorted(decision_counts.items())] or ["- none"]
    lines = [
        "ML composite guarded overlay promotion",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Gate key: {checkpoint['gate_key']}",
        f"Promotion kind: {PROMOTION_KIND}",
        f"Policy version: {POLICY_VERSION}",
        "",
        "Decision:",
        f"- Decision: {decision}",
        f"- Operational state: {PROMOTED_STATE if decision == 'promote_guarded_overlay' else 'unchanged'}",
        "- Auto-apply allowed: 0",
        f"- Blockers: {len(blockers)}",
        f"- Warnings: {len(warnings)}",
        "",
        "Checkpoint:",
        f"- Guarded checkpoint id: {checkpoint['id']}",
        f"- Macro checkpoint id kept active: {active_gate['active_checkpoint_id']}",
        f"- Promotion status: {checkpoint['promotion_status']}",
        f"- Source policy run id: {checkpoint['source_policy_run_id']}",
        f"- Parent overlay run id: {checkpoint['active_gate_overlay_run_id']}",
        f"- New overlay run id: {checkpoint['overlay_run_id']}",
        f"- Guarded releases: {checkpoint['guarded_release_count']}",
        f"- Invalid releases: {checkpoint['invalid_release_count']}",
        f"- Apply allowed: {checkpoint['apply_allowed_count']}",
        "",
        "Release review decisions:",
        *decision_lines,
        "",
        "Previous active gate:",
        f"- Overlay run id: {active_gate['active_overlay_run_id']}",
        f"- Guarded checkpoint id: {active_gate.get('active_guarded_checkpoint_id') or 'none'}",
        f"- Promotion kind: {active_gate.get('active_promotion_kind') or 'macro_checkpoint'}",
        "",
        "Blockers:",
        *([f"- {item}" for item in blockers] or ["- none"]),
        "",
        "Warnings:",
        *([f"- {item}" for item in warnings] or ["- none"]),
        "",
        "Interpretation:",
        "- This promotion activates the guarded subpolicy overlay as the operational review gate.",
        "- It does not update output files, confirmations, source/index tables, or model weights.",
        "- Auto-apply remains disabled; text cleanup rows stay blocked from output application.",
    ]
    return db.write_report(settings, "ml_composite_guarded_overlay_promote", lines)


def insert_promotion(
    conn,
    *,
    checkpoint: dict[str, Any],
    active_gate: dict[str, Any],
    decision: str,
    blockers: list[str],
    warnings: list[str],
    metrics: dict[str, Any],
    report_path: Path,
    now: str,
    promoted_by: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO ml_composite_gate_promotions (
            gate_key,
            checkpoint_id,
            guarded_checkpoint_id,
            overlay_run_id,
            source_policy_run_id,
            previous_checkpoint_id,
            previous_overlay_run_id,
            decision,
            policy_version,
            promotion_kind,
            auto_apply_allowed,
            reason,
            blockers_json,
            warnings_json,
            metrics_json,
            promoted_by,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            checkpoint["gate_key"],
            active_gate["active_checkpoint_id"],
            checkpoint["id"],
            checkpoint["overlay_run_id"],
            checkpoint["source_policy_run_id"],
            active_gate["active_checkpoint_id"],
            active_gate["active_overlay_run_id"],
            decision,
            POLICY_VERSION,
            PROMOTION_KIND,
            0,
            "guarded subpolicy overlay promotion; auto-apply disabled",
            json.dumps(blockers, ensure_ascii=False),
            json.dumps(warnings, ensure_ascii=False),
            json.dumps({**metrics, "report_path": str(report_path)}, ensure_ascii=False),
            promoted_by,
            now,
        ),
    )
    return int(cursor.lastrowid)


def update_registry(
    conn,
    *,
    checkpoint: dict[str, Any],
    active_gate: dict[str, Any],
    metrics: dict[str, Any],
    report_path: Path,
    now: str,
    promoted_by: str,
) -> None:
    registry_metrics = {
        **metrics,
        "active_guarded_checkpoint_id": checkpoint["id"],
        "active_macro_checkpoint_id": active_gate["active_checkpoint_id"],
        "promotion_kind": PROMOTION_KIND,
        "report_path": str(report_path),
    }
    conn.execute(
        """
        INSERT INTO ml_composite_gate_registry (
            gate_key,
            coordinator_key,
            active_checkpoint_id,
            active_guarded_checkpoint_id,
            active_overlay_run_id,
            active_policy_run_id,
            operational_state,
            active_promotion_kind,
            auto_apply_allowed,
            promoted_at,
            promoted_by,
            reason,
            metrics_json,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(gate_key) DO UPDATE SET
            coordinator_key = excluded.coordinator_key,
            active_checkpoint_id = excluded.active_checkpoint_id,
            active_guarded_checkpoint_id = excluded.active_guarded_checkpoint_id,
            active_overlay_run_id = excluded.active_overlay_run_id,
            active_policy_run_id = excluded.active_policy_run_id,
            operational_state = excluded.operational_state,
            active_promotion_kind = excluded.active_promotion_kind,
            auto_apply_allowed = excluded.auto_apply_allowed,
            promoted_at = excluded.promoted_at,
            promoted_by = excluded.promoted_by,
            reason = excluded.reason,
            metrics_json = excluded.metrics_json,
            updated_at = excluded.updated_at
        """,
        (
            checkpoint["gate_key"],
            COORDINATOR_KEY,
            active_gate["active_checkpoint_id"],
            checkpoint["id"],
            checkpoint["overlay_run_id"],
            checkpoint["source_policy_run_id"],
            PROMOTED_STATE,
            PROMOTION_KIND,
            0,
            now,
            promoted_by,
            "active guarded subpolicy overlay; auto-apply disabled",
            json.dumps(registry_metrics, ensure_ascii=False),
            now,
        ),
    )
    conn.execute(
        """
        UPDATE ml_agent_registry
        SET operational_state = ?,
            decision_role = 'route_and_arbitrate',
            updated_at = ?,
            notes_json = json_set(
                COALESCE(notes_json, '{}'),
                '$.active_composite_gate',
                ?,
                '$.active_checkpoint_id',
                ?,
                '$.active_guarded_checkpoint_id',
                ?,
                '$.active_overlay_run_id',
                ?,
                '$.active_promotion_kind',
                ?
            )
        WHERE agent_key = ?
        """,
        (
            PROMOTED_STATE,
            now,
            checkpoint["gate_key"],
            active_gate["active_checkpoint_id"],
            checkpoint["id"],
            checkpoint["overlay_run_id"],
            PROMOTION_KIND,
            COORDINATOR_KEY,
        ),
    )


def main(
    *,
    checkpoint_id: int | None = None,
    gate_key: str = DEFAULT_ACTIVE_GATE_KEY,
    promoted_by: str = "codex_guarded_overlay",
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    now = started_at.isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_checkpoint_id = checkpoint_id or latest_checkpoint_id(conn, gate_key)
        checkpoint = fetch_checkpoint(conn, selected_checkpoint_id)
        active_gate = fetch_active_gate(conn, checkpoint["gate_key"])
        overlay = fetch_overlay(conn, int(checkpoint["overlay_run_id"]))
        release_rows = fetch_release_rows(conn, checkpoint=checkpoint)
        release_blockers, release_warnings, release_metrics = validate_release_rows(release_rows)
        can_promote, blockers, warnings, metrics = validate_promotion(
            checkpoint=checkpoint,
            overlay=overlay,
            active_gate=active_gate,
            release_metrics=release_metrics,
        )
        blockers = sorted(set(blockers + release_blockers))
        warnings = sorted(set(warnings + release_warnings))
        can_promote = can_promote and not blockers
        decision = "promote_guarded_overlay" if can_promote else "blocked"
        report_path = write_report(
            settings,
            checkpoint=checkpoint,
            active_gate=active_gate,
            decision=decision,
            blockers=blockers,
            warnings=warnings,
            metrics=metrics,
            started_at=started_at,
        )
        promotion_id = insert_promotion(
            conn,
            checkpoint=checkpoint,
            active_gate=active_gate,
            decision=decision,
            blockers=blockers,
            warnings=warnings,
            metrics=metrics,
            report_path=report_path,
            now=now,
            promoted_by=promoted_by,
        )
        if can_promote:
            update_registry(
                conn,
                checkpoint=checkpoint,
                active_gate=active_gate,
                metrics=metrics,
                report_path=report_path,
                now=now,
                promoted_by=promoted_by,
            )
        conn.commit()

    print("[ml_composite_guarded_overlay_promote] Promotion evaluated")
    print(f"[ml_composite_guarded_overlay_promote] Rule version: {RULE_VERSION}")
    print(f"[ml_composite_guarded_overlay_promote] Promotion id: {promotion_id}")
    print(f"[ml_composite_guarded_overlay_promote] Guarded checkpoint id: {selected_checkpoint_id}")
    print(f"[ml_composite_guarded_overlay_promote] Decision: {decision}")
    print(f"[ml_composite_guarded_overlay_promote] Overlay run id: {checkpoint['overlay_run_id']}")
    print(f"[ml_composite_guarded_overlay_promote] Auto-apply allowed: 0")
    print(f"[ml_composite_guarded_overlay_promote] Release rows: {release_metrics['release_rows']}")
    print(f"[ml_composite_guarded_overlay_promote] Text cleanup blocked: {release_metrics['text_cleanup_block_count']}")
    print(f"[ml_composite_guarded_overlay_promote] Blockers: {len(blockers)}")
    print(f"[ml_composite_guarded_overlay_promote] Warnings: {len(warnings)}")
    print(f"[ml_composite_guarded_overlay_promote] Report: {report_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Promote a guarded composite overlay as the active gate.")
    parser.add_argument("--checkpoint-id", type=int, default=None)
    parser.add_argument("--gate-key", default=DEFAULT_ACTIVE_GATE_KEY)
    parser.add_argument("--promoted-by", default="codex_guarded_overlay")
    args = parser.parse_args()
    main(checkpoint_id=args.checkpoint_id, gate_key=args.gate_key, promoted_by=args.promoted_by)
