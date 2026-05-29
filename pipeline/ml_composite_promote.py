from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "ml_composite_promote_v1"
GATE_KEY = "segment_token_composite_review_gate"
COORDINATOR_KEY = "coordinator_ensemble_v1"
POLICY_VERSION = "composite_gate_guarded_v1"
READY_STATUS = "ready_for_guarded_promotion"
PROMOTED_STATE = "operational"


def latest_checkpoint_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_composite_checkpoints
        WHERE finished_at IS NOT NULL
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No complete ml_composite_checkpoints entry found.")
    return int(row["id"])


def fetch_checkpoint(conn, checkpoint_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_composite_checkpoints
        WHERE id = ?
        """,
        (checkpoint_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Composite checkpoint {checkpoint_id} was not found.")
    return dict(row)


def parse_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    payload = json.loads(value)
    if isinstance(payload, list):
        return [str(item) for item in payload]
    return [str(payload)]


def validate_checkpoint(checkpoint: dict[str, Any]) -> tuple[bool, list[str], list[str]]:
    blockers = parse_json_list(checkpoint.get("blockers_json"))
    warnings = parse_json_list(checkpoint.get("warnings_json"))
    if checkpoint.get("promotion_status") != READY_STATUS:
        blockers.append("checkpoint_not_ready_for_guarded_promotion")
    if int(checkpoint.get("overlay_critical_count") or 0) != 0:
        blockers.append("overlay_critical_count_is_not_zero")
    if int(checkpoint.get("critical_queue_count") or 0) != 0:
        blockers.append("critical_queue_count_is_not_zero")
    if int(checkpoint.get("apply_allowed_count") or 0) != 0:
        blockers.append("apply_allowed_count_must_remain_zero")
    if int(checkpoint.get("released_critical_count") or 0) < int(checkpoint.get("base_critical_count") or 0):
        blockers.append("released_critical_count_does_not_cover_base_critical_count")
    return not blockers, sorted(set(blockers)), sorted(set(warnings))


def fetch_previous_registry(conn) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM ml_composite_gate_registry
        WHERE gate_key = ?
        """,
        (GATE_KEY,),
    ).fetchone()
    return dict(row) if row else None


def write_report(
    settings: dict[str, Any],
    *,
    checkpoint: dict[str, Any],
    previous: dict[str, Any] | None,
    decision: str,
    blockers: list[str],
    warnings: list[str],
    started_at: datetime,
) -> Path:
    lines = [
        "ML composite guarded promotion",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Gate key: {GATE_KEY}",
        f"Coordinator: {COORDINATOR_KEY}",
        f"Policy version: {POLICY_VERSION}",
        "",
        "Decision:",
        f"- Decision: {decision}",
        f"- Operational state: {PROMOTED_STATE if decision == 'promote_guarded' else 'unchanged'}",
        "- Auto-apply allowed: 0",
        f"- Blockers: {len(blockers)}",
        f"- Warnings: {len(warnings)}",
        "",
        "Checkpoint:",
        f"- Checkpoint id: {checkpoint['id']}",
        f"- Promotion status: {checkpoint['promotion_status']}",
        f"- Source policy run id: {checkpoint['source_policy_run_id']}",
        f"- Overlay run id: {checkpoint['overlay_run_id']}",
        f"- Base critical: {checkpoint['base_critical_count']}",
        f"- Overlay critical: {checkpoint['overlay_critical_count']}",
        f"- Critical queue: {checkpoint['critical_queue_count']}",
        f"- Released critical: {checkpoint['released_critical_count']}",
        f"- Apply allowed: {checkpoint['apply_allowed_count']}",
        "",
        "Previous active gate:",
        f"- Checkpoint id: {previous.get('active_checkpoint_id') if previous else 'none'}",
        f"- Overlay run id: {previous.get('active_overlay_run_id') if previous else 'none'}",
        f"- Operational state: {previous.get('operational_state') if previous else 'none'}",
        "",
        "Blockers:",
        *([f"- {item}" for item in blockers] or ["- none"]),
        "",
        "Warnings:",
        *([f"- {item}" for item in warnings] or ["- none"]),
        "",
        "Interpretation:",
        "- This promotion activates the composite overlay as the operational review gate.",
        "- It does not update output files, confirmations, source/index tables, or ML model weights.",
        "- Auto-apply remains explicitly disabled.",
    ]
    return db.write_report(settings, "ml_composite_promote", lines)


def insert_promotion(
    conn,
    *,
    checkpoint: dict[str, Any],
    previous: dict[str, Any] | None,
    decision: str,
    blockers: list[str],
    warnings: list[str],
    report_path: Path,
    now: str,
    promoted_by: str,
) -> int:
    metrics = {
        "checkpoint_id": checkpoint["id"],
        "base_critical_count": checkpoint["base_critical_count"],
        "overlay_critical_count": checkpoint["overlay_critical_count"],
        "critical_queue_count": checkpoint["critical_queue_count"],
        "released_critical_count": checkpoint["released_critical_count"],
        "enabled_rule_count": checkpoint["enabled_rule_count"],
        "report_path": str(report_path),
    }
    cursor = conn.execute(
        """
        INSERT INTO ml_composite_gate_promotions (
            gate_key,
            checkpoint_id,
            overlay_run_id,
            source_policy_run_id,
            previous_checkpoint_id,
            previous_overlay_run_id,
            decision,
            policy_version,
            auto_apply_allowed,
            reason,
            blockers_json,
            warnings_json,
            metrics_json,
            promoted_by,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            GATE_KEY,
            checkpoint["id"],
            checkpoint["overlay_run_id"],
            checkpoint["source_policy_run_id"],
            previous.get("active_checkpoint_id") if previous else None,
            previous.get("active_overlay_run_id") if previous else None,
            decision,
            POLICY_VERSION,
            0,
            "guarded composite gate promotion; auto-apply disabled",
            json.dumps(blockers, ensure_ascii=False),
            json.dumps(warnings, ensure_ascii=False),
            json.dumps(metrics, ensure_ascii=False),
            promoted_by,
            now,
        ),
    )
    return int(cursor.lastrowid)


def update_registry(conn, checkpoint: dict[str, Any], now: str, promoted_by: str, report_path: Path) -> None:
    metrics = {
        "checkpoint_id": checkpoint["id"],
        "promotion_status": checkpoint["promotion_status"],
        "base_critical_count": checkpoint["base_critical_count"],
        "overlay_critical_count": checkpoint["overlay_critical_count"],
        "critical_queue_count": checkpoint["critical_queue_count"],
        "released_critical_count": checkpoint["released_critical_count"],
        "enabled_rule_count": checkpoint["enabled_rule_count"],
        "report_path": str(report_path),
    }
    conn.execute(
        """
        INSERT INTO ml_composite_gate_registry (
            gate_key,
            coordinator_key,
            active_checkpoint_id,
            active_overlay_run_id,
            active_policy_run_id,
            operational_state,
            auto_apply_allowed,
            promoted_at,
            promoted_by,
            reason,
            metrics_json,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(gate_key) DO UPDATE SET
            coordinator_key = excluded.coordinator_key,
            active_checkpoint_id = excluded.active_checkpoint_id,
            active_overlay_run_id = excluded.active_overlay_run_id,
            active_policy_run_id = excluded.active_policy_run_id,
            operational_state = excluded.operational_state,
            auto_apply_allowed = excluded.auto_apply_allowed,
            promoted_at = excluded.promoted_at,
            promoted_by = excluded.promoted_by,
            reason = excluded.reason,
            metrics_json = excluded.metrics_json,
            updated_at = excluded.updated_at
        """,
        (
            GATE_KEY,
            COORDINATOR_KEY,
            checkpoint["id"],
            checkpoint["overlay_run_id"],
            checkpoint["source_policy_run_id"],
            PROMOTED_STATE,
            0,
            now,
            promoted_by,
            "active composite review gate; auto-apply disabled",
            json.dumps(metrics, ensure_ascii=False),
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
                '$.active_overlay_run_id',
                ?
            )
        WHERE agent_key = ?
        """,
        (
            PROMOTED_STATE,
            now,
            GATE_KEY,
            checkpoint["id"],
            checkpoint["overlay_run_id"],
            COORDINATOR_KEY,
        ),
    )


def main(*, checkpoint_id: int | None = None, promoted_by: str = "codex") -> None:
    settings = db.load_settings()
    started_at_dt = datetime.now()
    now = started_at_dt.isoformat(timespec="seconds")
    print("[ml_composite_promote] Starting guarded composite promotion")
    print(f"[ml_composite_promote] Rule version: {RULE_VERSION}")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_checkpoint_id = checkpoint_id or latest_checkpoint_id(conn)
        checkpoint = fetch_checkpoint(conn, selected_checkpoint_id)
        previous = fetch_previous_registry(conn)
        can_promote, blockers, warnings = validate_checkpoint(checkpoint)
        decision = "promote_guarded" if can_promote else "blocked"
        report_path = write_report(
            settings,
            checkpoint=checkpoint,
            previous=previous,
            decision=decision,
            blockers=blockers,
            warnings=warnings,
            started_at=started_at_dt,
        )
        promotion_id = insert_promotion(
            conn,
            checkpoint=checkpoint,
            previous=previous,
            decision=decision,
            blockers=blockers,
            warnings=warnings,
            report_path=report_path,
            now=now,
            promoted_by=promoted_by,
        )
        if can_promote:
            update_registry(conn, checkpoint, now, promoted_by, report_path)
        conn.commit()

    print(f"[ml_composite_promote] Promotion id: {promotion_id}")
    print(f"[ml_composite_promote] Checkpoint id: {selected_checkpoint_id}")
    print(f"[ml_composite_promote] Decision: {decision}")
    print(f"[ml_composite_promote] Auto-apply allowed: 0")
    print(f"[ml_composite_promote] Blockers: {len(blockers)}")
    print(f"[ml_composite_promote] Warnings: {len(warnings)}")
    print(f"[ml_composite_promote] Report: {report_path}")
    print("[ml_composite_promote] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Promote the composite overlay as a guarded review gate.")
    parser.add_argument("--checkpoint-id", type=int, default=None)
    parser.add_argument("--promoted-by", default="codex")
    args = parser.parse_args()
    main(checkpoint_id=args.checkpoint_id, promoted_by=args.promoted_by)
