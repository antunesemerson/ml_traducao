from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "ml_composite_checkpoint_v1"
CHECKPOINT_NAME = "segment_token_composite_overlay_zero_critical"
CHECKPOINT_SCOPE = "segment_token_policy_overlay"
COORDINATOR_KEY = "coordinator_ensemble_v1"


def latest_overlay_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_token_policy_overlay_runs
        WHERE finished_at IS NOT NULL
          AND total_candidates > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No completed segment_token_policy_overlay_runs entry found.")
    return int(row["id"])


def fetch_overlay_summary(conn, overlay_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            o.*,
            p.critical_count AS base_critical_count,
            p.high_count,
            p.medium_count,
            p.low_count,
            p.report_path AS source_policy_report_path
        FROM segment_token_policy_overlay_runs o
        JOIN segment_token_policy_runs p ON p.id = o.source_policy_run_id
        WHERE o.id = ?
        """,
        (overlay_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Overlay run {overlay_run_id} was not found.")
    return dict(row)


def fetch_agent_counts(conn) -> dict[str, int]:
    if not table_exists(conn, "ml_agent_registry"):
        return {
            "active_agent_count": 0,
            "operational_agent_count": 0,
            "experimental_agent_count": 0,
            "planned_agent_count": 0,
        }
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active_agent_count,
            SUM(CASE WHEN operational_state = 'operational' THEN 1 ELSE 0 END) AS operational_agent_count,
            SUM(CASE WHEN operational_state IN ('experimental', 'dry_run') THEN 1 ELSE 0 END) AS experimental_agent_count,
            SUM(CASE WHEN status = 'planned' THEN 1 ELSE 0 END) AS planned_agent_count
        FROM ml_agent_registry
        """
    ).fetchone()
    return {
        "active_agent_count": int(row["active_agent_count"] or 0) if row else 0,
        "operational_agent_count": int(row["operational_agent_count"] or 0) if row else 0,
        "experimental_agent_count": int(row["experimental_agent_count"] or 0) if row else 0,
        "planned_agent_count": int(row["planned_agent_count"] or 0) if row else 0,
    }


def table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def rows_by_key(conn, query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def build_metrics(conn, overlay_run_id: int) -> dict[str, Any]:
    risk_comparison = rows_by_key(
        conn,
        """
        SELECT original_risk_level, overlay_risk_level, COUNT(*) AS total
        FROM segment_token_policy_overlay_items
        WHERE run_id = ?
        GROUP BY original_risk_level, overlay_risk_level
        ORDER BY total DESC
        """,
        (overlay_run_id,),
    )
    action_distribution = rows_by_key(
        conn,
        """
        SELECT overlay_action, overlay_agent_key, overlay_risk_level, COUNT(*) AS total
        FROM segment_token_policy_overlay_items
        WHERE run_id = ?
        GROUP BY overlay_action, overlay_agent_key, overlay_risk_level
        ORDER BY total DESC
        """,
        (overlay_run_id,),
    )
    released_by_rule = rows_by_key(
        conn,
        """
        SELECT
            COALESCE(NULLIF(rule_key, ''), 'unchanged') AS rule_key,
            overlay_agent_key,
            COUNT(*) AS total
        FROM segment_token_policy_overlay_items
        WHERE run_id = ?
          AND would_release_critical = 1
        GROUP BY COALESCE(NULLIF(rule_key, ''), 'unchanged'), overlay_agent_key
        ORDER BY total DESC, rule_key
        """,
        (overlay_run_id,),
    )
    remaining_critical_by_bucket = rows_by_key(
        conn,
        """
        SELECT overlay_policy_bucket, overlay_agent_key, COUNT(*) AS total
        FROM segment_token_policy_overlay_items
        WHERE run_id = ?
          AND overlay_risk_level = 'critical'
        GROUP BY overlay_policy_bucket, overlay_agent_key
        ORDER BY total DESC, overlay_policy_bucket
        """,
        (overlay_run_id,),
    )
    return {
        "risk_comparison": risk_comparison,
        "action_distribution": action_distribution,
        "released_by_rule": released_by_rule,
        "remaining_critical_by_bucket": remaining_critical_by_bucket,
    }


def evaluate_checkpoint(summary: dict[str, Any], metrics: dict[str, Any]) -> tuple[str, str, list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    base_critical = int(summary.get("base_critical_count") or 0)
    overlay_critical = int(summary.get("overlay_critical_count") or 0)
    released = int(summary.get("released_critical_count") or 0)
    apply_allowed = int(summary.get("apply_allowed_count") or 0)
    enabled_rules = int(summary.get("enabled_rule_count") or 0)
    total_candidates = int(summary.get("total_candidates") or 0)

    if total_candidates <= 0:
        blockers.append("overlay_has_no_candidates")
    if overlay_critical > 0:
        blockers.append("overlay_still_has_critical_segments")
    if apply_allowed > 0:
        blockers.append("overlay_allows_apply_but_checkpoint_must_be_dry_run")
    if base_critical > 0 and released < base_critical:
        blockers.append("not_all_base_critical_segments_are_explained_by_overlay")
    if base_critical > 0 and enabled_rules <= 0:
        blockers.append("critical_release_without_enabled_subpolicy_rules")

    if base_critical > 0:
        warnings.append("base_policy_still_marks_segments_critical_but_composite_overlay_resolves_them")
    if metrics["remaining_critical_by_bucket"]:
        warnings.append("remaining_critical_bucket_distribution_is_not_empty")
    if enabled_rules < 2:
        warnings.append("few_enabled_rules_review_before_active_promotion")

    if blockers:
        return (
            "blocked",
            "keep_composite_overlay_in_dry_run_and_review_blockers",
            blockers,
            warnings,
        )
    if overlay_critical == 0 and apply_allowed == 0:
        return (
            "ready_for_guarded_promotion",
            "promote_composite_overlay_to_active_review_gate_keep_auto_apply_disabled",
            blockers,
            warnings,
        )
    return (
        "watch",
        "keep_collecting_overlay_runs_before_promotion",
        blockers,
        warnings,
    )


def write_report(
    settings: dict[str, Any],
    *,
    summary: dict[str, Any],
    agent_counts: dict[str, int],
    metrics: dict[str, Any],
    promotion_status: str,
    recommended_action: str,
    blockers: list[str],
    warnings: list[str],
    started_at: datetime,
) -> Path:
    risk_pairs = [
        f"- {row['original_risk_level']} -> {row['overlay_risk_level']}: {row['total']}"
        for row in metrics["risk_comparison"]
    ] or ["- none: 0"]
    actions = [
        f"- {row['overlay_action']} | {row['overlay_agent_key']} | {row['overlay_risk_level']}: {row['total']}"
        for row in metrics["action_distribution"]
    ] or ["- none: 0"]
    released = [
        f"- {row['rule_key']} | {row['overlay_agent_key']}: {row['total']}"
        for row in metrics["released_by_rule"]
    ] or ["- none: 0"]
    remaining = [
        f"- {row['overlay_policy_bucket']} | {row['overlay_agent_key']}: {row['total']}"
        for row in metrics["remaining_critical_by_bucket"]
    ] or ["- none: 0"]
    blocker_lines = [f"- {item}" for item in blockers] or ["- none"]
    warning_lines = [f"- {item}" for item in warnings] or ["- none"]

    lines = [
        "ML composite checkpoint",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint name: {CHECKPOINT_NAME}",
        f"Scope: {CHECKPOINT_SCOPE}",
        f"Coordinator: {COORDINATOR_KEY}",
        "",
        "Decision:",
        f"- Promotion status: {promotion_status}",
        f"- Recommended action: {recommended_action}",
        f"- Blockers: {len(blockers)}",
        f"- Warnings: {len(warnings)}",
        "",
        "Core metrics:",
        f"- Source policy run id: {summary['source_policy_run_id']}",
        f"- Overlay run id: {summary['id']}",
        f"- Total candidates: {summary['total_candidates']}",
        f"- Base critical: {summary['base_critical_count']}",
        f"- Overlay critical: {summary['overlay_critical_count']}",
        f"- Released critical: {summary['released_critical_count']}",
        f"- Apply allowed: {summary['apply_allowed_count']}",
        f"- Enabled rules: {summary['enabled_rule_count']}",
        f"- High/medium/low in base policy: {summary['high_count']}/{summary['medium_count']}/{summary['low_count']}",
        "",
        "Agents:",
        f"- Active: {agent_counts['active_agent_count']}",
        f"- Operational: {agent_counts['operational_agent_count']}",
        f"- Experimental/dry-run: {agent_counts['experimental_agent_count']}",
        f"- Planned: {agent_counts['planned_agent_count']}",
        "",
        "Blockers:",
        *blocker_lines,
        "",
        "Warnings:",
        *warning_lines,
        "",
        "Risk comparison:",
        *risk_pairs,
        "",
        "Overlay actions:",
        *actions,
        "",
        "Released by rule:",
        *released,
        "",
        "Remaining critical by bucket:",
        *remaining,
        "",
        "Interpretation:",
        "- This checkpoint evaluates the composite coordinator as a review gate, not as an output writer.",
        "- Promotion means using the composite overlay to classify token-policy criticality in operational review queues.",
        "- Auto-apply remains disabled until a separate output-application gate is reviewed.",
    ]
    return db.write_report(settings, "ml_composite_checkpoint", lines)


def insert_checkpoint(
    conn,
    *,
    summary: dict[str, Any],
    agent_counts: dict[str, int],
    metrics: dict[str, Any],
    promotion_status: str,
    recommended_action: str,
    blockers: list[str],
    warnings: list[str],
    report_path: Path,
    started_at: str,
    finished_at: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO ml_composite_checkpoints (
            rule_version,
            checkpoint_name,
            checkpoint_scope,
            coordinator_key,
            source_policy_run_id,
            overlay_run_id,
            source_state_run_id,
            total_candidates,
            base_critical_count,
            overlay_critical_count,
            released_critical_count,
            critical_queue_count,
            high_count,
            medium_count,
            low_count,
            enabled_rule_count,
            apply_allowed_count,
            active_agent_count,
            operational_agent_count,
            experimental_agent_count,
            planned_agent_count,
            promotion_status,
            recommended_action,
            blockers_json,
            warnings_json,
            metrics_json,
            report_path,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            CHECKPOINT_NAME,
            CHECKPOINT_SCOPE,
            COORDINATOR_KEY,
            summary["source_policy_run_id"],
            summary["id"],
            summary.get("source_state_run_id"),
            summary.get("total_candidates") or 0,
            summary.get("base_critical_count") or 0,
            summary.get("overlay_critical_count") or 0,
            summary.get("released_critical_count") or 0,
            summary.get("overlay_critical_count") or 0,
            summary.get("high_count") or 0,
            summary.get("medium_count") or 0,
            summary.get("low_count") or 0,
            summary.get("enabled_rule_count") or 0,
            summary.get("apply_allowed_count") or 0,
            agent_counts["active_agent_count"],
            agent_counts["operational_agent_count"],
            agent_counts["experimental_agent_count"],
            agent_counts["planned_agent_count"],
            promotion_status,
            recommended_action,
            json.dumps(blockers, ensure_ascii=False),
            json.dumps(warnings, ensure_ascii=False),
            json.dumps(metrics, ensure_ascii=False),
            str(report_path),
            started_at,
            finished_at,
            finished_at,
        ),
    )
    return int(cursor.lastrowid)


def main(*, overlay_run_id: int | None = None) -> None:
    settings = db.load_settings()
    started_at_dt = datetime.now()
    started_at = started_at_dt.isoformat(timespec="seconds")
    print("[ml_composite_checkpoint] Starting composite checkpoint")
    print(f"[ml_composite_checkpoint] Rule version: {RULE_VERSION}")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_overlay_run_id = overlay_run_id or latest_overlay_run_id(conn)
        summary = fetch_overlay_summary(conn, selected_overlay_run_id)
        agent_counts = fetch_agent_counts(conn)
        metrics = build_metrics(conn, selected_overlay_run_id)
        promotion_status, recommended_action, blockers, warnings = evaluate_checkpoint(summary, metrics)
        report_path = write_report(
            settings,
            summary=summary,
            agent_counts=agent_counts,
            metrics=metrics,
            promotion_status=promotion_status,
            recommended_action=recommended_action,
            blockers=blockers,
            warnings=warnings,
            started_at=started_at_dt,
        )
        finished_at = datetime.now().isoformat(timespec="seconds")
        checkpoint_id = insert_checkpoint(
            conn,
            summary=summary,
            agent_counts=agent_counts,
            metrics=metrics,
            promotion_status=promotion_status,
            recommended_action=recommended_action,
            blockers=blockers,
            warnings=warnings,
            report_path=report_path,
            started_at=started_at,
            finished_at=finished_at,
        )
        conn.commit()

    print(f"[ml_composite_checkpoint] Checkpoint id: {checkpoint_id}")
    print(f"[ml_composite_checkpoint] Overlay run id: {selected_overlay_run_id}")
    print(f"[ml_composite_checkpoint] Promotion status: {promotion_status}")
    print(f"[ml_composite_checkpoint] Recommended action: {recommended_action}")
    print(f"[ml_composite_checkpoint] Blockers: {len(blockers)}")
    print(f"[ml_composite_checkpoint] Warnings: {len(warnings)}")
    print(f"[ml_composite_checkpoint] Report: {report_path}")
    print("[ml_composite_checkpoint] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create an official checkpoint for the composite ML/token-policy overlay.")
    parser.add_argument("--overlay-run-id", type=int, default=None)
    args = parser.parse_args()
    main(overlay_run_id=args.overlay_run_id)
