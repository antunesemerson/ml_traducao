from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "learning_network_diagnostic_v1"


def latest_id(conn, table_name: str, where_sql: str = "1 = 1") -> int | None:
    row = conn.execute(
        f"""
        SELECT id
        FROM {table_name}
        WHERE {where_sql}
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    return int(row["id"]) if row else None


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_learning_network_diagnostic"
    return base.with_suffix(".txt"), base.with_suffix(".json")


def fetch_one(conn, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def fetch_all(conn, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        LIMIT 1
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def percent(numerator: int | float, denominator: int | float) -> float:
    if not denominator:
        return 0.0
    return float(numerator) / float(denominator)


def parse_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def summarize_agent_registry(conn) -> dict[str, Any]:
    rows = fetch_all(
        conn,
        """
        SELECT
            agent_type,
            status,
            operational_state,
            dashboard_group,
            COUNT(*) AS count
        FROM ml_agent_registry
        GROUP BY agent_type, status, operational_state, dashboard_group
        ORDER BY dashboard_group, agent_type, status, operational_state
        """,
    )
    total = sum(int(row["count"] or 0) for row in rows)
    by_status = Counter()
    by_state = Counter()
    by_group = Counter()
    by_type = Counter()
    for row in rows:
        count = int(row["count"] or 0)
        by_status[row["status"]] += count
        by_state[row["operational_state"]] += count
        by_group[row["dashboard_group"] or "unassigned"] += count
        by_type[row["agent_type"]] += count
    return {
        "total": total,
        "by_status": dict(by_status),
        "by_operational_state": dict(by_state),
        "by_dashboard_group": dict(by_group),
        "by_agent_type": dict(by_type),
        "rows": rows,
    }


def summarize_latest_routing(conn) -> dict[str, Any] | None:
    run_id = latest_id(conn, "ml_agent_routing_runs", "finished_at IS NOT NULL")
    if run_id is None:
        return None
    run = fetch_one(conn, "SELECT * FROM ml_agent_routing_runs WHERE id = ?", (run_id,))
    notes = {}
    if run and run.get("notes_json"):
        try:
            notes = json.loads(run["notes_json"])
        except json.JSONDecodeError:
            notes = {}
    materialized_count = fetch_one(
        conn,
        """
        SELECT COUNT(*) AS count
        FROM ml_agent_routing_items
        WHERE run_id = ?
        """,
        (run_id,),
    )
    by_status = fetch_all(
        conn,
        """
        SELECT route_status, COUNT(*) AS count
        FROM ml_agent_routing_items
        WHERE run_id = ?
        GROUP BY route_status
        ORDER BY count DESC
        """,
        (run_id,),
    )
    top_agents = fetch_all(
        conn,
        """
        SELECT route_agent_key, route_status, COUNT(*) AS count
        FROM ml_agent_routing_items
        WHERE run_id = ?
        GROUP BY route_agent_key, route_status
        ORDER BY count DESC, route_agent_key
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "notes": notes,
        "scope_sum_count": int(run["routed_count"] or 0) if run else 0,
        "materialized_count": int(materialized_count["count"] or 0) if materialized_count else 0,
        "by_status": by_status,
        "top_agents": top_agents,
    }


def summarize_text_audit(conn) -> dict[str, Any] | None:
    run_id = latest_id(conn, "auto_confirmation_reopen_text_specialist_audit_runs", "finished_at IS NOT NULL")
    if run_id is None:
        return None
    run = fetch_one(conn, "SELECT * FROM auto_confirmation_reopen_text_specialist_audit_runs WHERE id = ?", (run_id,))
    rows = fetch_all(
        conn,
        """
        SELECT *
        FROM auto_confirmation_reopen_text_specialist_audit_items
        WHERE run_id = ?
        ORDER BY diagnostic_count DESC, agent_key
        """,
        (run_id,),
    )
    totals = Counter()
    status_counts = Counter()
    for row in rows:
        totals["diagnostic"] += int(row["diagnostic_count"] or 0)
        totals["queued"] += int(row["queued_count"] or 0)
        totals["reviewed"] += int(row["reviewed_count"] or 0)
        totals["positive"] += int(row["positive_count"] or 0)
        totals["negative"] += int(row["negative_count"] or 0)
        totals["context"] += int(row["needs_more_context_count"] or 0)
        totals["manual"] += int(row["manual_exception_count"] or 0)
        status_counts[row["maturity_status"]] += 1
    clean_candidates = [
        row
        for row in rows
        if int(row["reviewed_count"] or 0) > 0
        and int(row["negative_count"] or 0) == 0
        and int(row["needs_more_context_count"] or 0) == 0
    ]
    blockers = [
        row
        for row in rows
        if int(row["negative_count"] or 0) > 0 or int(row["needs_more_context_count"] or 0) > 0
    ]
    return {
        "run": run,
        "totals": dict(totals),
        "review_coverage": percent(totals["reviewed"], totals["diagnostic"]),
        "positive_rate": percent(totals["positive"], totals["reviewed"]),
        "negative_rate": percent(totals["negative"], totals["reviewed"]),
        "status_counts": dict(status_counts),
        "clean_candidates": clean_candidates,
        "blockers": blockers,
        "rows": rows,
    }


def summarize_weak_auto_split_agents(conn) -> dict[str, Any] | None:
    parents = {
        "weak_auto_embedded_literal_token_specialist",
        "weak_auto_token_surface_semantic_boundary",
        "weak_auto_custom_loc_helper_boundary",
    }
    placeholders = ",".join("?" for _ in parents)
    rows = fetch_all(
        conn,
        f"""
        SELECT
            agent_key,
            parent_agent_key,
            status,
            operational_state,
            decision_role,
            scope_group,
            scope_description,
            notes_json
        FROM ml_agent_registry
        WHERE parent_agent_key IN ({placeholders})
          AND agent_key NOT IN ({placeholders})
        ORDER BY parent_agent_key, decision_role, agent_key
        """,
        (*sorted(parents), *sorted(parents)),
    )
    if not rows:
        return None
    parsed_rows: list[dict[str, Any]] = []
    totals = Counter()
    by_parent = Counter()
    by_maturity = Counter()
    for row in rows:
        notes: dict[str, Any] = {}
        if row.get("notes_json"):
            try:
                notes = json.loads(row["notes_json"])
            except json.JSONDecodeError:
                notes = {}
        reviewed = int(notes.get("reviewed_count") or 0)
        positive = int(notes.get("positive_count") or 0)
        negative = int(notes.get("negative_count") or 0)
        maturity = str(notes.get("maturity_status") or "unknown")
        totals["agents"] += 1
        totals["reviewed"] += reviewed
        totals["positive"] += positive
        totals["negative"] += negative
        by_parent[row["parent_agent_key"]] += 1
        by_maturity[maturity] += 1
        parsed_rows.append(
            {
                **row,
                "notes": notes,
                "reviewed_count": reviewed,
                "positive_count": positive,
                "negative_count": negative,
                "negative_rate": percent(negative, reviewed),
                "maturity_status": maturity,
            }
        )
    hard_boundaries = [
        row
        for row in parsed_rows
        if row["maturity_status"] in {"hard_boundary_candidate", "blocked_boundary"}
    ]
    positive_candidates = [
        row
        for row in parsed_rows
        if row["maturity_status"].startswith("positive")
    ]
    hard_boundaries.sort(key=lambda row: (row["negative_count"], row["reviewed_count"], row["agent_key"]), reverse=True)
    positive_candidates.sort(key=lambda row: (row["positive_count"], row["reviewed_count"], row["agent_key"]), reverse=True)
    return {
        "totals": dict(totals),
        "by_parent": dict(by_parent),
        "by_maturity": dict(by_maturity),
        "rows": parsed_rows,
        "hard_boundaries": hard_boundaries,
        "positive_candidates": positive_candidates,
    }


def summarize_text_shadow_policies(conn) -> dict[str, Any] | None:
    rows = fetch_all(
        conn,
        """
        SELECT run.*
        FROM auto_confirmation_reopen_text_shadow_policy_runs run
        JOIN (
            SELECT policy_name, MAX(id) AS id
            FROM auto_confirmation_reopen_text_shadow_policy_runs
            WHERE finished_at IS NOT NULL
            GROUP BY policy_name
        ) latest
          ON latest.id = run.id
        ORDER BY run.started_at DESC, run.id DESC
        """
    )
    if not rows:
        return None
    totals = Counter()
    for row in rows:
        totals["policies"] += 1
        totals["candidates"] += int(row["total_candidates"] or 0)
        totals["shadow_ready"] += int(row["shadow_ready_count"] or 0)
        totals["blocked"] += int(row["blocked_count"] or 0)
        totals["positive"] += int(row["positive_evidence_count"] or 0)
        totals["negative"] += int(row["negative_evidence_count"] or 0)
    return {
        "totals": dict(totals),
        "rows": rows,
        "ready_rate": percent(totals["shadow_ready"], totals["candidates"]),
    }


def summarize_text_boundary_policies(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "auto_confirmation_reopen_text_boundary_policy_runs"):
        return None
    consolidated_rows = fetch_all(
        conn,
        """
        SELECT run.*
        FROM auto_confirmation_reopen_text_boundary_policy_runs run
        WHERE run.finished_at IS NOT NULL
          AND run.policy_name = 'weak_auto_boundary_policy_all_v1'
        ORDER BY run.finished_at DESC, run.id DESC
        LIMIT 1
        """,
    )
    snapshot_mode = "consolidated_all" if consolidated_rows else "latest_by_policy_name"
    rows = consolidated_rows or fetch_all(
        conn,
        """
        SELECT run.*
        FROM auto_confirmation_reopen_text_boundary_policy_runs run
        JOIN (
            SELECT policy_name, MAX(id) AS id
            FROM auto_confirmation_reopen_text_boundary_policy_runs
            WHERE finished_at IS NOT NULL
            GROUP BY policy_name
        ) latest
          ON latest.id = run.id
        ORDER BY run.started_at DESC, run.id DESC
        """,
    )
    if not rows:
        return None
    run_ids = [int(row["id"]) for row in rows]
    placeholders = ",".join("?" for _ in run_ids)
    items = fetch_all(
        conn,
        f"""
        SELECT
            boundary_policy,
            boundary_agent_key,
            boundary_status,
            token_status,
            COUNT(*) AS count
        FROM auto_confirmation_reopen_text_boundary_policy_items
        WHERE run_id IN ({placeholders})
        GROUP BY boundary_policy, boundary_agent_key, boundary_status, token_status
        ORDER BY count DESC, boundary_policy, boundary_status, token_status
        """,
        tuple(run_ids),
    )
    totals = Counter()
    by_policy = Counter()
    by_status = Counter()
    by_token = Counter()
    for row in rows:
        totals["policies"] += 1
        totals["candidates"] += int(row["total_candidates"] or 0)
        totals["repair_candidates"] += int(row["repair_candidate_count"] or 0)
        totals["block_only"] += int(row["block_only_count"] or 0)
        totals["same_token"] += int(row["same_token_count"] or 0)
        totals["token_change"] += int(row["token_change_count"] or 0)
        totals["unclassified"] += int(row["unclassified_count"] or 0)
    for row in items:
        count = int(row["count"] or 0)
        by_policy[row["boundary_policy"]] += count
        by_status[row["boundary_status"]] += count
        by_token[row["token_status"]] += count
    return {
        "totals": dict(totals),
        "snapshot_mode": snapshot_mode,
        "rows": rows,
        "items": items,
        "by_policy": dict(by_policy),
        "by_status": dict(by_status),
        "by_token": dict(by_token),
        "repair_rate": percent(totals["repair_candidates"], totals["candidates"]),
        "same_token_repair_rate": percent(totals["same_token"], totals["repair_candidates"]),
    }


def summarize_text_boundary_repair_queues(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "auto_confirmation_reopen_text_boundary_repair_queue_runs"):
        return None
    rows = fetch_all(
        conn,
        """
        SELECT run.*
        FROM auto_confirmation_reopen_text_boundary_repair_queue_runs run
        JOIN (
            SELECT queue_scope, MAX(id) AS id
            FROM auto_confirmation_reopen_text_boundary_repair_queue_runs
            WHERE finished_at IS NOT NULL
            GROUP BY queue_scope
        ) latest
          ON latest.id = run.id
        ORDER BY run.started_at DESC, run.id DESC
        """
    )
    if not rows:
        return None
    run_ids = [int(row["id"]) for row in rows]
    placeholders = ",".join("?" for _ in run_ids)
    items = fetch_all(
        conn,
        f"""
        SELECT
            boundary_policy,
            boundary_agent_key,
            repair_route,
            risk_level,
            token_status,
            COUNT(*) AS count
        FROM auto_confirmation_reopen_text_boundary_repair_queue_items
        WHERE run_id IN ({placeholders})
        GROUP BY boundary_policy, boundary_agent_key, repair_route, risk_level, token_status
        ORDER BY count DESC, boundary_policy, repair_route
        """,
        tuple(run_ids),
    )
    totals = Counter()
    by_scope = Counter()
    by_route = Counter()
    by_policy = Counter()
    by_risk = Counter()
    for row in rows:
        count = int(row["selected_count"] or 0)
        totals["queues"] += 1
        totals["candidates"] += int(row["candidate_count"] or 0)
        totals["selected"] += count
        totals["same_token"] += int(row["same_token_count"] or 0)
        totals["token_change"] += int(row["token_change_count"] or 0)
        by_scope[row["queue_scope"]] += count
    for row in items:
        count = int(row["count"] or 0)
        by_route[row["repair_route"]] += count
        by_policy[row["boundary_policy"]] += count
        by_risk[row["risk_level"]] += count
    return {
        "totals": dict(totals),
        "rows": rows,
        "items": items,
        "by_scope": dict(by_scope),
        "by_route": dict(by_route),
        "by_policy": dict(by_policy),
        "by_risk": dict(by_risk),
        "selection_rate": percent(totals["selected"], totals["candidates"]),
    }


def summarize_text_boundary_repair_shadow_policies(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "auto_confirmation_reopen_text_boundary_repair_shadow_runs"):
        return None
    rows = fetch_all(
        conn,
        """
        SELECT run.*
        FROM auto_confirmation_reopen_text_boundary_repair_shadow_runs run
        JOIN (
            SELECT policy_name, MAX(id) AS id
            FROM auto_confirmation_reopen_text_boundary_repair_shadow_runs
            WHERE finished_at IS NOT NULL
            GROUP BY policy_name
        ) latest
          ON latest.id = run.id
        ORDER BY run.started_at DESC, run.id DESC
        """
    )
    if not rows:
        return None
    run_ids = [int(row["id"]) for row in rows]
    placeholders = ",".join("?" for _ in run_ids)
    items = fetch_all(
        conn,
        f"""
        SELECT
            boundary_policy,
            boundary_agent_key,
            shadow_status,
            block_reason,
            text_delta_kind,
            COUNT(*) AS count
        FROM auto_confirmation_reopen_text_boundary_repair_shadow_items
        WHERE run_id IN ({placeholders})
        GROUP BY boundary_policy, boundary_agent_key, shadow_status, block_reason, text_delta_kind
        ORDER BY count DESC, boundary_policy, shadow_status
        """,
        tuple(run_ids),
    )
    totals = Counter()
    by_status = Counter()
    by_policy = Counter()
    by_delta = Counter()
    for row in rows:
        totals["policies"] += 1
        totals["candidates"] += int(row["total_candidates"] or 0)
        totals["shadow_ready"] += int(row["shadow_ready_count"] or 0)
        totals["blocked"] += int(row["blocked_count"] or 0)
        totals["no_text_delta"] += int(row["no_text_delta_count"] or 0)
        totals["validation_issue"] += int(row["validation_issue_count"] or 0)
        totals["same_token"] += int(row["same_token_count"] or 0)
    for row in items:
        count = int(row["count"] or 0)
        by_status[row["shadow_status"]] += count
        by_policy[row["boundary_policy"]] += count
        by_delta[row["text_delta_kind"]] += count
    return {
        "totals": dict(totals),
        "rows": rows,
        "items": items,
        "by_status": dict(by_status),
        "by_policy": dict(by_policy),
        "by_delta": dict(by_delta),
        "ready_rate": percent(totals["shadow_ready"], totals["candidates"]),
    }


def summarize_text_boundary_repair_checkpoints(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "auto_confirmation_reopen_text_boundary_repair_checkpoint_runs"):
        return None
    rows = fetch_all(
        conn,
        """
        SELECT run.*
        FROM auto_confirmation_reopen_text_boundary_repair_checkpoint_runs run
        JOIN (
            SELECT checkpoint_name, MAX(id) AS id
            FROM auto_confirmation_reopen_text_boundary_repair_checkpoint_runs
            WHERE finished_at IS NOT NULL
            GROUP BY checkpoint_name
        ) latest
          ON latest.id = run.id
        ORDER BY run.started_at DESC, run.id DESC
        """
    )
    if not rows:
        return None
    totals = Counter()
    for row in rows:
        totals["checkpoints"] += 1
        totals["candidates"] += int(row["total_candidates"] or 0)
        totals["ready"] += int(row["ready_count"] or 0)
        totals["blocked"] += int(row["blocked_count"] or 0)
        totals["allowed"] += int(row["checkpoint_allowed_count"] or 0)
        totals["checkpoint_blocked"] += int(row["checkpoint_blocked_count"] or 0)
    return {
        "totals": dict(totals),
        "rows": rows,
        "allowed_rate": percent(totals["allowed"], totals["candidates"]),
    }


def summarize_text_boundary_repair_lifecycle_policies(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "auto_confirmation_reopen_text_boundary_repair_lifecycle_runs"):
        return None
    rows = fetch_all(
        conn,
        """
        SELECT run.*
        FROM auto_confirmation_reopen_text_boundary_repair_lifecycle_runs run
        JOIN (
            SELECT policy_name, policy_status, MAX(id) AS id
            FROM auto_confirmation_reopen_text_boundary_repair_lifecycle_runs
            WHERE finished_at IS NOT NULL
            GROUP BY policy_name, policy_status
        ) latest
          ON latest.id = run.id
        ORDER BY run.started_at DESC, run.id DESC
        """
    )
    if not rows:
        return None
    run_ids = [int(row["id"]) for row in rows]
    placeholders = ",".join("?" for _ in run_ids)
    items = fetch_all(
        conn,
        f"""
        SELECT
            boundary_policy,
            boundary_agent_key,
            policy_allowed,
            block_reason,
            COUNT(*) AS count
        FROM auto_confirmation_reopen_text_boundary_repair_lifecycle_items
        WHERE run_id IN ({placeholders})
        GROUP BY boundary_policy, boundary_agent_key, policy_allowed, block_reason
        ORDER BY count DESC, boundary_policy
        """,
        tuple(run_ids),
    )
    totals = Counter()
    by_policy = Counter()
    by_status = Counter()
    for row in rows:
        totals["policies"] += 1
        totals["candidates"] += int(row["candidate_count"] or 0)
        totals["released"] += int(row["released_count"] or 0)
        totals["blocked"] += int(row["blocked_count"] or 0)
    for row in items:
        count = int(row["count"] or 0)
        by_policy[row["boundary_policy"]] += count
        by_status["released_shadow" if int(row["policy_allowed"] or 0) else (row["block_reason"] or "blocked")] += count
    return {
        "totals": dict(totals),
        "rows": rows,
        "items": items,
        "by_policy": dict(by_policy),
        "by_status": dict(by_status),
        "release_rate": percent(totals["released"], totals["candidates"]),
    }


def summarize_text_boundary_token_policy_bridges(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "auto_confirmation_reopen_text_boundary_token_policy_bridge_runs"):
        return None
    rows = fetch_all(
        conn,
        """
        SELECT run.*
        FROM auto_confirmation_reopen_text_boundary_token_policy_bridge_runs run
        JOIN (
            SELECT bridge_name, MAX(id) AS id
            FROM auto_confirmation_reopen_text_boundary_token_policy_bridge_runs
            WHERE finished_at IS NOT NULL
            GROUP BY bridge_name
        ) latest
          ON latest.id = run.id
        ORDER BY run.started_at DESC, run.id DESC
        """
    )
    if not rows:
        return None
    run_ids = [int(row["id"]) for row in rows]
    placeholders = ",".join("?" for _ in run_ids)
    items = fetch_all(
        conn,
        f"""
        SELECT
            bridge_status,
            policy_bucket,
            risk_level,
            boundary_policy,
            diff_kind,
            COUNT(*) AS count
        FROM auto_confirmation_reopen_text_boundary_token_policy_bridge_items
        WHERE run_id IN ({placeholders})
        GROUP BY bridge_status, policy_bucket, risk_level, boundary_policy, diff_kind
        ORDER BY count DESC, risk_level, policy_bucket
        """,
        tuple(run_ids),
    )
    totals = Counter()
    by_status = Counter()
    by_bucket = Counter()
    by_risk = Counter()
    by_policy = Counter()
    by_diff = Counter()
    for row in rows:
        totals["bridges"] += 1
        totals["candidates"] += int(row["total_candidates"] or 0)
        totals["token_change"] += int(row["token_change_count"] or 0)
        totals["same_token"] += int(row["same_token_count"] or 0)
        totals["review_required"] += int(row["review_required_count"] or 0)
        totals["blocked"] += int(row["blocked_count"] or 0)
        totals["critical"] += int(row["critical_count"] or 0)
        totals["high"] += int(row["high_count"] or 0)
        totals["medium"] += int(row["medium_count"] or 0)
        totals["low"] += int(row["low_count"] or 0)
    for row in items:
        count = int(row["count"] or 0)
        by_status[row["bridge_status"]] += count
        by_bucket[row["policy_bucket"]] += count
        by_risk[row["risk_level"]] += count
        by_policy[row["boundary_policy"]] += count
        by_diff[row["diff_kind"]] += count
    return {
        "totals": dict(totals),
        "rows": rows,
        "items": items,
        "by_status": dict(by_status),
        "by_bucket": dict(by_bucket),
        "by_risk": dict(by_risk),
        "by_policy": dict(by_policy),
        "by_diff": dict(by_diff),
        "review_rate": percent(totals["review_required"], totals["candidates"]),
    }


def summarize_text_boundary_token_subpolicy_shadows(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "auto_confirmation_reopen_text_boundary_token_subpolicy_shadow_runs"):
        return None
    rows = fetch_all(
        conn,
        """
        SELECT run.*
        FROM auto_confirmation_reopen_text_boundary_token_subpolicy_shadow_runs run
        JOIN (
            SELECT subpolicy_name, MAX(id) AS id
            FROM auto_confirmation_reopen_text_boundary_token_subpolicy_shadow_runs
            WHERE finished_at IS NOT NULL
            GROUP BY subpolicy_name
        ) latest
          ON latest.id = run.id
        ORDER BY run.started_at DESC, run.id DESC
        """
    )
    if not rows:
        return None
    run_ids = [int(row["id"]) for row in rows]
    placeholders = ",".join("?" for _ in run_ids)
    items = fetch_all(
        conn,
        f"""
        SELECT
            subpolicy_status,
            subpolicy_action,
            policy_bucket,
            boundary_policy,
            risk_level,
            COUNT(*) AS count
        FROM auto_confirmation_reopen_text_boundary_token_subpolicy_shadow_items
        WHERE run_id IN ({placeholders})
        GROUP BY subpolicy_status, subpolicy_action, policy_bucket, boundary_policy, risk_level
        ORDER BY count DESC, policy_bucket
        """,
        tuple(run_ids),
    )
    unique_rows = fetch_all(
        conn,
        f"""
        WITH attempts AS (
            SELECT
                bridge_item_id,
                segment_id,
                CASE
                    WHEN subpolicy_action LIKE 'would_accept_%' THEN 1
                    ELSE 0
                END AS is_ready
            FROM auto_confirmation_reopen_text_boundary_token_subpolicy_shadow_items
            WHERE run_id IN ({placeholders})
        ),
        per_bridge AS (
            SELECT
                bridge_item_id,
                segment_id,
                MAX(is_ready) AS has_ready,
                SUM(CASE WHEN is_ready = 1 THEN 1 ELSE 0 END) AS ready_attempts,
                SUM(CASE WHEN is_ready = 0 THEN 1 ELSE 0 END) AS blocked_attempts
            FROM attempts
            GROUP BY bridge_item_id, segment_id
        )
        SELECT
            COUNT(*) AS unique_bridge_items,
            SUM(has_ready) AS ready_unique,
            SUM(CASE WHEN has_ready = 0 THEN 1 ELSE 0 END) AS unresolved_unique,
            SUM(ready_attempts) AS ready_attempts,
            SUM(blocked_attempts) AS blocked_attempts,
            SUM(
                CASE
                    WHEN has_ready = 1 AND blocked_attempts > 0 THEN 1
                    ELSE 0
                END
            ) AS covered_items_with_rejected_attempts
        FROM per_bridge
        """,
        tuple(run_ids),
    )
    unique_unresolved_items = fetch_all(
        conn,
        f"""
        WITH attempts AS (
            SELECT
                item.bridge_item_id,
                item.segment_id,
                item.relative_path,
                item.source_key,
                item.policy_bucket,
                item.boundary_policy,
                item.risk_level,
                item.subpolicy_action,
                item.block_reason,
                run.subpolicy_name,
                CASE
                    WHEN item.subpolicy_action LIKE 'would_accept_%' THEN 1
                    ELSE 0
                END AS is_ready
            FROM auto_confirmation_reopen_text_boundary_token_subpolicy_shadow_items item
            JOIN auto_confirmation_reopen_text_boundary_token_subpolicy_shadow_runs run
              ON run.id = item.run_id
            WHERE item.run_id IN ({placeholders})
        ),
        unresolved AS (
            SELECT bridge_item_id
            FROM attempts
            GROUP BY bridge_item_id
            HAVING MAX(is_ready) = 0
        )
        SELECT
            attempts.bridge_item_id,
            attempts.segment_id,
            attempts.relative_path,
            attempts.source_key,
            GROUP_CONCAT(DISTINCT attempts.boundary_policy) AS boundary_policies,
            GROUP_CONCAT(DISTINCT attempts.policy_bucket) AS policy_buckets,
            GROUP_CONCAT(DISTINCT attempts.risk_level) AS risk_levels,
            GROUP_CONCAT(
                attempts.subpolicy_name || ':' || attempts.block_reason,
                ' || '
            ) AS blocked_attempts
        FROM attempts
        JOIN unresolved
          ON unresolved.bridge_item_id = attempts.bridge_item_id
        GROUP BY
            attempts.bridge_item_id,
            attempts.segment_id,
            attempts.relative_path,
            attempts.source_key
        ORDER BY attempts.bridge_item_id
        """,
        tuple(run_ids),
    )
    unique_summary = unique_rows[0] if unique_rows else {}
    totals = Counter()
    by_status = Counter()
    by_action = Counter()
    by_bucket = Counter()
    by_policy = Counter()
    by_risk = Counter()
    for row in rows:
        totals["subpolicies"] += 1
        totals["candidates"] += int(row["total_candidates"] or 0)
        totals["shadow_ready"] += int(row["shadow_ready_count"] or 0)
        totals["blocked"] += int(row["blocked_count"] or 0)
        totals["validation_issue"] += int(row["validation_issue_count"] or 0)
    for row in items:
        count = int(row["count"] or 0)
        by_status[row["subpolicy_status"]] += count
        by_action[row["subpolicy_action"]] += count
        by_bucket[row["policy_bucket"]] += count
        by_policy[row["boundary_policy"]] += count
        by_risk[row["risk_level"]] += count
    return {
        "totals": dict(totals),
        "rows": rows,
        "items": items,
        "by_status": dict(by_status),
        "by_action": dict(by_action),
        "by_bucket": dict(by_bucket),
        "by_policy": dict(by_policy),
        "by_risk": dict(by_risk),
        "unique_bridge_coverage": unique_summary,
        "unique_unresolved_items": unique_unresolved_items,
        "ready_rate": percent(totals["shadow_ready"], totals["candidates"]),
        "unique_ready_rate": percent(
            int(unique_summary.get("ready_unique") or 0),
            int(unique_summary.get("unique_bridge_items") or 0),
        ),
    }


def summarize_text_boundary_token_subpolicy_checkpoints(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint_runs"):
        return None
    rows = fetch_all(
        conn,
        """
        SELECT run.*
        FROM auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint_runs run
        JOIN (
            SELECT checkpoint_name, subpolicy_name, MAX(id) AS id
            FROM auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint_runs
            WHERE finished_at IS NOT NULL
            GROUP BY checkpoint_name, subpolicy_name
        ) latest
          ON latest.id = run.id
        ORDER BY run.started_at DESC, run.id DESC
        """
    )
    if not rows:
        return None
    run_ids = [int(row["id"]) for row in rows]
    placeholders = ",".join("?" for _ in run_ids)
    bucket_items = fetch_all(
        conn,
        f"""
        SELECT
            policy_bucket,
            checkpoint_allowed,
            COUNT(*) AS count
        FROM auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint_items
        WHERE checkpoint_run_id IN ({placeholders})
        GROUP BY policy_bucket, checkpoint_allowed
        ORDER BY count DESC, policy_bucket
        """,
        tuple(run_ids),
    )
    totals = Counter()
    by_status = Counter()
    by_subpolicy = Counter()
    by_bucket = Counter()
    by_bucket_allowed = Counter()
    for row in rows:
        totals["checkpoints"] += 1
        totals["candidates"] += int(row["total_candidates"] or 0)
        totals["ready"] += int(row["ready_count"] or 0)
        totals["blocked"] += int(row["blocked_count"] or 0)
        totals["allowed"] += int(row["checkpoint_allowed_count"] or 0)
        totals["checkpoint_blocked"] += int(row["checkpoint_blocked_count"] or 0)
        totals["validation_issue"] += int(row["validation_issue_count"] or 0)
        by_status[row["checkpoint_status"]] += 1
        by_subpolicy[row["subpolicy_name"]] += int(row["checkpoint_allowed_count"] or 0)
    for row in bucket_items:
        count = int(row["count"] or 0)
        by_bucket[row["policy_bucket"]] += count
        if int(row["checkpoint_allowed"] or 0):
            by_bucket_allowed[row["policy_bucket"]] += count
    return {
        "totals": dict(totals),
        "rows": rows,
        "by_status": dict(by_status),
        "by_subpolicy": dict(by_subpolicy),
        "by_bucket": dict(by_bucket),
        "by_bucket_allowed": dict(by_bucket_allowed),
        "allowed_rate": percent(totals["allowed"], totals["candidates"]),
    }


def summarize_text_policy_checkpoints(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "auto_confirmation_reopen_text_policy_checkpoint_runs"):
        return None
    rows = fetch_all(
        conn,
        """
        SELECT run.*
        FROM auto_confirmation_reopen_text_policy_checkpoint_runs run
        JOIN (
            SELECT checkpoint_name, MAX(id) AS id
            FROM auto_confirmation_reopen_text_policy_checkpoint_runs
            WHERE finished_at IS NOT NULL
            GROUP BY checkpoint_name
        ) latest
          ON latest.id = run.id
        ORDER BY run.started_at DESC, run.id DESC
        """
    )
    if not rows:
        return None
    totals = Counter()
    for row in rows:
        totals["checkpoints"] += 1
        totals["candidates"] += int(row["total_candidates"] or 0)
        totals["allowed"] += int(row["checkpoint_allowed_count"] or 0)
        totals["blocked"] += int(row["checkpoint_blocked_count"] or 0)
        totals["production_release_allowed"] += int(row["production_release_allowed"] or 0)
    return {
        "totals": dict(totals),
        "rows": rows,
        "allowed_rate": percent(totals["allowed"], totals["candidates"]),
    }


def summarize_text_boundary_token_subpolicy_lifecycle_policies(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_runs"):
        return None
    rows = fetch_all(
        conn,
        """
        SELECT run.*
        FROM auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_runs run
        JOIN (
            SELECT policy_name, MAX(id) AS id
            FROM auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_runs
            WHERE finished_at IS NOT NULL
            GROUP BY policy_name
        ) latest
          ON latest.id = run.id
        ORDER BY run.started_at DESC, run.id DESC
        """
    )
    if not rows:
        return None
    run_ids = [int(row["id"]) for row in rows]
    placeholders = ",".join("?" for _ in run_ids)
    items = fetch_all(
        conn,
        f"""
        SELECT
            subpolicy_name,
            policy_bucket,
            boundary_policy,
            policy_allowed,
            COUNT(*) AS count
        FROM auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_items
        WHERE run_id IN ({placeholders})
        GROUP BY subpolicy_name, policy_bucket, boundary_policy, policy_allowed
        ORDER BY count DESC, subpolicy_name
        """,
        tuple(run_ids),
    )
    totals = Counter()
    by_status = Counter()
    by_subpolicy = Counter()
    by_bucket = Counter()
    by_boundary = Counter()
    for row in rows:
        totals["policies"] += 1
        totals["candidates"] += int(row["candidate_count"] or 0)
        totals["released"] += int(row["released_count"] or 0)
        totals["blocked"] += int(row["blocked_count"] or 0)
        totals["checkpoints"] += int(row["checkpoint_count"] or 0)
        totals["subpolicies"] += int(row["subpolicy_count"] or 0)
        by_status[row["policy_status"]] += 1
    for row in items:
        if int(row["policy_allowed"] or 0) != 1:
            continue
        count = int(row["count"] or 0)
        by_subpolicy[row["subpolicy_name"]] += count
        by_bucket[row["policy_bucket"]] += count
        by_boundary[row["boundary_policy"]] += count
    return {
        "totals": dict(totals),
        "rows": rows,
        "items": items,
        "by_status": dict(by_status),
        "by_subpolicy": dict(by_subpolicy),
        "by_bucket": dict(by_bucket),
        "by_boundary": dict(by_boundary),
        "release_rate": percent(totals["released"], totals["candidates"]),
    }


def summarize_text_boundary_token_subpolicy_production_audits(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit_runs"):
        return None
    rows = fetch_all(
        conn,
        """
        SELECT run.*
        FROM auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit_runs run
        JOIN (
            SELECT audit_name, MAX(id) AS id
            FROM auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit_runs
            WHERE finished_at IS NOT NULL
            GROUP BY audit_name
        ) latest
          ON latest.id = run.id
        ORDER BY run.started_at DESC, run.id DESC
        """
    )
    if not rows:
        return None
    run_ids = [int(row["id"]) for row in rows]
    placeholders = ",".join("?" for _ in run_ids)
    items = fetch_all(
        conn,
        f"""
        SELECT
            subpolicy_name,
            policy_bucket,
            eligible_controlled_production,
            estimated_closed_gain,
            COUNT(*) AS count
        FROM auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit_items
        WHERE run_id IN ({placeholders})
        GROUP BY subpolicy_name, policy_bucket, eligible_controlled_production, estimated_closed_gain
        ORDER BY count DESC, subpolicy_name
        """,
        tuple(run_ids),
    )
    block_items = fetch_all(
        conn,
        f"""
        SELECT
            COALESCE(block_reason, '') AS block_reason,
            COALESCE(current_state_group, '') AS current_state_group,
            COALESCE(current_final_state, '') AS current_final_state,
            COALESCE(current_apply_state, '') AS current_apply_state,
            COALESCE(subpolicy_name, '') AS subpolicy_name,
            COALESCE(policy_bucket, '') AS policy_bucket,
            COUNT(*) AS count
        FROM auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit_items
        WHERE run_id IN ({placeholders})
          AND eligible_controlled_production = 0
          AND COALESCE(block_reason, '') <> ''
        GROUP BY
            block_reason,
            current_state_group,
            current_final_state,
            current_apply_state,
            subpolicy_name,
            policy_bucket
        ORDER BY count DESC, block_reason, subpolicy_name
        """,
        tuple(run_ids),
    )
    actionable_items = fetch_all(
        conn,
        f"""
        SELECT
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.subpolicy_name,
            item.policy_bucket,
            item.boundary_policy,
            item.current_final_state,
            item.current_apply_state,
            item.current_state_group,
            item.block_reason,
            item.requires_confirmation_promotion,
            item.requires_output_apply,
            item.requires_segment_state_lifecycle_integration,
            CASE
                WHEN item.block_reason = 'current_confirmation_output_mismatch'
                 AND sc.confirmed_text IS NOT NULL
                 AND o.portuguese_text IS NOT NULL
                 AND REPLACE(o.portuguese_text, '\\\"', '"') = sc.confirmed_text
                THEN 1
                ELSE 0
            END AS quote_escape_only_mismatch
        FROM auto_confirmation_reopen_text_boundary_token_subpolicy_production_audit_items item
        LEFT JOIN segment_confirmations sc
          ON sc.segment_id = item.segment_id
        LEFT JOIN output_segments o
          ON o.segment_id = item.segment_id
        WHERE item.run_id IN ({placeholders})
          AND item.eligible_controlled_production = 0
          AND COALESCE(item.block_reason, '') <> ''
          AND item.block_reason <> 'already_applied'
        ORDER BY item.relative_path, item.source_key, item.subpolicy_name
        """,
        tuple(run_ids),
    )
    totals = Counter()
    by_status = Counter()
    by_subpolicy_eligible = Counter()
    by_bucket_eligible = Counter()
    by_subpolicy_gain = Counter()
    by_block_reason = Counter()
    for row in rows:
        totals["audits"] += 1
        totals["candidates"] += int(row["candidate_count"] or 0)
        totals["eligible"] += int(row["eligible_controlled_production_count"] or 0)
        totals["estimated_closed_gain"] += int(row["estimated_closed_gain_count"] or 0)
        totals["blocked"] += int(row["blocked_count"] or 0)
        totals["requires_confirmation_promotion"] += int(row["requires_confirmation_promotion_count"] or 0)
        totals["requires_output_apply"] += int(row["requires_output_apply_count"] or 0)
        totals["requires_segment_state_lifecycle_integration"] += int(row["requires_segment_state_lifecycle_integration_count"] or 0)
        by_status[row["audit_status"]] += 1
    for row in block_items:
        count = int(row["count"] or 0)
        reason = row["block_reason"] or "unknown"
        by_block_reason[reason] += count
        if reason == "already_applied":
            totals["already_applied_non_eligible"] += count
        else:
            totals["actionable_blocked"] += count
    totals["quote_escape_only_blocked"] = sum(
        1 for row in actionable_items if int(row.get("quote_escape_only_mismatch") or 0)
    )
    for row in items:
        count = int(row["count"] or 0)
        if int(row["eligible_controlled_production"] or 0):
            by_subpolicy_eligible[row["subpolicy_name"]] += count
            by_bucket_eligible[row["policy_bucket"]] += count
        if int(row["estimated_closed_gain"] or 0):
            by_subpolicy_gain[row["subpolicy_name"]] += count
    return {
        "totals": dict(totals),
        "rows": rows,
        "items": items,
        "block_items": block_items,
        "actionable_items": actionable_items,
        "by_status": dict(by_status),
        "by_subpolicy_eligible": dict(by_subpolicy_eligible),
        "by_bucket_eligible": dict(by_bucket_eligible),
        "by_subpolicy_gain": dict(by_subpolicy_gain),
        "by_block_reason": dict(by_block_reason),
        "eligible_rate": percent(totals["eligible"], totals["candidates"]),
        "gain_rate": percent(totals["estimated_closed_gain"], totals["candidates"]),
    }


def summarize_text_boundary_repair_production_audits(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "auto_confirmation_reopen_text_boundary_repair_production_audit_runs"):
        return None
    rows = fetch_all(
        conn,
        """
        SELECT run.*
        FROM auto_confirmation_reopen_text_boundary_repair_production_audit_runs run
        JOIN (
            SELECT audit_name, MAX(id) AS id
            FROM auto_confirmation_reopen_text_boundary_repair_production_audit_runs
            WHERE finished_at IS NOT NULL
            GROUP BY audit_name
        ) latest
          ON latest.id = run.id
        ORDER BY run.started_at DESC, run.id DESC
        """
    )
    if not rows:
        return None
    run_ids = [int(row["id"]) for row in rows]
    placeholders = ",".join("?" for _ in run_ids)
    items = fetch_all(
        conn,
        f"""
        SELECT
            boundary_policy,
            shadow_action,
            text_delta_kind,
            eligible_controlled_production,
            estimated_closed_gain,
            COUNT(*) AS count
        FROM auto_confirmation_reopen_text_boundary_repair_production_audit_items
        WHERE run_id IN ({placeholders})
        GROUP BY boundary_policy, shadow_action, text_delta_kind, eligible_controlled_production, estimated_closed_gain
        ORDER BY count DESC, boundary_policy
        """,
        tuple(run_ids),
    )
    block_items = fetch_all(
        conn,
        f"""
        SELECT
            COALESCE(block_reason, '') AS block_reason,
            COALESCE(current_state_group, '') AS current_state_group,
            COALESCE(current_final_state, '') AS current_final_state,
            COALESCE(current_apply_state, '') AS current_apply_state,
            COUNT(*) AS count
        FROM auto_confirmation_reopen_text_boundary_repair_production_audit_items
        WHERE run_id IN ({placeholders})
          AND eligible_controlled_production = 0
          AND COALESCE(block_reason, '') <> ''
        GROUP BY block_reason, current_state_group, current_final_state, current_apply_state
        ORDER BY count DESC, block_reason, current_state_group
        """,
        tuple(run_ids),
    )
    totals = Counter()
    by_status = Counter()
    by_policy_eligible = Counter()
    by_policy_gain = Counter()
    by_action = Counter()
    by_delta = Counter()
    by_block_reason = Counter()
    for row in rows:
        totals["audits"] += 1
        totals["candidates"] += int(row["candidate_count"] or 0)
        totals["eligible"] += int(row["eligible_controlled_production_count"] or 0)
        totals["estimated_closed_gain"] += int(row["estimated_closed_gain_count"] or 0)
        totals["blocked"] += int(row["blocked_count"] or 0)
        totals["real_repair"] += int(row["real_repair_count"] or 0)
        totals["noop_observation"] += int(row["noop_observation_count"] or 0)
        totals["requires_confirmation_promotion"] += int(row["requires_confirmation_promotion_count"] or 0)
        totals["requires_output_apply"] += int(row["requires_output_apply_count"] or 0)
        totals["requires_segment_state_lifecycle_integration"] += int(row["requires_segment_state_lifecycle_integration_count"] or 0)
        by_status[row["audit_status"]] += 1
    for row in items:
        count = int(row["count"] or 0)
        by_action[row["shadow_action"]] += count
        by_delta[row["text_delta_kind"]] += count
        if int(row["eligible_controlled_production"] or 0):
            by_policy_eligible[row["boundary_policy"]] += count
        if int(row["estimated_closed_gain"] or 0):
            by_policy_gain[row["boundary_policy"]] += count
    for row in block_items:
        count = int(row["count"] or 0)
        reason = row["block_reason"] or "unknown"
        by_block_reason[reason] += count
        if row["current_state_group"] != "closed" and reason != "real_repair_already_applied":
            totals["actionable_blocked"] += count
    return {
        "totals": dict(totals),
        "rows": rows,
        "items": items,
        "block_items": block_items,
        "by_status": dict(by_status),
        "by_policy_eligible": dict(by_policy_eligible),
        "by_policy_gain": dict(by_policy_gain),
        "by_action": dict(by_action),
        "by_delta": dict(by_delta),
        "by_block_reason": dict(by_block_reason),
        "eligible_rate": percent(totals["eligible"], totals["candidates"]),
        "gain_rate": percent(totals["estimated_closed_gain"], totals["candidates"]),
    }


def summarize_reopen_lifecycle_policies(conn) -> dict[str, Any] | None:
    rows = fetch_all(
        conn,
        """
        SELECT run.*
        FROM auto_confirmation_reopen_lifecycle_policy_runs run
        JOIN (
            SELECT policy_name, MAX(id) AS id
            FROM auto_confirmation_reopen_lifecycle_policy_runs
            WHERE finished_at IS NOT NULL
              AND policy_status = 'active'
              AND released_count > 0
            GROUP BY policy_name
        ) latest
          ON latest.id = run.id
        ORDER BY run.policy_name
        """
    )
    if not rows:
        return None
    totals = Counter()
    for row in rows:
        totals["policies"] += 1
        totals["candidates"] += int(row["candidate_count"] or 0)
        totals["released"] += int(row["released_count"] or 0)
        totals["blocked"] += int(row["blocked_count"] or 0)
    return {
        "totals": dict(totals),
        "rows": rows,
        "release_rate": percent(totals["released"], totals["candidates"]),
    }


def summarize_divine_realm_symbolic_policy(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "religion_divine_realm_symbolic_policy_runs"):
        return None
    run = fetch_one(
        conn,
        """
        SELECT *
        FROM religion_divine_realm_symbolic_policy_runs
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if run is None:
        return None
    statuses = fetch_all(
        conn,
        """
        SELECT policy_status, COUNT(*) AS count
        FROM religion_divine_realm_symbolic_policy_items
        WHERE run_id = ?
        GROUP BY policy_status
        ORDER BY count DESC, policy_status
        """,
        (run["id"],),
    )
    families = fetch_all(
        conn,
        """
        SELECT text_family, COUNT(*) AS count
        FROM religion_divine_realm_symbolic_policy_items
        WHERE run_id = ?
        GROUP BY text_family
        ORDER BY count DESC, text_family
        """,
        (run["id"],),
    )
    blocked = fetch_all(
        conn,
        """
        SELECT *
        FROM religion_divine_realm_symbolic_policy_items
        WHERE run_id = ?
          AND policy_status NOT LIKE 'shadow_ready%'
        ORDER BY relative_path, source_line_number, source_key
        LIMIT 20
        """,
        (run["id"],),
    )
    return {
        "run": run,
        "ready_rate": percent(int(run["shadow_ready_count"] or 0), int(run["total_rows"] or 0)),
        "statuses": statuses,
        "families": families,
        "blocked": blocked,
    }


def summarize_recommendations(conn) -> dict[str, Any] | None:
    run_id = latest_id(conn, "ml_agent_recommendations", "1 = 1")
    if run_id is None:
        return None
    latest_run = fetch_one(
        conn,
        """
        SELECT run_id
        FROM ml_agent_recommendations
        ORDER BY COALESCE(run_id, 0) DESC, id DESC
        LIMIT 1
        """,
    )
    selected_run_id = latest_run["run_id"] if latest_run else None
    if selected_run_id is None:
        rows = fetch_all(conn, "SELECT * FROM ml_agent_recommendations ORDER BY id DESC LIMIT 20")
    else:
        rows = fetch_all(
            conn,
            """
            SELECT *
            FROM ml_agent_recommendations
            WHERE run_id = ?
            ORDER BY evidence_count DESC, proposed_agent_key
            """,
            (selected_run_id,),
        )
    totals = Counter()
    for row in rows:
        totals["evidence"] += int(row["evidence_count"] or 0)
        totals["positive"] += int(row["positive_count"] or 0)
        totals["negative"] += int(row["negative_count"] or 0)
        totals["corrected"] += int(row["corrected_count"] or 0)
    return {"run_id": selected_run_id, "totals": dict(totals), "rows": rows}


def summarize_specialist_policy(conn) -> dict[str, Any] | None:
    latest = fetch_one(
        conn,
        """
        SELECT created_at
        FROM ml_specialist_policy_snapshots
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
    )
    if latest is None:
        return None
    rows = fetch_all(
        conn,
        """
        SELECT *
        FROM ml_specialist_policy_snapshots
        WHERE created_at = ?
        ORDER BY status, specialist_key
        """,
        (latest["created_at"],),
    )
    status_counts = Counter(row["status"] for row in rows)
    safety_blocks = [
        row
        for row in rows
        if row["status"] in {"MODEL_SAFETY_FAIL", "MODEL_HOLDOUT_SAFETY_FAIL"}
    ]
    ready = [row for row in rows if str(row["status"]).startswith("READY")]
    return {
        "created_at": latest["created_at"],
        "status_counts": dict(status_counts),
        "ready_count": len(ready),
        "safety_block_count": len(safety_blocks),
        "rows": rows,
        "safety_blocks": safety_blocks,
    }


def summarize_boundary_correction_queues(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "auto_confirmation_reopen_text_review_queue_runs"):
        return None
    row = fetch_one(
        conn,
        """
        SELECT *
        FROM auto_confirmation_reopen_text_review_queue_runs
        WHERE agent_key = 'boundary_correction_specialist'
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
    )
    if row is None:
        return None
    items = fetch_all(
        conn,
        """
        SELECT
            item.suggested_agent_key,
            item.text_subfamily,
            item.suggested_review_label,
            item.risk_level,
            COUNT(*) AS count,
            SUM(
                CASE
                    WHEN decision.id IS NULL THEN 1
                    ELSE 0
                END
            ) AS pending_count,
            SUM(
                CASE
                    WHEN decision.id IS NOT NULL THEN 1
                    ELSE 0
                END
            ) AS reviewed_count
        FROM auto_confirmation_reopen_text_review_queue_items item
        LEFT JOIN auto_confirmation_reopen_text_review_decisions decision
          ON decision.queue_item_id = item.id
        WHERE item.run_id = ?
        GROUP BY item.suggested_agent_key, item.text_subfamily, item.suggested_review_label, item.risk_level
        ORDER BY count DESC, item.suggested_agent_key, item.suggested_review_label
        """,
        (row["id"],),
    )
    totals = Counter()
    by_agent = Counter()
    by_label = Counter()
    by_risk = Counter()
    for item in items:
        count = int(item["count"] or 0)
        pending = int(item["pending_count"] or 0)
        reviewed = int(item["reviewed_count"] or 0)
        totals["items"] += count
        totals["pending"] += pending
        totals["reviewed"] += reviewed
        by_agent[item["suggested_agent_key"]] += count
        by_label[item["suggested_review_label"]] += count
        by_risk[item["risk_level"]] += count
    return {
        "run": row,
        "totals": dict(totals),
        "items": items,
        "by_agent": dict(by_agent),
        "by_label": dict(by_label),
        "by_risk": dict(by_risk),
        "review_rate": percent(totals["reviewed"], totals["items"]),
    }


def summarize_segment_state(conn) -> dict[str, Any] | None:
    run_id = latest_id(conn, "segment_state_runs", "finished_at IS NOT NULL")
    if run_id is None:
        return None
    return fetch_one(conn, "SELECT * FROM segment_state_runs WHERE id = ?", (run_id,))


def summarize_active_policy_gap(conn) -> dict[str, Any] | None:
    """Summarize rows where the legacy active score is safer than the current policy."""
    state_run = summarize_segment_state(conn)
    if not state_run:
        return None
    run_id = int(state_run["id"])
    active_score_run_id = state_run.get("active_score_run_id")
    candidate_score_run_id = state_run.get("candidate_score_run_id")
    policy_run_id = state_run.get("policy_run_id")
    if not active_score_run_id or not candidate_score_run_id or not policy_run_id:
        return None

    gap_filter = """
        ssi.run_id = ?
        AND ssi.state_group = 'pending'
        AND ssi.active_action = 'auto_safe'
        AND ssi.policy_action = 'needs_autofix'
    """
    totals = fetch_one(
        conn,
        f"""
        SELECT
            COUNT(*) AS total,
            ROUND(AVG(active.model_safe_probability), 6) AS active_avg_safe_probability,
            ROUND(MIN(active.model_safe_probability), 6) AS active_min_safe_probability,
            ROUND(MAX(active.model_safe_probability), 6) AS active_max_safe_probability,
            ROUND(AVG(candidate.model_safe_probability), 6) AS candidate_avg_safe_probability,
            ROUND(MIN(candidate.model_safe_probability), 6) AS candidate_min_safe_probability,
            ROUND(MAX(candidate.model_safe_probability), 6) AS candidate_max_safe_probability,
            SUM(policy.learned_positive) AS learned_positive,
            SUM(policy.learned_negative) AS learned_negative
        FROM segment_state_items ssi
        JOIN ml_score_items active
          ON active.segment_id = ssi.segment_id
         AND active.run_id = ?
        JOIN ml_score_items candidate
          ON candidate.segment_id = ssi.segment_id
         AND candidate.run_id = ?
        JOIN ml_policy_items policy
          ON policy.segment_id = ssi.segment_id
         AND policy.run_id = ?
        WHERE {gap_filter}
        """,
        (
            active_score_run_id,
            candidate_score_run_id,
            policy_run_id,
            run_id,
        ),
    )
    if not totals or int(totals.get("total") or 0) == 0:
        return {
            "segment_state_run_id": run_id,
            "active_score_run_id": active_score_run_id,
            "candidate_score_run_id": candidate_score_run_id,
            "policy_run_id": policy_run_id,
            "totals": {"total": 0},
            "by_path": [],
            "candidate_probability_buckets": [],
            "by_policy_group": [],
            "by_confirmation": [],
            "by_key_shape": [],
            "top_near_threshold": [],
            "policy_safe_candidate_autofix_blocks": [],
            "policy_safe_candidate_autofix_samples": [],
        }

    by_path = fetch_all(
        conn,
        f"""
        SELECT
            ssi.relative_path,
            COUNT(*) AS count,
            ROUND(AVG(active.model_safe_probability), 6) AS active_avg_safe_probability,
            ROUND(AVG(candidate.model_safe_probability), 6) AS candidate_avg_safe_probability,
            SUM(policy.learned_positive) AS learned_positive,
            SUM(policy.learned_negative) AS learned_negative
        FROM segment_state_items ssi
        JOIN ml_score_items active
          ON active.segment_id = ssi.segment_id
         AND active.run_id = ?
        JOIN ml_score_items candidate
          ON candidate.segment_id = ssi.segment_id
         AND candidate.run_id = ?
        JOIN ml_policy_items policy
          ON policy.segment_id = ssi.segment_id
         AND policy.run_id = ?
        WHERE {gap_filter}
        GROUP BY ssi.relative_path
        ORDER BY count DESC, ssi.relative_path
        LIMIT 30
        """,
        (
            active_score_run_id,
            candidate_score_run_id,
            policy_run_id,
            run_id,
        ),
    )
    buckets = fetch_all(
        conn,
        f"""
        SELECT
            CASE
                WHEN candidate.model_safe_probability < 0.10 THEN '<0.10'
                WHEN candidate.model_safe_probability < 0.25 THEN '0.10-0.25'
                WHEN candidate.model_safe_probability < 0.50 THEN '0.25-0.50'
                WHEN candidate.model_safe_probability < 0.70 THEN '0.50-0.70'
                WHEN candidate.model_safe_probability < 0.85 THEN '0.70-0.85'
                ELSE '>=0.85'
            END AS bucket,
            COUNT(*) AS count,
            ROUND(AVG(candidate.model_safe_probability), 6) AS avg_safe_probability
        FROM segment_state_items ssi
        JOIN ml_score_items candidate
          ON candidate.segment_id = ssi.segment_id
         AND candidate.run_id = ?
        WHERE {gap_filter}
        GROUP BY bucket
        ORDER BY MIN(candidate.model_safe_probability)
        """,
        (candidate_score_run_id, run_id),
    )
    by_policy_group = fetch_all(
        conn,
        f"""
        SELECT
            policy.policy_group,
            policy.policy_action,
            policy.score_final_action,
            COUNT(*) AS count,
            SUM(policy.learned_positive) AS learned_positive,
            SUM(policy.learned_negative) AS learned_negative,
            ROUND(AVG(policy.model_safe_probability), 6) AS avg_safe_probability
        FROM segment_state_items ssi
        JOIN ml_policy_items policy
          ON policy.segment_id = ssi.segment_id
         AND policy.run_id = ?
        WHERE {gap_filter}
        GROUP BY policy.policy_group, policy.policy_action, policy.score_final_action
        ORDER BY count DESC, policy.policy_group
        """,
        (policy_run_id, run_id),
    )
    by_confirmation = fetch_all(
        conn,
        f"""
        SELECT
            COALESCE(sc.confirmation_source, '') AS confirmation_source,
            COALESCE(sc.confirmation_label, '') AS confirmation_label,
            COALESCE(sc.locked, 0) AS locked,
            COUNT(*) AS count
        FROM segment_state_items ssi
        LEFT JOIN segment_confirmations sc ON sc.segment_id = ssi.segment_id
        WHERE {gap_filter}
        GROUP BY
            COALESCE(sc.confirmation_source, ''),
            COALESCE(sc.confirmation_label, ''),
            COALESCE(sc.locked, 0)
        ORDER BY count DESC
        LIMIT 20
        """,
        (run_id,),
    )
    by_key_shape = fetch_all(
        conn,
        f"""
        SELECT
            CASE
                WHEN ssi.source_key LIKE '%_adj' THEN 'adj_key'
                WHEN ssi.source_key LIKE '%_name' THEN 'name_key'
                WHEN ssi.source_key GLOB 'c_*' THEN 'county_key'
                WHEN ssi.source_key GLOB 'b_*' THEN 'barony_key'
                WHEN ssi.source_key GLOB 'k_*' THEN 'kingdom_key'
                WHEN ssi.source_key GLOB 'e_*' THEN 'e_prefix_key'
                WHEN ssi.source_key GLOB 'd_*' THEN 'decision_key'
                ELSE 'other'
            END AS key_shape,
            COUNT(*) AS count
        FROM segment_state_items ssi
        WHERE {gap_filter}
        GROUP BY key_shape
        ORDER BY count DESC, key_shape
        """,
        (run_id,),
    )
    top_near_threshold = fetch_all(
        conn,
        f"""
        SELECT
            ssi.segment_id,
            ssi.relative_path,
            ssi.source_key,
            active.model_safe_probability AS active_safe_probability,
            candidate.model_safe_probability AS candidate_safe_probability,
            candidate.reasons_json AS candidate_reasons_json
        FROM segment_state_items ssi
        JOIN ml_score_items active
          ON active.segment_id = ssi.segment_id
         AND active.run_id = ?
        JOIN ml_score_items candidate
          ON candidate.segment_id = ssi.segment_id
         AND candidate.run_id = ?
        WHERE {gap_filter}
        ORDER BY candidate.model_safe_probability DESC, ssi.relative_path, ssi.source_key
        LIMIT 20
        """,
        (active_score_run_id, candidate_score_run_id, run_id),
    )
    policy_safe_candidate_autofix_blocks = fetch_all(
        conn,
        """
        SELECT
            policy.policy_group,
            ssi.relative_path,
            COUNT(*) AS count,
            SUM(CASE WHEN ssi.active_action = 'auto_safe' THEN 1 ELSE 0 END) AS active_auto_safe_count,
            SUM(CASE WHEN ssi.active_action = 'needs_human' THEN 1 ELSE 0 END) AS active_needs_human_count,
            SUM(policy.learned_positive) AS learned_positive,
            SUM(policy.learned_negative) AS learned_negative,
            ROUND(AVG(policy.model_safe_probability), 6) AS avg_safe_probability
        FROM segment_state_items ssi
        JOIN ml_policy_items policy
          ON policy.segment_id = ssi.segment_id
         AND policy.run_id = ?
        WHERE ssi.run_id = ?
          AND ssi.state_group = 'pending'
          AND ssi.candidate_action = 'needs_autofix'
          AND ssi.policy_action = 'auto_safe'
          AND policy.new_safe = 1
          AND policy.learned_positive = 1
          AND policy.learned_negative = 0
        GROUP BY policy.policy_group, ssi.relative_path
        ORDER BY count DESC, policy.policy_group, ssi.relative_path
        """,
        (policy_run_id, run_id),
    )
    policy_safe_candidate_autofix_samples = fetch_all(
        conn,
        """
        SELECT
            ssi.segment_id,
            ssi.relative_path,
            ssi.source_key,
            ssi.active_action,
            ssi.candidate_action,
            ssi.policy_action,
            policy.policy_group,
            policy.model_safe_probability,
            policy.learned_positive,
            policy.learned_negative,
            policy.reasons_json
        FROM segment_state_items ssi
        JOIN ml_policy_items policy
          ON policy.segment_id = ssi.segment_id
         AND policy.run_id = ?
        WHERE ssi.run_id = ?
          AND ssi.state_group = 'pending'
          AND ssi.candidate_action = 'needs_autofix'
          AND ssi.policy_action = 'auto_safe'
          AND policy.new_safe = 1
          AND policy.learned_positive = 1
          AND policy.learned_negative = 0
        ORDER BY policy.policy_group, policy.model_safe_probability DESC, ssi.relative_path, ssi.source_key
        LIMIT 40
        """,
        (policy_run_id, run_id),
    )

    return {
        "segment_state_run_id": run_id,
        "active_score_run_id": active_score_run_id,
        "candidate_score_run_id": candidate_score_run_id,
        "policy_run_id": policy_run_id,
        "totals": totals,
        "by_path": by_path,
        "candidate_probability_buckets": buckets,
        "by_policy_group": by_policy_group,
        "by_confirmation": by_confirmation,
        "by_key_shape": by_key_shape,
        "top_near_threshold": top_near_threshold,
        "policy_safe_candidate_autofix_blocks": policy_safe_candidate_autofix_blocks,
        "policy_safe_candidate_autofix_samples": policy_safe_candidate_autofix_samples,
    }


def summarize_issue_ledger(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_ledger_runs") or not table_exists(conn, "ml_issue_ledger_items"):
        return None
    run_id = latest_id(conn, "ml_issue_ledger_runs", "finished_at IS NOT NULL")
    if run_id is None:
        return None
    run = fetch_one(conn, "SELECT * FROM ml_issue_ledger_runs WHERE id = ?", (run_id,))
    if not run:
        return None
    by_family = fetch_all(
        conn,
        """
        SELECT issue_family, COUNT(*) AS count, COUNT(DISTINCT segment_id) AS segment_count
        FROM ml_issue_ledger_items
        WHERE run_id = ?
        GROUP BY issue_family
        ORDER BY count DESC
        LIMIT 20
        """,
        (run_id,),
    )
    by_agent = fetch_all(
        conn,
        """
        SELECT agent_key, COUNT(*) AS count, COUNT(DISTINCT segment_id) AS segment_count
        FROM ml_issue_ledger_items
        WHERE run_id = ?
        GROUP BY agent_key
        ORDER BY count DESC
        LIMIT 20
        """,
        (run_id,),
    )
    by_status = fetch_all(
        conn,
        """
        SELECT status, route_status, COUNT(*) AS count
        FROM ml_issue_ledger_items
        WHERE run_id = ?
        GROUP BY status, route_status
        ORDER BY count DESC
        """,
        (run_id,),
    )
    by_role = fetch_all(
        conn,
        """
        SELECT issue_role, issue_severity, COUNT(*) AS count
        FROM ml_issue_ledger_items
        WHERE run_id = ?
        GROUP BY issue_role, issue_severity
        ORDER BY count DESC
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_family": by_family,
        "by_agent": by_agent,
        "by_status": by_status,
        "by_role": by_role,
    }


def summarize_issue_partial_coverage(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_partial_coverage_runs") or not table_exists(
        conn,
        "ml_issue_partial_coverage_items",
    ):
        return None
    run_id = latest_id(conn, "ml_issue_partial_coverage_runs", "finished_at IS NOT NULL")
    if run_id is None:
        return None
    run = fetch_one(conn, "SELECT * FROM ml_issue_partial_coverage_runs WHERE id = ?", (run_id,))
    if not run:
        return None
    by_state = fetch_all(
        conn,
        """
        SELECT coverage_state, COUNT(*) AS segment_count, SUM(total_issue_count) AS issue_count
        FROM ml_issue_partial_coverage_items
        WHERE run_id = ?
        GROUP BY coverage_state
        ORDER BY segment_count DESC, coverage_state
        """,
        (run_id,),
    )
    by_review = fetch_all(
        conn,
        """
        SELECT review_state, COUNT(*) AS segment_count, SUM(reviewed_issue_count) AS reviewed_issue_count
        FROM ml_issue_partial_coverage_items
        WHERE run_id = ?
        GROUP BY review_state
        ORDER BY segment_count DESC, review_state
        """,
        (run_id,),
    )
    partial_samples = fetch_all(
        conn,
        """
        SELECT
            relative_path,
            source_key,
            source_line_number,
            total_issue_count,
            covered_issue_count,
            reviewed_issue_count,
            open_issue_count,
            issue_families_json,
            covered_families_json,
            open_families_json
        FROM ml_issue_partial_coverage_items
        WHERE run_id = ?
          AND coverage_state = 'partial'
        ORDER BY open_issue_count DESC, total_issue_count DESC, relative_path, source_line_number, source_key
        LIMIT 12
        """,
        (run_id,),
    )
    full_samples = fetch_all(
        conn,
        """
        SELECT
            relative_path,
            source_key,
            source_line_number,
            total_issue_count,
            covered_issue_count,
            coverage_sources_json
        FROM ml_issue_partial_coverage_items
        WHERE run_id = ?
          AND coverage_state = 'full'
        ORDER BY total_issue_count DESC, relative_path, source_line_number, source_key
        LIMIT 8
        """,
        (run_id,),
    )
    uncovered_samples = fetch_all(
        conn,
        """
        SELECT
            relative_path,
            source_key,
            source_line_number,
            total_issue_count,
            open_issue_count,
            issue_families_json
        FROM ml_issue_partial_coverage_items
        WHERE run_id = ?
          AND coverage_state = 'none'
        ORDER BY total_issue_count DESC, relative_path, source_line_number, source_key
        LIMIT 12
        """,
        (run_id,),
    )
    family_counts = parse_json_dict(run.get("family_counts_json"))
    top_open_families = sorted(
        family_counts.items(),
        key=lambda item: (-int((item[1] or {}).get("open") or 0), item[0]),
    )[:12]
    top_covered_families = sorted(
        family_counts.items(),
        key=lambda item: (-int((item[1] or {}).get("covered") or 0), item[0]),
    )[:12]
    return {
        "run": run,
        "by_state": by_state,
        "by_review": by_review,
        "bucket_counts": parse_json_dict(run.get("bucket_counts_json")),
        "family_counts": family_counts,
        "agent_counts": parse_json_dict(run.get("agent_counts_json")),
        "source_counts": parse_json_dict(run.get("source_counts_json")),
        "top_open_families": top_open_families,
        "top_covered_families": top_covered_families,
        "partial_samples": partial_samples,
        "full_samples": full_samples,
        "uncovered_samples": uncovered_samples,
    }


def summarize_issue_review_queue(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_review_queue_runs") or not table_exists(conn, "ml_issue_review_queue_items"):
        return None
    run_id = latest_id(conn, "ml_issue_review_queue_runs", "finished_at IS NOT NULL")
    if run_id is None:
        return None
    run = fetch_one(conn, "SELECT * FROM ml_issue_review_queue_runs WHERE id = ?", (run_id,))
    if not run:
        return None
    by_bucket = fetch_all(
        conn,
        """
        SELECT queue_bucket, COUNT(*) AS count, ROUND(AVG(priority_score), 4) AS avg_priority
        FROM ml_issue_review_queue_items
        WHERE run_id = ?
        GROUP BY queue_bucket
        ORDER BY count DESC, queue_bucket
        """,
        (run_id,),
    )
    by_decision = fetch_all(
        conn,
        """
        SELECT suggested_decision, review_status, COUNT(*) AS count
        FROM ml_issue_review_queue_items
        WHERE run_id = ?
        GROUP BY suggested_decision, review_status
        ORDER BY count DESC, suggested_decision
        """,
        (run_id,),
    )
    by_package = fetch_all(
        conn,
        """
        SELECT
            CASE
                WHEN instr(relative_path, '/') > 0 THEN substr(relative_path, 1, instr(relative_path, '/') - 1)
                ELSE relative_path
            END AS package_name,
            COUNT(*) AS count
        FROM ml_issue_review_queue_items
        WHERE run_id = ?
        GROUP BY package_name
        ORDER BY count DESC, package_name
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_bucket": by_bucket,
        "by_decision": by_decision,
        "by_package": by_package,
    }


def summarize_issue_review_decisions(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_review_decision_runs") or not table_exists(conn, "ml_issue_review_decisions"):
        return None
    run_id = latest_id(conn, "ml_issue_review_decision_runs", "finished_at IS NOT NULL")
    if run_id is None:
        return None
    run = fetch_one(conn, "SELECT * FROM ml_issue_review_decision_runs WHERE id = ?", (run_id,))
    if not run:
        return None
    by_evidence = fetch_all(
        conn,
        """
        SELECT evidence_label, normalized_decision, COUNT(*) AS count
        FROM ml_issue_review_decisions
        WHERE run_id = ?
        GROUP BY evidence_label, normalized_decision
        ORDER BY count DESC, evidence_label, normalized_decision
        """,
        (run_id,),
    )
    by_bucket = fetch_all(
        conn,
        """
        SELECT queue_bucket, evidence_label, COUNT(*) AS count
        FROM ml_issue_review_decisions
        WHERE run_id = ?
        GROUP BY queue_bucket, evidence_label
        ORDER BY count DESC, queue_bucket
        """,
        (run_id,),
    )
    queue_progress = None
    if run.get("queue_run_id"):
        queue_progress = fetch_one(
            conn,
            """
            SELECT id, selected_count, open_count, reviewed_count
            FROM ml_issue_review_queue_runs
            WHERE id = ?
            """,
            (run["queue_run_id"],),
        )
    return {
        "run": run,
        "by_evidence": by_evidence,
        "by_bucket": by_bucket,
        "queue_progress": queue_progress,
    }


def summarize_issue_dynamic_literal_repair(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_dynamic_literal_repair_runs") or not table_exists(
        conn,
        "ml_issue_dynamic_literal_repair_items",
    ):
        return None
    run_id = latest_id(conn, "ml_issue_dynamic_literal_repair_runs", "finished_at IS NOT NULL")
    if run_id is None:
        return None
    run = fetch_one(conn, "SELECT * FROM ml_issue_dynamic_literal_repair_runs WHERE id = ?", (run_id,))
    if not run:
        return None
    by_status = fetch_all(
        conn,
        """
        SELECT repair_status, token_status, COUNT(*) AS count
        FROM ml_issue_dynamic_literal_repair_items
        WHERE run_id = ?
        GROUP BY repair_status, token_status
        ORDER BY count DESC, repair_status, token_status
        """,
        (run_id,),
    )
    by_bucket = fetch_all(
        conn,
        """
        SELECT queue_bucket, repair_status, COUNT(*) AS count
        FROM ml_issue_dynamic_literal_repair_items
        WHERE run_id = ?
        GROUP BY queue_bucket, repair_status
        ORDER BY count DESC, queue_bucket, repair_status
        """,
        (run_id,),
    )
    context_rows = fetch_all(
        conn,
        """
        SELECT segment_id, relative_path, source_key, repair_status, token_status, reasons_json
        FROM ml_issue_dynamic_literal_repair_items
        WHERE run_id = ?
          AND repair_status <> 'ready_shadow'
        ORDER BY repair_status, relative_path, source_key
        LIMIT 12
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_status": by_status,
        "by_bucket": by_bucket,
        "context_rows": context_rows,
    }


def summarize_issue_select_cstring_auxiliary_rewrite_checkpoint(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_select_cstring_auxiliary_rewrite_checkpoint_runs") or not table_exists(
        conn,
        "ml_issue_select_cstring_auxiliary_rewrite_checkpoint_items",
    ):
        return None
    run_id = latest_id(
        conn,
        "ml_issue_select_cstring_auxiliary_rewrite_checkpoint_runs",
        "finished_at IS NOT NULL",
    )
    if run_id is None:
        return None
    run = fetch_one(
        conn,
        "SELECT * FROM ml_issue_select_cstring_auxiliary_rewrite_checkpoint_runs WHERE id = ?",
        (run_id,),
    )
    if not run:
        return None
    by_subpolicy = fetch_all(
        conn,
        """
        SELECT
            subpolicy_name,
            checkpoint_allowed,
            token_status,
            block_reason,
            COUNT(*) AS count
        FROM ml_issue_select_cstring_auxiliary_rewrite_checkpoint_items
        WHERE run_id = ?
        GROUP BY subpolicy_name, checkpoint_allowed, token_status, block_reason
        ORDER BY count DESC, subpolicy_name
        """,
        (run_id,),
    )
    blocked_rows = fetch_all(
        conn,
        """
        SELECT
            segment_id,
            relative_path,
            source_key,
            subpolicy_name,
            token_status,
            block_reason,
            current_text,
            corrected_text
        FROM ml_issue_select_cstring_auxiliary_rewrite_checkpoint_items
        WHERE run_id = ?
          AND checkpoint_allowed = 0
        ORDER BY segment_id
        LIMIT 12
        """,
        (run_id,),
    )
    ready_samples = fetch_all(
        conn,
        """
        SELECT
            segment_id,
            relative_path,
            source_key,
            subpolicy_name,
            token_status
        FROM ml_issue_select_cstring_auxiliary_rewrite_checkpoint_items
        WHERE run_id = ?
          AND checkpoint_allowed = 1
        ORDER BY segment_id
        LIMIT 12
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_subpolicy": by_subpolicy,
        "blocked_rows": blocked_rows,
        "ready_samples": ready_samples,
        "allowed_rate": percent(run["allowed_count"], run["candidate_count"]),
    }


def summarize_issue_select_cstring_antpath_relation_rewrite_checkpoint(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_select_cstring_antpath_relation_rewrite_checkpoint_runs") or not table_exists(
        conn,
        "ml_issue_select_cstring_antpath_relation_rewrite_checkpoint_items",
    ):
        return None
    run_id = latest_id(
        conn,
        "ml_issue_select_cstring_antpath_relation_rewrite_checkpoint_runs",
        "finished_at IS NOT NULL",
    )
    if run_id is None:
        return None
    run = fetch_one(
        conn,
        "SELECT * FROM ml_issue_select_cstring_antpath_relation_rewrite_checkpoint_runs WHERE id = ?",
        (run_id,),
    )
    if not run:
        return None
    by_subpolicy = fetch_all(
        conn,
        """
        SELECT
            subpolicy_name,
            checkpoint_allowed,
            token_status,
            block_reason,
            COUNT(*) AS count
        FROM ml_issue_select_cstring_antpath_relation_rewrite_checkpoint_items
        WHERE run_id = ?
        GROUP BY subpolicy_name, checkpoint_allowed, token_status, block_reason
        ORDER BY count DESC, subpolicy_name
        """,
        (run_id,),
    )
    blocked_rows = fetch_all(
        conn,
        """
        SELECT
            segment_id,
            relative_path,
            source_key,
            subpolicy_name,
            token_status,
            block_reason,
            current_text,
            corrected_text
        FROM ml_issue_select_cstring_antpath_relation_rewrite_checkpoint_items
        WHERE run_id = ?
          AND checkpoint_allowed = 0
        ORDER BY segment_id
        LIMIT 12
        """,
        (run_id,),
    )
    ready_samples = fetch_all(
        conn,
        """
        SELECT
            segment_id,
            relative_path,
            source_key,
            subpolicy_name,
            token_status
        FROM ml_issue_select_cstring_antpath_relation_rewrite_checkpoint_items
        WHERE run_id = ?
          AND checkpoint_allowed = 1
        ORDER BY segment_id
        LIMIT 12
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_subpolicy": by_subpolicy,
        "blocked_rows": blocked_rows,
        "ready_samples": ready_samples,
        "allowed_rate": percent(run["allowed_count"], run["candidate_count"]),
    }


def summarize_issue_select_cstring_same_token_lifecycle(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_select_cstring_same_token_lifecycle_runs") or not table_exists(
        conn,
        "ml_issue_select_cstring_same_token_lifecycle_items",
    ):
        return None
    run = fetch_one(
        conn,
        """
        SELECT *
        FROM ml_issue_select_cstring_same_token_lifecycle_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not run:
        return None
    run_id = int(run["id"])
    by_source = fetch_all(
        conn,
        """
        SELECT source_family, policy_allowed, COALESCE(block_reason, '') AS block_reason, COUNT(*) AS count
        FROM ml_issue_select_cstring_same_token_lifecycle_items
        WHERE run_id = ?
        GROUP BY source_family, policy_allowed, COALESCE(block_reason, '')
        ORDER BY policy_allowed DESC, count DESC, source_family
        """,
        (run_id,),
    )
    by_subpolicy = fetch_all(
        conn,
        """
        SELECT subpolicy_name, policy_allowed, COALESCE(block_reason, '') AS block_reason, COUNT(*) AS count
        FROM ml_issue_select_cstring_same_token_lifecycle_items
        WHERE run_id = ?
        GROUP BY subpolicy_name, policy_allowed, COALESCE(block_reason, '')
        ORDER BY policy_allowed DESC, count DESC, subpolicy_name
        """,
        (run_id,),
    )
    by_block = fetch_all(
        conn,
        """
        SELECT COALESCE(block_reason, '') AS block_reason, COUNT(*) AS count
        FROM ml_issue_select_cstring_same_token_lifecycle_items
        WHERE run_id = ?
          AND policy_allowed = 0
        GROUP BY COALESCE(block_reason, '')
        ORDER BY count DESC, block_reason
        """,
        (run_id,),
    )
    samples = fetch_all(
        conn,
        """
        SELECT
            source_family,
            segment_id,
            relative_path,
            source_key,
            subpolicy_name,
            token_status,
            policy_allowed,
            block_reason
        FROM ml_issue_select_cstring_same_token_lifecycle_items
        WHERE run_id = ?
        ORDER BY policy_allowed DESC, source_family, segment_id
        LIMIT 24
        """,
        (run_id,),
    )
    blocked_samples = fetch_all(
        conn,
        """
        SELECT
            source_family,
            segment_id,
            relative_path,
            source_key,
            subpolicy_name,
            token_status,
            block_reason,
            validation_issues_json
        FROM ml_issue_select_cstring_same_token_lifecycle_items
        WHERE run_id = ?
          AND policy_allowed = 0
        ORDER BY source_family, segment_id
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_source": by_source,
        "by_subpolicy": by_subpolicy,
        "by_block": by_block,
        "samples": samples,
        "blocked_samples": blocked_samples,
        "release_rate": percent(run["released_count"], run["candidate_count"]),
        "source_counts": parse_json_dict(run.get("source_counts_json")),
        "subpolicy_counts": parse_json_dict(run.get("subpolicy_counts_json")),
        "block_counts": parse_json_dict(run.get("block_counts_json")),
    }


def summarize_issue_select_cstring_residual_literal_cleanup_checkpoint(conn) -> dict[str, Any] | None:
    if not table_exists(
        conn,
        "ml_issue_select_cstring_residual_literal_cleanup_checkpoint_runs",
    ) or not table_exists(
        conn,
        "ml_issue_select_cstring_residual_literal_cleanup_checkpoint_items",
    ):
        return None
    run_id = latest_id(
        conn,
        "ml_issue_select_cstring_residual_literal_cleanup_checkpoint_runs",
        "finished_at IS NOT NULL",
    )
    if run_id is None:
        return None
    run = fetch_one(
        conn,
        "SELECT * FROM ml_issue_select_cstring_residual_literal_cleanup_checkpoint_runs WHERE id = ?",
        (run_id,),
    )
    if not run:
        return None
    by_subpolicy = fetch_all(
        conn,
        """
        SELECT
            subpolicy_name,
            checkpoint_allowed,
            COALESCE(block_reason, '') AS block_reason,
            token_status,
            COUNT(*) AS count
        FROM ml_issue_select_cstring_residual_literal_cleanup_checkpoint_items
        WHERE run_id = ?
        GROUP BY subpolicy_name, checkpoint_allowed, COALESCE(block_reason, ''), token_status
        ORDER BY checkpoint_allowed DESC, count DESC, subpolicy_name
        """,
        (run_id,),
    )
    by_token_status = fetch_all(
        conn,
        """
        SELECT token_status, checkpoint_allowed, COUNT(*) AS count
        FROM ml_issue_select_cstring_residual_literal_cleanup_checkpoint_items
        WHERE run_id = ?
        GROUP BY token_status, checkpoint_allowed
        ORDER BY checkpoint_allowed DESC, count DESC, token_status
        """,
        (run_id,),
    )
    blocked_rows = fetch_all(
        conn,
        """
        SELECT
            segment_id,
            relative_path,
            source_key,
            subpolicy_name,
            token_status,
            block_reason,
            validation_issues_json
        FROM ml_issue_select_cstring_residual_literal_cleanup_checkpoint_items
        WHERE run_id = ?
          AND checkpoint_allowed = 0
        ORDER BY segment_id
        LIMIT 20
        """,
        (run_id,),
    )
    ready_samples = fetch_all(
        conn,
        """
        SELECT
            segment_id,
            relative_path,
            source_key,
            subpolicy_name,
            token_status
        FROM ml_issue_select_cstring_residual_literal_cleanup_checkpoint_items
        WHERE run_id = ?
          AND checkpoint_allowed = 1
        ORDER BY segment_id
        LIMIT 12
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_subpolicy": by_subpolicy,
        "by_token_status": by_token_status,
        "blocked_rows": blocked_rows,
        "ready_samples": ready_samples,
        "allowed_rate": percent(run["allowed_count"], run["candidate_count"]),
    }


def summarize_issue_select_cstring_same_token_composition_overlay(conn) -> dict[str, Any] | None:
    if not table_exists(
        conn,
        "ml_issue_select_cstring_same_token_composition_overlay_runs",
    ) or not table_exists(
        conn,
        "ml_issue_select_cstring_same_token_composition_overlay_items",
    ):
        return None
    run_id = latest_id(
        conn,
        "ml_issue_select_cstring_same_token_composition_overlay_runs",
        "finished_at IS NOT NULL",
    )
    if run_id is None:
        return None
    run = fetch_one(
        conn,
        "SELECT * FROM ml_issue_select_cstring_same_token_composition_overlay_runs WHERE id = ?",
        (run_id,),
    )
    if not run:
        return None
    by_source = fetch_all(
        conn,
        """
        SELECT
            composition_source,
            composition_allowed,
            COALESCE(block_reason, '') AS block_reason,
            COUNT(*) AS count
        FROM ml_issue_select_cstring_same_token_composition_overlay_items
        WHERE run_id = ?
        GROUP BY composition_source, composition_allowed, COALESCE(block_reason, '')
        ORDER BY composition_allowed DESC, count DESC, composition_source
        """,
        (run_id,),
    )
    by_block = fetch_all(
        conn,
        """
        SELECT COALESCE(block_reason, '') AS block_reason, token_status, COUNT(*) AS count
        FROM ml_issue_select_cstring_same_token_composition_overlay_items
        WHERE run_id = ?
          AND composition_allowed = 0
        GROUP BY COALESCE(block_reason, ''), token_status
        ORDER BY count DESC, block_reason
        """,
        (run_id,),
    )
    recovered_samples = fetch_all(
        conn,
        """
        SELECT segment_id, relative_path, source_key, source_family, subpolicy_name, token_status
        FROM ml_issue_select_cstring_same_token_composition_overlay_items
        WHERE run_id = ?
          AND composition_source = 'residual_literal_cleanup'
        ORDER BY segment_id
        LIMIT 12
        """,
        (run_id,),
    )
    blocked_rows = fetch_all(
        conn,
        """
        SELECT segment_id, relative_path, source_key, source_family, subpolicy_name, token_status, block_reason
        FROM ml_issue_select_cstring_same_token_composition_overlay_items
        WHERE run_id = ?
          AND composition_allowed = 0
        ORDER BY segment_id
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_source": by_source,
        "by_block": by_block,
        "recovered_samples": recovered_samples,
        "blocked_rows": blocked_rows,
        "baseline_rate": percent(run["baseline_released_count"], run["candidate_count"]),
        "composed_rate": percent(run["composed_released_count"], run["candidate_count"]),
        "lift_rate": percent(run["cleanup_lift_count"], run["candidate_count"]),
        "source_counts": parse_json_dict(run.get("source_counts_json")),
        "block_counts": parse_json_dict(run.get("block_counts_json")),
    }


def summarize_issue_select_cstring_dynamic_literal_payload_checkpoint(conn) -> dict[str, Any] | None:
    if not table_exists(
        conn,
        "ml_issue_select_cstring_dynamic_literal_payload_checkpoint_runs",
    ) or not table_exists(
        conn,
        "ml_issue_select_cstring_dynamic_literal_payload_checkpoint_items",
    ):
        return None
    run_id = latest_id(
        conn,
        "ml_issue_select_cstring_dynamic_literal_payload_checkpoint_runs",
        "finished_at IS NOT NULL",
    )
    if run_id is None:
        return None
    run = fetch_one(
        conn,
        "SELECT * FROM ml_issue_select_cstring_dynamic_literal_payload_checkpoint_runs WHERE id = ?",
        (run_id,),
    )
    if not run:
        return None
    by_subpolicy = fetch_all(
        conn,
        """
        SELECT
            subpolicy_name,
            checkpoint_allowed,
            COALESCE(block_reason, '') AS block_reason,
            token_status,
            COUNT(*) AS count
        FROM ml_issue_select_cstring_dynamic_literal_payload_checkpoint_items
        WHERE run_id = ?
        GROUP BY subpolicy_name, checkpoint_allowed, COALESCE(block_reason, ''), token_status
        ORDER BY checkpoint_allowed DESC, count DESC, subpolicy_name
        """,
        (run_id,),
    )
    ready_samples = fetch_all(
        conn,
        """
        SELECT segment_id, relative_path, source_key, subpolicy_name, token_status
        FROM ml_issue_select_cstring_dynamic_literal_payload_checkpoint_items
        WHERE run_id = ?
          AND checkpoint_allowed = 1
        ORDER BY segment_id
        LIMIT 12
        """,
        (run_id,),
    )
    blocked_rows = fetch_all(
        conn,
        """
        SELECT
            segment_id,
            relative_path,
            source_key,
            subpolicy_name,
            token_status,
            block_reason,
            validation_issues_json
        FROM ml_issue_select_cstring_dynamic_literal_payload_checkpoint_items
        WHERE run_id = ?
          AND checkpoint_allowed = 0
        ORDER BY segment_id
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_subpolicy": by_subpolicy,
        "ready_samples": ready_samples,
        "blocked_rows": blocked_rows,
        "allowed_rate": percent(run["allowed_count"], run["candidate_count"]),
        "block_counts": parse_json_dict(run.get("block_counts_json")),
    }


def summarize_issue_select_cstring_multistage_composition_overlay(conn) -> dict[str, Any] | None:
    if not table_exists(
        conn,
        "ml_issue_select_cstring_multistage_composition_overlay_runs",
    ) or not table_exists(
        conn,
        "ml_issue_select_cstring_multistage_composition_overlay_items",
    ):
        return None
    run_id = latest_id(
        conn,
        "ml_issue_select_cstring_multistage_composition_overlay_runs",
        "finished_at IS NOT NULL",
    )
    if run_id is None:
        return None
    run = fetch_one(
        conn,
        "SELECT * FROM ml_issue_select_cstring_multistage_composition_overlay_runs WHERE id = ?",
        (run_id,),
    )
    if not run:
        return None
    by_source = fetch_all(
        conn,
        """
        SELECT
            composition_source,
            composition_allowed,
            COALESCE(block_reason, '') AS block_reason,
            COUNT(*) AS count
        FROM ml_issue_select_cstring_multistage_composition_overlay_items
        WHERE run_id = ?
        GROUP BY composition_source, composition_allowed, COALESCE(block_reason, '')
        ORDER BY composition_allowed DESC, count DESC, composition_source
        """,
        (run_id,),
    )
    blocked_rows = fetch_all(
        conn,
        """
        SELECT segment_id, relative_path, source_key, source_family, subpolicy_name, token_status, block_reason
        FROM ml_issue_select_cstring_multistage_composition_overlay_items
        WHERE run_id = ?
          AND composition_allowed = 0
        ORDER BY segment_id
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_source": by_source,
        "blocked_rows": blocked_rows,
        "baseline_rate": percent(run["baseline_released_count"], run["candidate_count"]),
        "cleanup_lift_rate": percent(run["cleanup_lift_count"], run["candidate_count"]),
        "dynamic_lift_rate": percent(run["dynamic_payload_lift_count"], run["candidate_count"]),
        "composed_rate": percent(run["composed_released_count"], run["candidate_count"]),
        "source_counts": parse_json_dict(run.get("source_counts_json")),
        "block_counts": parse_json_dict(run.get("block_counts_json")),
    }


def summarize_issue_select_cstring_local_player_pronoun_checkpoint(conn) -> dict[str, Any] | None:
    if not table_exists(
        conn,
        "ml_issue_select_cstring_local_player_pronoun_checkpoint_runs",
    ) or not table_exists(
        conn,
        "ml_issue_select_cstring_local_player_pronoun_checkpoint_items",
    ):
        return None
    run_id = latest_id(
        conn,
        "ml_issue_select_cstring_local_player_pronoun_checkpoint_runs",
        "finished_at IS NOT NULL",
    )
    if run_id is None:
        return None
    run = fetch_one(
        conn,
        "SELECT * FROM ml_issue_select_cstring_local_player_pronoun_checkpoint_runs WHERE id = ?",
        (run_id,),
    )
    if not run:
        return None
    by_subpolicy = fetch_all(
        conn,
        """
        SELECT
            subpolicy_name,
            checkpoint_allowed,
            COALESCE(block_reason, '') AS block_reason,
            token_status,
            COUNT(*) AS count
        FROM ml_issue_select_cstring_local_player_pronoun_checkpoint_items
        WHERE run_id = ?
        GROUP BY subpolicy_name, checkpoint_allowed, COALESCE(block_reason, ''), token_status
        ORDER BY checkpoint_allowed DESC, count DESC, subpolicy_name
        """,
        (run_id,),
    )
    ready_samples = fetch_all(
        conn,
        """
        SELECT segment_id, relative_path, source_key, subpolicy_name, token_status
        FROM ml_issue_select_cstring_local_player_pronoun_checkpoint_items
        WHERE run_id = ?
          AND checkpoint_allowed = 1
        ORDER BY segment_id
        LIMIT 12
        """,
        (run_id,),
    )
    blocked_rows = fetch_all(
        conn,
        """
        SELECT segment_id, relative_path, source_key, subpolicy_name, token_status, block_reason
        FROM ml_issue_select_cstring_local_player_pronoun_checkpoint_items
        WHERE run_id = ?
          AND checkpoint_allowed = 0
        ORDER BY segment_id
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_subpolicy": by_subpolicy,
        "ready_samples": ready_samples,
        "blocked_rows": blocked_rows,
        "allowed_rate": percent(run["allowed_count"], run["candidate_count"]),
        "block_counts": parse_json_dict(run.get("block_counts_json")),
    }


def summarize_issue_select_cstring_final_composition_overlay(conn) -> dict[str, Any] | None:
    if not table_exists(
        conn,
        "ml_issue_select_cstring_final_composition_overlay_runs",
    ) or not table_exists(
        conn,
        "ml_issue_select_cstring_final_composition_overlay_items",
    ):
        return None
    run_id = latest_id(
        conn,
        "ml_issue_select_cstring_final_composition_overlay_runs",
        "finished_at IS NOT NULL",
    )
    if run_id is None:
        return None
    run = fetch_one(
        conn,
        "SELECT * FROM ml_issue_select_cstring_final_composition_overlay_runs WHERE id = ?",
        (run_id,),
    )
    if not run:
        return None
    by_source = fetch_all(
        conn,
        """
        SELECT
            composition_source,
            composition_allowed,
            COALESCE(block_reason, '') AS block_reason,
            COUNT(*) AS count
        FROM ml_issue_select_cstring_final_composition_overlay_items
        WHERE run_id = ?
        GROUP BY composition_source, composition_allowed, COALESCE(block_reason, '')
        ORDER BY composition_allowed DESC, count DESC, composition_source
        """,
        (run_id,),
    )
    blocked_rows = fetch_all(
        conn,
        """
        SELECT segment_id, relative_path, source_key, source_family, subpolicy_name, token_status, block_reason
        FROM ml_issue_select_cstring_final_composition_overlay_items
        WHERE run_id = ?
          AND composition_allowed = 0
        ORDER BY segment_id
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_source": by_source,
        "blocked_rows": blocked_rows,
        "baseline_rate": percent(run["baseline_released_count"], run["candidate_count"]),
        "cleanup_lift_rate": percent(run["cleanup_lift_count"], run["candidate_count"]),
        "dynamic_lift_rate": percent(run["dynamic_payload_lift_count"], run["candidate_count"]),
        "local_pronoun_lift_rate": percent(run["local_pronoun_lift_count"], run["candidate_count"]),
        "composed_rate": percent(run["composed_released_count"], run["candidate_count"]),
        "source_counts": parse_json_dict(run.get("source_counts_json")),
        "block_counts": parse_json_dict(run.get("block_counts_json")),
    }


def summarize_issue_select_cstring_final_composition_maturity_audit(conn) -> dict[str, Any] | None:
    if not table_exists(
        conn,
        "ml_issue_select_cstring_final_composition_maturity_audit_runs",
    ) or not table_exists(
        conn,
        "ml_issue_select_cstring_final_composition_maturity_audit_items",
    ):
        return None
    run_id = latest_id(
        conn,
        "ml_issue_select_cstring_final_composition_maturity_audit_runs",
        "finished_at IS NOT NULL",
    )
    if run_id is None:
        return None
    run = fetch_one(
        conn,
        "SELECT * FROM ml_issue_select_cstring_final_composition_maturity_audit_runs WHERE id = ?",
        (run_id,),
    )
    if not run:
        return None
    issues = fetch_all(
        conn,
        """
        SELECT
            issue_code,
            issue_detail,
            segment_id,
            relative_path,
            source_key,
            composition_source
        FROM ml_issue_select_cstring_final_composition_maturity_audit_items
        WHERE run_id = ?
        ORDER BY issue_code, segment_id
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "issues": issues,
        "source_counts": parse_json_dict(run.get("source_counts_json")),
        "issue_counts": parse_json_dict(run.get("issue_counts_json")),
        "composed_rate": percent(run["composed_released_count"], run["candidate_count"]),
    }


def summarize_issue_select_cstring_governed_bridge_proposal(conn) -> dict[str, Any] | None:
    if not table_exists(
        conn,
        "ml_issue_select_cstring_governed_bridge_proposal_runs",
    ) or not table_exists(
        conn,
        "ml_issue_select_cstring_governed_bridge_proposal_items",
    ):
        return None
    run_id = latest_id(
        conn,
        "ml_issue_select_cstring_governed_bridge_proposal_runs",
        "finished_at IS NOT NULL",
    )
    if run_id is None:
        return None
    run = fetch_one(
        conn,
        "SELECT * FROM ml_issue_select_cstring_governed_bridge_proposal_runs WHERE id = ?",
        (run_id,),
    )
    if not run:
        return None
    by_status = fetch_all(
        conn,
        """
        SELECT bridge_status, risk_level, token_status, COUNT(*) AS count
        FROM ml_issue_select_cstring_governed_bridge_proposal_items
        WHERE run_id = ?
        GROUP BY bridge_status, risk_level, token_status
        ORDER BY count DESC, bridge_status
        """,
        (run_id,),
    )
    by_source = fetch_all(
        conn,
        """
        SELECT composition_source, bridge_status, COUNT(*) AS count
        FROM ml_issue_select_cstring_governed_bridge_proposal_items
        WHERE run_id = ?
        GROUP BY composition_source, bridge_status
        ORDER BY count DESC, composition_source
        """,
        (run_id,),
    )
    blocked_rows = fetch_all(
        conn,
        """
        SELECT segment_id, relative_path, source_key, composition_source, bridge_status, blocking_reason
        FROM ml_issue_select_cstring_governed_bridge_proposal_items
        WHERE run_id = ?
          AND bridge_status LIKE 'blocked_%'
        ORDER BY segment_id
        LIMIT 20
        """,
        (run_id,),
    )
    ready_samples = fetch_all(
        conn,
        """
        SELECT segment_id, relative_path, source_key, composition_source, token_status, risk_level
        FROM ml_issue_select_cstring_governed_bridge_proposal_items
        WHERE run_id = ?
          AND bridge_status = 'ready_for_governed_bridge_review'
        ORDER BY segment_id
        LIMIT 12
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_status": by_status,
        "by_source": by_source,
        "blocked_rows": blocked_rows,
        "ready_samples": ready_samples,
        "ready_rate": percent(run["ready_count"], run["total_candidates"]),
        "status_counts": parse_json_dict(run.get("status_counts_json")),
        "source_counts": parse_json_dict(run.get("source_counts_json")),
        "token_status_counts": parse_json_dict(run.get("token_status_counts_json")),
    }


def summarize_issue_select_cstring_label_rewrite_checkpoint(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_select_cstring_label_rewrite_checkpoint_runs") or not table_exists(
        conn,
        "ml_issue_select_cstring_label_rewrite_checkpoint_items",
    ):
        return None
    run_id = latest_id(
        conn,
        "ml_issue_select_cstring_label_rewrite_checkpoint_runs",
        "finished_at IS NOT NULL",
    )
    if run_id is None:
        return None
    run = fetch_one(
        conn,
        "SELECT * FROM ml_issue_select_cstring_label_rewrite_checkpoint_runs WHERE id = ?",
        (run_id,),
    )
    if not run:
        return None
    by_subpolicy = fetch_all(
        conn,
        """
        SELECT
            subpolicy_name,
            checkpoint_allowed,
            token_status,
            block_reason,
            COUNT(*) AS count
        FROM ml_issue_select_cstring_label_rewrite_checkpoint_items
        WHERE run_id = ?
        GROUP BY subpolicy_name, checkpoint_allowed, token_status, block_reason
        ORDER BY count DESC, subpolicy_name
        """,
        (run_id,),
    )
    blocked_rows = fetch_all(
        conn,
        """
        SELECT
            segment_id,
            relative_path,
            source_key,
            subpolicy_name,
            token_status,
            block_reason,
            current_text,
            corrected_text
        FROM ml_issue_select_cstring_label_rewrite_checkpoint_items
        WHERE run_id = ?
          AND checkpoint_allowed = 0
        ORDER BY segment_id
        LIMIT 12
        """,
        (run_id,),
    )
    ready_samples = fetch_all(
        conn,
        """
        SELECT
            segment_id,
            relative_path,
            source_key,
            subpolicy_name,
            token_status
        FROM ml_issue_select_cstring_label_rewrite_checkpoint_items
        WHERE run_id = ?
          AND checkpoint_allowed = 1
        ORDER BY segment_id
        LIMIT 12
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_subpolicy": by_subpolicy,
        "blocked_rows": blocked_rows,
        "ready_samples": ready_samples,
        "allowed_rate": percent(run["allowed_count"], run["candidate_count"]),
    }


def summarize_issue_select_cstring_object_sentence_rewrite_checkpoint(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_select_cstring_object_sentence_rewrite_checkpoint_runs") or not table_exists(
        conn,
        "ml_issue_select_cstring_object_sentence_rewrite_checkpoint_items",
    ):
        return None
    run_id = latest_id(
        conn,
        "ml_issue_select_cstring_object_sentence_rewrite_checkpoint_runs",
        "finished_at IS NOT NULL",
    )
    if run_id is None:
        return None
    run = fetch_one(
        conn,
        "SELECT * FROM ml_issue_select_cstring_object_sentence_rewrite_checkpoint_runs WHERE id = ?",
        (run_id,),
    )
    if not run:
        return None
    by_subpolicy = fetch_all(
        conn,
        """
        SELECT
            subpolicy_name,
            checkpoint_allowed,
            token_status,
            block_reason,
            COUNT(*) AS count
        FROM ml_issue_select_cstring_object_sentence_rewrite_checkpoint_items
        WHERE run_id = ?
        GROUP BY subpolicy_name, checkpoint_allowed, token_status, block_reason
        ORDER BY count DESC, subpolicy_name
        """,
        (run_id,),
    )
    blocked_rows = fetch_all(
        conn,
        """
        SELECT
            segment_id,
            relative_path,
            source_key,
            subpolicy_name,
            token_status,
            block_reason,
            current_text,
            corrected_text
        FROM ml_issue_select_cstring_object_sentence_rewrite_checkpoint_items
        WHERE run_id = ?
          AND checkpoint_allowed = 0
        ORDER BY segment_id
        LIMIT 12
        """,
        (run_id,),
    )
    ready_samples = fetch_all(
        conn,
        """
        SELECT
            segment_id,
            relative_path,
            source_key,
            subpolicy_name,
            token_status
        FROM ml_issue_select_cstring_object_sentence_rewrite_checkpoint_items
        WHERE run_id = ?
          AND checkpoint_allowed = 1
        ORDER BY segment_id
        LIMIT 12
        """,
        (run_id,),
    )
    family_count = fetch_one(
        conn,
        """
        SELECT COUNT(*) AS count
        FROM source_segments
        WHERE old_text LIKE '%mi persona%'
        """,
    )
    return {
        "run": run,
        "by_subpolicy": by_subpolicy,
        "blocked_rows": blocked_rows,
        "ready_samples": ready_samples,
        "allowed_rate": percent(run["allowed_count"], run["candidate_count"]),
        "observed_mi_persona_family_count": int((family_count or {}).get("count") or 0),
    }


def summarize_issue_nickname_pauper_king_title_semantic_checkpoint(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_nickname_pauper_king_title_semantic_checkpoint_runs") or not table_exists(
        conn,
        "ml_issue_nickname_pauper_king_title_semantic_checkpoint_items",
    ):
        return None
    run_id = latest_id(
        conn,
        "ml_issue_nickname_pauper_king_title_semantic_checkpoint_runs",
        "finished_at IS NOT NULL",
    )
    if run_id is None:
        return None
    run = fetch_one(
        conn,
        "SELECT * FROM ml_issue_nickname_pauper_king_title_semantic_checkpoint_runs WHERE id = ?",
        (run_id,),
    )
    if not run:
        return None
    by_subpolicy = fetch_all(
        conn,
        """
        SELECT
            subpolicy_name,
            checkpoint_allowed,
            token_status,
            block_reason,
            COUNT(*) AS count
        FROM ml_issue_nickname_pauper_king_title_semantic_checkpoint_items
        WHERE run_id = ?
        GROUP BY subpolicy_name, checkpoint_allowed, token_status, block_reason
        ORDER BY count DESC, subpolicy_name
        """,
        (run_id,),
    )
    blocked_rows = fetch_all(
        conn,
        """
        SELECT
            segment_id,
            relative_path,
            source_key,
            subpolicy_name,
            token_status,
            block_reason,
            current_text,
            corrected_text,
            reference_key,
            reference_translation
        FROM ml_issue_nickname_pauper_king_title_semantic_checkpoint_items
        WHERE run_id = ?
          AND checkpoint_allowed = 0
        ORDER BY segment_id
        LIMIT 12
        """,
        (run_id,),
    )
    ready_samples = fetch_all(
        conn,
        """
        SELECT
            segment_id,
            relative_path,
            source_key,
            subpolicy_name,
            token_status,
            reference_key,
            reference_translation
        FROM ml_issue_nickname_pauper_king_title_semantic_checkpoint_items
        WHERE run_id = ?
          AND checkpoint_allowed = 1
        ORDER BY segment_id
        LIMIT 12
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_subpolicy": by_subpolicy,
        "blocked_rows": blocked_rows,
        "ready_samples": ready_samples,
        "allowed_rate": percent(run["allowed_count"], run["candidate_count"]),
    }


def summarize_issue_review_dataset_bridge(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_dataset_runs") or not table_exists(conn, "ml_training_examples"):
        return None
    run = fetch_one(
        conn,
        """
        SELECT r.*
        FROM ml_dataset_runs r
        WHERE EXISTS (
            SELECT 1
            FROM ml_training_examples e
            WHERE e.run_id = r.id
              AND e.evidence_source = 'issue_review_decision'
        )
        ORDER BY r.id DESC
        LIMIT 1
        """,
    )
    if not run:
        return None
    by_label = fetch_all(
        conn,
        """
        SELECT label, action_label, issue_label, COUNT(*) AS count
        FROM ml_training_examples
        WHERE run_id = ?
          AND evidence_source = 'issue_review_decision'
        GROUP BY label, action_label, issue_label
        ORDER BY count DESC, label, action_label
        """,
        (run["id"],),
    )
    trainable = fetch_one(
        conn,
        """
        SELECT COUNT(*) AS count
        FROM ml_training_examples
        WHERE run_id = ?
          AND evidence_source = 'issue_review_decision'
          AND candidate_text IS NOT NULL
          AND candidate_text <> ''
        """,
        (run["id"],),
    )
    return {
        "run": run,
        "by_label": by_label,
        "trainable_count": int((trainable or {}).get("count") or 0),
    }


def summarize_short_label_positive_release(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_short_label_release_runs") or not table_exists(
        conn,
        "ml_issue_short_label_release_items",
    ):
        return None
    run_id = latest_id(conn, "ml_issue_short_label_release_runs", "finished_at IS NOT NULL")
    if run_id is None:
        return None
    run = fetch_one(conn, "SELECT * FROM ml_issue_short_label_release_runs WHERE id = ?", (run_id,))
    if not run:
        return None
    by_blocker = fetch_all(
        conn,
        """
        SELECT COALESCE(block_reason, 'released_shadow') AS block_reason, COUNT(*) AS count
        FROM ml_issue_short_label_release_items
        WHERE run_id = ?
        GROUP BY COALESCE(block_reason, 'released_shadow')
        ORDER BY count DESC, block_reason
        """,
        (run_id,),
    )
    by_bucket = fetch_all(
        conn,
        """
        SELECT queue_bucket, policy_allowed, COUNT(*) AS count
        FROM ml_issue_short_label_release_items
        WHERE run_id = ?
        GROUP BY queue_bucket, policy_allowed
        ORDER BY count DESC, queue_bucket
        """,
        (run_id,),
    )
    by_decision = fetch_all(
        conn,
        """
        SELECT normalized_decision, policy_allowed, COUNT(*) AS count
        FROM ml_issue_short_label_release_items
        WHERE run_id = ?
        GROUP BY normalized_decision, policy_allowed
        ORDER BY count DESC, normalized_decision
        """,
        (run_id,),
    )
    samples = fetch_all(
        conn,
        """
        SELECT relative_path, source_key, source_line_number, queue_bucket, normalized_decision, policy_allowed, block_reason
        FROM ml_issue_short_label_release_items
        WHERE run_id = ?
        ORDER BY policy_allowed DESC, queue_bucket, relative_path, source_line_number, source_key
        LIMIT 12
        """,
        (run_id,),
    )
    released = int(run["released_shadow_count"] or 0)
    candidates = int(run["candidate_count"] or 0)
    return {
        "run": run,
        "by_blocker": by_blocker,
        "by_bucket": by_bucket,
        "by_decision": by_decision,
        "samples": samples,
        "release_rate": percent(released, candidates),
    }


def summarize_short_label_positive_checkpoint(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_short_label_release_checkpoint_runs") or not table_exists(
        conn,
        "ml_issue_short_label_release_checkpoint_items",
    ):
        return None
    run_id = latest_id(conn, "ml_issue_short_label_release_checkpoint_runs", "finished_at IS NOT NULL")
    if run_id is None:
        return None
    run = fetch_one(conn, "SELECT * FROM ml_issue_short_label_release_checkpoint_runs WHERE id = ?", (run_id,))
    if not run:
        return None
    by_blocker = fetch_all(
        conn,
        """
        SELECT COALESCE(block_reason, 'checkpoint_allowed') AS block_reason, COUNT(*) AS count
        FROM ml_issue_short_label_release_checkpoint_items
        WHERE checkpoint_run_id = ?
        GROUP BY COALESCE(block_reason, 'checkpoint_allowed')
        ORDER BY count DESC, block_reason
        """,
        (run_id,),
    )
    by_bucket = fetch_all(
        conn,
        """
        SELECT queue_bucket, checkpoint_allowed, COUNT(*) AS count
        FROM ml_issue_short_label_release_checkpoint_items
        WHERE checkpoint_run_id = ?
        GROUP BY queue_bucket, checkpoint_allowed
        ORDER BY count DESC, queue_bucket
        """,
        (run_id,),
    )
    by_decision = fetch_all(
        conn,
        """
        SELECT normalized_decision, checkpoint_allowed, COUNT(*) AS count
        FROM ml_issue_short_label_release_checkpoint_items
        WHERE checkpoint_run_id = ?
        GROUP BY normalized_decision, checkpoint_allowed
        ORDER BY count DESC, normalized_decision
        """,
        (run_id,),
    )
    samples = fetch_all(
        conn,
        """
        SELECT relative_path, source_key, source_line_number, queue_bucket, normalized_decision, checkpoint_allowed, block_reason
        FROM ml_issue_short_label_release_checkpoint_items
        WHERE checkpoint_run_id = ?
        ORDER BY checkpoint_allowed DESC, queue_bucket, relative_path, source_line_number, source_key
        LIMIT 12
        """,
        (run_id,),
    )
    allowed = int(run["checkpoint_allowed_count"] or 0)
    candidates = int(run["total_candidates"] or 0)
    return {
        "run": run,
        "by_blocker": by_blocker,
        "by_bucket": by_bucket,
        "by_decision": by_decision,
        "samples": samples,
        "checkpoint_rate": percent(allowed, candidates),
    }


def summarize_macro_model_status(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_model_runs") or not table_exists(conn, "ml_model_registry"):
        return None
    active = fetch_one(
        conn,
        """
        SELECT
            r.model_kind,
            r.active_model_run_id,
            r.active_model_version,
            m.dataset_run_id,
            m.accuracy,
            m.macro_f1,
            m.false_safe_count,
            m.false_safe_rate,
            m.safe_precision,
            m.safe_recall
        FROM ml_model_registry r
        JOIN ml_model_runs m ON m.id = r.active_model_run_id
        WHERE r.model_kind = 'risk_action_classifier'
        """,
    )
    latest = fetch_one(
        conn,
        """
        SELECT
            id,
            model_version,
            dataset_run_id,
            accuracy,
            macro_f1,
            false_safe_count,
            false_safe_rate,
            safe_precision,
            safe_recall,
            model_path,
            finished_at
        FROM ml_model_runs
        WHERE model_kind = 'risk_action_classifier'
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not active and not latest:
        return None
    promotion_hint = "unknown"
    if active and latest:
        active_false_safe = int(active["false_safe_count"] or 0)
        latest_false_safe = int(latest["false_safe_count"] or 0)
        active_recall = float(active["safe_recall"] or 0.0)
        latest_recall = float(latest["safe_recall"] or 0.0)
        if int(latest["id"]) == int(active["active_model_run_id"]):
            promotion_hint = "latest_is_active"
        elif latest_false_safe > active_false_safe:
            promotion_hint = "do_not_promote_false_safe_regression"
        elif latest_recall < active_recall:
            promotion_hint = "do_not_promote_safe_recall_regression"
        else:
            promotion_hint = "candidate_needs_full_score_and_holdout"
    return {
        "active": active,
        "latest": latest,
        "promotion_hint": promotion_hint,
    }


def summarize_latest_holdout_eval(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_holdout_eval_runs"):
        return None
    run = fetch_one(
        conn,
        """
        SELECT *
        FROM ml_holdout_eval_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not run:
        return None
    metrics: dict[str, Any] = {}
    if run.get("metrics_json"):
        try:
            metrics = json.loads(run["metrics_json"])
        except json.JSONDecodeError:
            metrics = {}
    return {
        "run": run,
        "holdout_paths": metrics.get("holdout_paths") or [],
        "false_safe_samples": metrics.get("false_safe_samples") or [],
        "per_path_summary": metrics.get("per_path_summary") or [],
    }


def summarize_latest_model_promotion(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_model_promotions"):
        return None
    row = fetch_one(
        conn,
        """
        SELECT *
        FROM ml_model_promotions
        WHERE model_kind = 'risk_action_classifier'
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not row:
        return None
    metrics: dict[str, Any] = {}
    if row.get("metrics_json"):
        try:
            metrics = json.loads(row["metrics_json"])
        except json.JSONDecodeError:
            metrics = {}
    return {
        "run": row,
        "metrics": metrics,
    }


def summarize_issue_gender_subpolicy_shadow(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_gender_subpolicy_shadow_runs"):
        return None
    run = fetch_one(
        conn,
        """
        SELECT *
        FROM ml_issue_gender_subpolicy_shadow_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not run:
        return None
    run_id = int(run["id"])
    by_subpolicy = fetch_all(
        conn,
        """
        SELECT subpolicy_name, shadow_status, shadow_action, COUNT(*) AS count
        FROM ml_issue_gender_subpolicy_shadow_items
        WHERE run_id = ?
        GROUP BY subpolicy_name, shadow_status, shadow_action
        ORDER BY count DESC, subpolicy_name
        """,
        (run_id,),
    )
    by_status = fetch_all(
        conn,
        """
        SELECT shadow_status, COUNT(*) AS count
        FROM ml_issue_gender_subpolicy_shadow_items
        WHERE run_id = ?
        GROUP BY shadow_status
        ORDER BY count DESC
        """,
        (run_id,),
    )
    samples = fetch_all(
        conn,
        """
        SELECT
            subpolicy_name,
            shadow_status,
            shadow_action,
            relative_path,
            source_key,
            source_line_number,
            issue_kind,
            normalized_decision
        FROM ml_issue_gender_subpolicy_shadow_items
        WHERE run_id = ?
          AND shadow_status LIKE 'shadow_ready%'
        ORDER BY
            CASE WHEN shadow_status = 'shadow_ready_boundary' THEN 0 ELSE 1 END,
            subpolicy_name,
            relative_path,
            source_line_number
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_subpolicy": by_subpolicy,
        "by_status": by_status,
        "samples": samples,
        "ready_rate": percent(int(run["shadow_ready_count"] or 0), int(run["candidate_count"] or 0)),
    }


def summarize_issue_gender_boundary_checkpoint(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_gender_boundary_checkpoint_runs"):
        return None
    run = fetch_one(
        conn,
        """
        SELECT *
        FROM ml_issue_gender_boundary_checkpoint_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not run:
        return None
    run_id = int(run["id"])
    by_subpolicy = fetch_all(
        conn,
        """
        SELECT subpolicy_name, checkpoint_allowed, checkpoint_action, COALESCE(block_reason, '') AS block_reason, COUNT(*) AS count
        FROM ml_issue_gender_boundary_checkpoint_items
        WHERE checkpoint_run_id = ?
        GROUP BY subpolicy_name, checkpoint_allowed, checkpoint_action, COALESCE(block_reason, '')
        ORDER BY count DESC, subpolicy_name
        """,
        (run_id,),
    )
    by_action = fetch_all(
        conn,
        """
        SELECT checkpoint_action, checkpoint_allowed, COUNT(*) AS count
        FROM ml_issue_gender_boundary_checkpoint_items
        WHERE checkpoint_run_id = ?
        GROUP BY checkpoint_action, checkpoint_allowed
        ORDER BY count DESC, checkpoint_action
        """,
        (run_id,),
    )
    samples = fetch_all(
        conn,
        """
        SELECT
            subpolicy_name,
            checkpoint_action,
            checkpoint_allowed,
            block_reason,
            relative_path,
            source_key,
            source_line_number,
            issue_kind,
            normalized_decision
        FROM ml_issue_gender_boundary_checkpoint_items
        WHERE checkpoint_run_id = ?
        ORDER BY checkpoint_allowed DESC, subpolicy_name, relative_path, source_line_number
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_subpolicy": by_subpolicy,
        "by_action": by_action,
        "samples": samples,
        "checkpoint_rate": percent(
            int(run["checkpoint_allowed_count"] or 0),
            int(run["total_candidates"] or 0),
        ),
    }


def summarize_issue_gender_boundary_lifecycle(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_gender_boundary_lifecycle_runs"):
        return None
    run = fetch_one(
        conn,
        """
        SELECT *
        FROM ml_issue_gender_boundary_lifecycle_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not run:
        return None
    run_id = int(run["id"])
    by_subpolicy = fetch_all(
        conn,
        """
        SELECT subpolicy_name, policy_allowed, COALESCE(block_reason, '') AS block_reason, COUNT(*) AS count
        FROM ml_issue_gender_boundary_lifecycle_items
        WHERE run_id = ?
        GROUP BY subpolicy_name, policy_allowed, COALESCE(block_reason, '')
        ORDER BY count DESC, subpolicy_name
        """,
        (run_id,),
    )
    by_action = fetch_all(
        conn,
        """
        SELECT checkpoint_action, policy_allowed, COUNT(*) AS count
        FROM ml_issue_gender_boundary_lifecycle_items
        WHERE run_id = ?
        GROUP BY checkpoint_action, policy_allowed
        ORDER BY count DESC, checkpoint_action
        """,
        (run_id,),
    )
    samples = fetch_all(
        conn,
        """
        SELECT
            subpolicy_name,
            checkpoint_action,
            policy_allowed,
            block_reason,
            relative_path,
            source_key,
            source_line_number,
            issue_kind,
            normalized_decision
        FROM ml_issue_gender_boundary_lifecycle_items
        WHERE run_id = ?
        ORDER BY policy_allowed DESC, subpolicy_name, relative_path, source_line_number
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_subpolicy": by_subpolicy,
        "by_action": by_action,
        "samples": samples,
        "release_rate": percent(
            int(run["released_count"] or 0),
            int(run["candidate_count"] or 0),
        ),
    }


def summarize_issue_trigger_gender_role_surface(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_trigger_gender_role_surface_runs"):
        return None
    run = fetch_one(
        conn,
        """
        SELECT *
        FROM ml_issue_trigger_gender_role_surface_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not run:
        return None
    run_id = int(run["id"])
    by_subpolicy = fetch_all(
        conn,
        """
        SELECT subpolicy_name, shadow_status, shadow_action, COUNT(*) AS count
        FROM ml_issue_trigger_gender_role_surface_items
        WHERE run_id = ?
        GROUP BY subpolicy_name, shadow_status, shadow_action
        ORDER BY count DESC, subpolicy_name
        """,
        (run_id,),
    )
    by_status = fetch_all(
        conn,
        """
        SELECT shadow_status, COUNT(*) AS count
        FROM ml_issue_trigger_gender_role_surface_items
        WHERE run_id = ?
        GROUP BY shadow_status
        ORDER BY count DESC
        """,
        (run_id,),
    )
    samples = fetch_all(
        conn,
        """
        SELECT
            subpolicy_name,
            shadow_status,
            shadow_action,
            relative_path,
            source_key,
            source_line_number,
            issue_kind,
            normalized_decision
        FROM ml_issue_trigger_gender_role_surface_items
        WHERE run_id = ?
          AND shadow_status LIKE 'shadow_ready%'
        ORDER BY subpolicy_name, relative_path, source_line_number
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_subpolicy": by_subpolicy,
        "by_status": by_status,
        "samples": samples,
        "ready_rate": percent(int(run["shadow_ready_count"] or 0), int(run["candidate_count"] or 0)),
        "analytics": parse_json_dict(run.get("analytics_json")),
    }


def summarize_issue_trigger_gender_role_checkpoint(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_trigger_gender_role_checkpoint_runs"):
        return None
    run = fetch_one(
        conn,
        """
        SELECT *
        FROM ml_issue_trigger_gender_role_checkpoint_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not run:
        return None
    run_id = int(run["id"])
    by_subpolicy = fetch_all(
        conn,
        """
        SELECT subpolicy_name, checkpoint_allowed, checkpoint_action, COALESCE(block_reason, '') AS block_reason, COUNT(*) AS count
        FROM ml_issue_trigger_gender_role_checkpoint_items
        WHERE checkpoint_run_id = ?
        GROUP BY subpolicy_name, checkpoint_allowed, checkpoint_action, COALESCE(block_reason, '')
        ORDER BY count DESC, subpolicy_name
        """,
        (run_id,),
    )
    by_action = fetch_all(
        conn,
        """
        SELECT checkpoint_action, checkpoint_allowed, COUNT(*) AS count
        FROM ml_issue_trigger_gender_role_checkpoint_items
        WHERE checkpoint_run_id = ?
        GROUP BY checkpoint_action, checkpoint_allowed
        ORDER BY count DESC, checkpoint_action
        """,
        (run_id,),
    )
    samples = fetch_all(
        conn,
        """
        SELECT
            subpolicy_name,
            checkpoint_action,
            checkpoint_allowed,
            block_reason,
            relative_path,
            source_key,
            source_line_number,
            issue_kind,
            normalized_decision
        FROM ml_issue_trigger_gender_role_checkpoint_items
        WHERE checkpoint_run_id = ?
        ORDER BY checkpoint_allowed DESC, subpolicy_name, relative_path, source_line_number
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_subpolicy": by_subpolicy,
        "by_action": by_action,
        "samples": samples,
        "checkpoint_rate": percent(
            int(run["checkpoint_allowed_count"] or 0),
            int(run["total_candidates"] or 0),
        ),
    }


def summarize_issue_trigger_gender_role_lifecycle(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_trigger_gender_role_lifecycle_runs"):
        return None
    run = fetch_one(
        conn,
        """
        SELECT *
        FROM ml_issue_trigger_gender_role_lifecycle_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not run:
        return None
    run_id = int(run["id"])
    by_subpolicy = fetch_all(
        conn,
        """
        SELECT subpolicy_name, policy_allowed, COALESCE(block_reason, '') AS block_reason, COUNT(*) AS count
        FROM ml_issue_trigger_gender_role_lifecycle_items
        WHERE run_id = ?
        GROUP BY subpolicy_name, policy_allowed, COALESCE(block_reason, '')
        ORDER BY count DESC, subpolicy_name
        """,
        (run_id,),
    )
    by_action = fetch_all(
        conn,
        """
        SELECT checkpoint_action, policy_allowed, COUNT(*) AS count
        FROM ml_issue_trigger_gender_role_lifecycle_items
        WHERE run_id = ?
        GROUP BY checkpoint_action, policy_allowed
        ORDER BY count DESC, checkpoint_action
        """,
        (run_id,),
    )
    samples = fetch_all(
        conn,
        """
        SELECT
            subpolicy_name,
            checkpoint_action,
            policy_allowed,
            block_reason,
            relative_path,
            source_key,
            source_line_number,
            issue_kind,
            normalized_decision
        FROM ml_issue_trigger_gender_role_lifecycle_items
        WHERE run_id = ?
        ORDER BY policy_allowed DESC, subpolicy_name, relative_path, source_line_number
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_subpolicy": by_subpolicy,
        "by_action": by_action,
        "samples": samples,
        "release_rate": percent(
            int(run["released_count"] or 0),
            int(run["candidate_count"] or 0),
        ),
    }


def summarize_issue_long_text_repair_route_checkpoint(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_long_text_repair_route_checkpoint_runs"):
        return None
    run = fetch_one(
        conn,
        """
        SELECT *
        FROM ml_issue_long_text_repair_route_checkpoint_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not run:
        return None
    run_id = int(run["id"])
    by_route = fetch_all(
        conn,
        """
        SELECT repair_route, checkpoint_allowed, COALESCE(block_reason, '') AS block_reason, COUNT(*) AS count
        FROM ml_issue_long_text_repair_route_checkpoint_items
        WHERE run_id = ?
        GROUP BY repair_route, checkpoint_allowed, COALESCE(block_reason, '')
        ORDER BY checkpoint_allowed DESC, count DESC, repair_route
        """,
        (run_id,),
    )
    by_token = fetch_all(
        conn,
        """
        SELECT token_status, checkpoint_allowed, COUNT(*) AS count
        FROM ml_issue_long_text_repair_route_checkpoint_items
        WHERE run_id = ?
        GROUP BY token_status, checkpoint_allowed
        ORDER BY checkpoint_allowed DESC, count DESC, token_status
        """,
        (run_id,),
    )
    samples = fetch_all(
        conn,
        """
        SELECT
            repair_route,
            token_status,
            checkpoint_allowed,
            block_reason,
            relative_path,
            source_key,
            source_line_number,
            normalized_decision
        FROM ml_issue_long_text_repair_route_checkpoint_items
        WHERE run_id = ?
        ORDER BY checkpoint_allowed DESC, repair_route, relative_path, source_line_number
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_route": by_route,
        "by_token": by_token,
        "samples": samples,
        "checkpoint_rate": percent(
            int(run["allowed_count"] or 0),
            int(run["candidate_count"] or 0),
        ),
    }


def summarize_issue_long_text_repair_route_lifecycle(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_long_text_repair_route_lifecycle_runs"):
        return None
    run = fetch_one(
        conn,
        """
        SELECT *
        FROM ml_issue_long_text_repair_route_lifecycle_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not run:
        return None
    run_id = int(run["id"])
    by_route = fetch_all(
        conn,
        """
        SELECT repair_route, policy_allowed, COALESCE(block_reason, '') AS block_reason, COUNT(*) AS count
        FROM ml_issue_long_text_repair_route_lifecycle_items
        WHERE run_id = ?
        GROUP BY repair_route, policy_allowed, COALESCE(block_reason, '')
        ORDER BY policy_allowed DESC, count DESC, repair_route
        """,
        (run_id,),
    )
    by_token = fetch_all(
        conn,
        """
        SELECT token_status, policy_allowed, COUNT(*) AS count
        FROM ml_issue_long_text_repair_route_lifecycle_items
        WHERE run_id = ?
        GROUP BY token_status, policy_allowed
        ORDER BY policy_allowed DESC, count DESC, token_status
        """,
        (run_id,),
    )
    samples = fetch_all(
        conn,
        """
        SELECT
            repair_route,
            token_status,
            policy_allowed,
            block_reason,
            relative_path,
            source_key,
            source_line_number
        FROM ml_issue_long_text_repair_route_lifecycle_items
        WHERE run_id = ?
        ORDER BY policy_allowed DESC, repair_route, relative_path, source_line_number
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_route": by_route,
        "by_token": by_token,
        "samples": samples,
        "release_rate": percent(
            int(run["released_count"] or 0),
            int(run["candidate_count"] or 0),
        ),
        "route_counts": parse_json_dict(run.get("route_counts_json")),
    }


def summarize_issue_long_text_structural_subpolicy_shadow(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_long_text_structural_subpolicy_shadow_runs"):
        return None
    run = fetch_one(
        conn,
        """
        SELECT *
        FROM ml_issue_long_text_structural_subpolicy_shadow_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not run:
        return None
    run_id = int(run["id"])
    by_subpolicy = fetch_all(
        conn,
        """
        SELECT subpolicy_name, shadow_status, shadow_action, COALESCE(block_reason, '') AS block_reason, COUNT(*) AS count
        FROM ml_issue_long_text_structural_subpolicy_shadow_items
        WHERE run_id = ?
        GROUP BY subpolicy_name, shadow_status, shadow_action, COALESCE(block_reason, '')
        ORDER BY shadow_status DESC, count DESC, subpolicy_name
        """,
        (run_id,),
    )
    by_route = fetch_all(
        conn,
        """
        SELECT repair_route, shadow_status, COUNT(*) AS count
        FROM ml_issue_long_text_structural_subpolicy_shadow_items
        WHERE run_id = ?
        GROUP BY repair_route, shadow_status
        ORDER BY shadow_status DESC, count DESC, repair_route
        """,
        (run_id,),
    )
    samples = fetch_all(
        conn,
        """
        SELECT
            subpolicy_name,
            shadow_status,
            shadow_action,
            block_reason,
            relative_path,
            source_key,
            source_line_number,
            repair_route,
            token_delta_json
        FROM ml_issue_long_text_structural_subpolicy_shadow_items
        WHERE run_id = ?
        ORDER BY shadow_ready DESC, subpolicy_name, relative_path, source_line_number
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_subpolicy": by_subpolicy,
        "by_route": by_route,
        "samples": samples,
        "ready_rate": percent(
            int(run["shadow_ready_count"] or 0),
            int(run["candidate_count"] or 0),
        ),
        "subpolicy_counts": parse_json_dict(run.get("subpolicy_counts_json")),
    }


def summarize_issue_long_text_structural_subpolicy_checkpoint(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_long_text_structural_subpolicy_checkpoint_runs"):
        return None
    run = fetch_one(
        conn,
        """
        SELECT *
        FROM ml_issue_long_text_structural_subpolicy_checkpoint_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not run:
        return None
    run_id = int(run["id"])
    by_subpolicy = fetch_all(
        conn,
        """
        SELECT subpolicy_name, checkpoint_action, checkpoint_allowed, COALESCE(block_reason, '') AS block_reason, COUNT(*) AS count
        FROM ml_issue_long_text_structural_subpolicy_checkpoint_items
        WHERE run_id = ?
        GROUP BY subpolicy_name, checkpoint_action, checkpoint_allowed, COALESCE(block_reason, '')
        ORDER BY checkpoint_allowed DESC, count DESC, subpolicy_name
        """,
        (run_id,),
    )
    samples = fetch_all(
        conn,
        """
        SELECT
            subpolicy_name,
            checkpoint_action,
            checkpoint_allowed,
            block_reason,
            relative_path,
            source_key,
            source_line_number,
            repair_route,
            token_delta_json
        FROM ml_issue_long_text_structural_subpolicy_checkpoint_items
        WHERE run_id = ?
        ORDER BY checkpoint_allowed DESC, subpolicy_name, relative_path, source_line_number
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_subpolicy": by_subpolicy,
        "samples": samples,
        "checkpoint_rate": percent(
            int(run["checkpoint_allowed_count"] or 0),
            int(run["total_candidates"] or 0),
        ),
        "subpolicy_counts": parse_json_dict(run.get("subpolicy_counts_json")),
    }


def summarize_issue_long_text_structural_subpolicy_lifecycle(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_long_text_structural_subpolicy_lifecycle_runs"):
        return None
    run = fetch_one(
        conn,
        """
        SELECT *
        FROM ml_issue_long_text_structural_subpolicy_lifecycle_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not run:
        return None
    run_id = int(run["id"])
    by_subpolicy = fetch_all(
        conn,
        """
        SELECT subpolicy_name, checkpoint_action, policy_allowed, COALESCE(block_reason, '') AS block_reason, COUNT(*) AS count
        FROM ml_issue_long_text_structural_subpolicy_lifecycle_items
        WHERE run_id = ?
        GROUP BY subpolicy_name, checkpoint_action, policy_allowed, COALESCE(block_reason, '')
        ORDER BY policy_allowed DESC, count DESC, subpolicy_name
        """,
        (run_id,),
    )
    samples = fetch_all(
        conn,
        """
        SELECT
            subpolicy_name,
            checkpoint_action,
            policy_allowed,
            block_reason,
            relative_path,
            source_key,
            source_line_number,
            repair_route,
            token_delta_json
        FROM ml_issue_long_text_structural_subpolicy_lifecycle_items
        WHERE run_id = ?
        ORDER BY policy_allowed DESC, subpolicy_name, relative_path, source_line_number
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_subpolicy": by_subpolicy,
        "samples": samples,
        "release_rate": percent(
            int(run["released_count"] or 0),
            int(run["candidate_count"] or 0),
        ),
        "subpolicy_counts": parse_json_dict(run.get("subpolicy_counts_json")),
    }


def summarize_issue_long_text_mixed_structural_split(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_long_text_mixed_structural_split_runs"):
        return None
    run = fetch_one(
        conn,
        """
        SELECT *
        FROM ml_issue_long_text_mixed_structural_split_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not run:
        return None
    run_id = int(run["id"])
    by_microagent = fetch_all(
        conn,
        """
        SELECT microagent_key, split_status, split_ready, COUNT(*) AS count
        FROM ml_issue_long_text_mixed_structural_split_items
        WHERE run_id = ?
        GROUP BY microagent_key, split_status, split_ready
        ORDER BY split_ready DESC, count DESC, microagent_key
        """,
        (run_id,),
    )
    by_source = fetch_all(
        conn,
        """
        SELECT original_subpolicy_name, split_status, COUNT(*) AS count
        FROM ml_issue_long_text_mixed_structural_split_items
        WHERE run_id = ?
        GROUP BY original_subpolicy_name, split_status
        ORDER BY count DESC, original_subpolicy_name
        """,
        (run_id,),
    )
    samples = fetch_all(
        conn,
        """
        SELECT
            microagent_key,
            micro_issue_kind,
            split_status,
            split_action,
            split_ready,
            original_subpolicy_name,
            relative_path,
            source_key,
            source_line_number
        FROM ml_issue_long_text_mixed_structural_split_items
        WHERE run_id = ?
        ORDER BY split_ready DESC, priority DESC, microagent_key, relative_path, source_line_number
        LIMIT 25
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_microagent": by_microagent,
        "by_source": by_source,
        "samples": samples,
        "unit_per_blocker_rate": percent(
            int(run["split_unit_count"] or 0),
            int(run["candidate_count"] or 0),
        ),
        "split_ready_rate": percent(
            int(run["split_ready_count"] or 0),
            int(run["split_unit_count"] or 0),
        ),
        "microagent_counts": parse_json_dict(run.get("microagent_counts_json")),
        "status_counts": parse_json_dict(run.get("status_counts_json")),
    }


def summarize_issue_long_text_mixed_structural_split_checkpoint(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_long_text_mixed_structural_split_checkpoint_runs"):
        return None
    run = fetch_one(
        conn,
        """
        SELECT *
        FROM ml_issue_long_text_mixed_structural_split_checkpoint_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not run:
        return None
    run_id = int(run["id"])
    by_microagent = fetch_all(
        conn,
        """
        SELECT microagent_key, checkpoint_action, checkpoint_allowed, COALESCE(block_reason, '') AS block_reason, COUNT(*) AS count
        FROM ml_issue_long_text_mixed_structural_split_checkpoint_items
        WHERE run_id = ?
        GROUP BY microagent_key, checkpoint_action, checkpoint_allowed, COALESCE(block_reason, '')
        ORDER BY checkpoint_allowed DESC, count DESC, microagent_key
        """,
        (run_id,),
    )
    samples = fetch_all(
        conn,
        """
        SELECT
            microagent_key,
            micro_issue_kind,
            checkpoint_action,
            checkpoint_allowed,
            block_reason,
            partial_component_only,
            production_release_allowed,
            relative_path,
            source_key,
            source_line_number
        FROM ml_issue_long_text_mixed_structural_split_checkpoint_items
        WHERE run_id = ?
        ORDER BY checkpoint_allowed DESC, microagent_key, relative_path, source_line_number
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_microagent": by_microagent,
        "samples": samples,
        "checkpoint_rate": percent(
            int(run["checkpoint_allowed_count"] or 0),
            int(run["candidate_count"] or 0),
        ),
        "microagent_counts": parse_json_dict(run.get("microagent_counts_json")),
    }


def summarize_issue_long_text_mixed_structural_split_lifecycle(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_long_text_mixed_structural_split_lifecycle_runs"):
        return None
    run = fetch_one(
        conn,
        """
        SELECT *
        FROM ml_issue_long_text_mixed_structural_split_lifecycle_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not run:
        return None
    run_id = int(run["id"])
    by_microagent = fetch_all(
        conn,
        """
        SELECT microagent_key, policy_action, policy_allowed, COALESCE(block_reason, '') AS block_reason, COUNT(*) AS count
        FROM ml_issue_long_text_mixed_structural_split_lifecycle_items
        WHERE run_id = ?
        GROUP BY microagent_key, policy_action, policy_allowed, COALESCE(block_reason, '')
        ORDER BY policy_allowed DESC, count DESC, microagent_key
        """,
        (run_id,),
    )
    samples = fetch_all(
        conn,
        """
        SELECT
            microagent_key,
            micro_issue_kind,
            policy_action,
            policy_allowed,
            block_reason,
            partial_component_only,
            production_release_allowed,
            relative_path,
            source_key,
            source_line_number
        FROM ml_issue_long_text_mixed_structural_split_lifecycle_items
        WHERE run_id = ?
        ORDER BY policy_allowed DESC, microagent_key, relative_path, source_line_number
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_microagent": by_microagent,
        "samples": samples,
        "release_rate": percent(
            int(run["released_count"] or 0),
            int(run["candidate_count"] or 0),
        ),
        "microagent_counts": parse_json_dict(run.get("microagent_counts_json")),
    }


def summarize_issue_long_text_mixed_structural_token_policy_shadow(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_long_text_mixed_structural_token_policy_shadow_runs"):
        return None
    run = fetch_one(
        conn,
        """
        SELECT *
        FROM ml_issue_long_text_mixed_structural_token_policy_shadow_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not run:
        return None
    run_id = int(run["id"])
    by_microagent = fetch_all(
        conn,
        """
        SELECT
            microagent_key,
            token_policy_status,
            token_policy_action,
            COALESCE(superseded_by, '') AS superseded_by,
            COALESCE(block_reason, '') AS block_reason,
            COUNT(*) AS count
        FROM ml_issue_long_text_mixed_structural_token_policy_shadow_items
        WHERE run_id = ?
        GROUP BY microagent_key, token_policy_status, token_policy_action, COALESCE(superseded_by, ''), COALESCE(block_reason, '')
        ORDER BY token_policy_status DESC, count DESC, microagent_key
        """,
        (run_id,),
    )
    samples = fetch_all(
        conn,
        """
        SELECT
            microagent_key,
            micro_issue_kind,
            token_policy_status,
            token_policy_action,
            shadow_ready,
            superseded_by,
            block_reason,
            partial_component_only,
            production_release_allowed,
            relative_path,
            source_key,
            source_line_number
        FROM ml_issue_long_text_mixed_structural_token_policy_shadow_items
        WHERE run_id = ?
        ORDER BY shadow_ready DESC, token_policy_status DESC, microagent_key, relative_path, source_line_number
        LIMIT 25
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_microagent": by_microagent,
        "samples": samples,
        "shadow_ready_rate": percent(
            int(run["shadow_ready_count"] or 0),
            int(run["candidate_count"] or 0),
        ),
        "effective_block_rate": percent(
            int(run["blocked_count"] or 0),
            int(run["candidate_count"] or 0),
        ),
        "microagent_counts": parse_json_dict(run.get("microagent_counts_json")),
        "status_counts": parse_json_dict(run.get("status_counts_json")),
    }


def summarize_issue_long_text_mixed_structural_token_policy_checkpoint(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_long_text_mixed_structural_token_policy_checkpoint_runs"):
        return None
    run = fetch_one(
        conn,
        """
        SELECT *
        FROM ml_issue_long_text_mixed_structural_token_policy_checkpoint_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not run:
        return None
    run_id = int(run["id"])
    by_microagent = fetch_all(
        conn,
        """
        SELECT
            microagent_key,
            checkpoint_action,
            checkpoint_allowed,
            COALESCE(block_reason, '') AS block_reason,
            COUNT(*) AS count
        FROM ml_issue_long_text_mixed_structural_token_policy_checkpoint_items
        WHERE run_id = ?
        GROUP BY microagent_key, checkpoint_action, checkpoint_allowed, COALESCE(block_reason, '')
        ORDER BY checkpoint_allowed DESC, count DESC, microagent_key
        """,
        (run_id,),
    )
    samples = fetch_all(
        conn,
        """
        SELECT
            microagent_key,
            micro_issue_kind,
            token_policy_action,
            checkpoint_action,
            checkpoint_allowed,
            block_reason,
            partial_component_only,
            production_release_allowed,
            relative_path,
            source_key,
            source_line_number
        FROM ml_issue_long_text_mixed_structural_token_policy_checkpoint_items
        WHERE run_id = ?
        ORDER BY checkpoint_allowed DESC, microagent_key, relative_path, source_line_number
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_microagent": by_microagent,
        "samples": samples,
        "checkpoint_rate": percent(
            int(run["checkpoint_allowed_count"] or 0),
            int(run["candidate_count"] or 0),
        ),
        "microagent_counts": parse_json_dict(run.get("microagent_counts_json")),
        "checkpoint_action_counts": parse_json_dict(run.get("checkpoint_action_counts_json")),
    }


def summarize_issue_long_text_mixed_structural_token_policy_lifecycle(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_long_text_mixed_structural_token_policy_lifecycle_runs"):
        return None
    run = fetch_one(
        conn,
        """
        SELECT *
        FROM ml_issue_long_text_mixed_structural_token_policy_lifecycle_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not run:
        return None
    run_id = int(run["id"])
    by_microagent = fetch_all(
        conn,
        """
        SELECT
            microagent_key,
            policy_action,
            policy_allowed,
            COALESCE(block_reason, '') AS block_reason,
            COUNT(*) AS count
        FROM ml_issue_long_text_mixed_structural_token_policy_lifecycle_items
        WHERE run_id = ?
        GROUP BY microagent_key, policy_action, policy_allowed, COALESCE(block_reason, '')
        ORDER BY policy_allowed DESC, count DESC, microagent_key
        """,
        (run_id,),
    )
    samples = fetch_all(
        conn,
        """
        SELECT
            microagent_key,
            micro_issue_kind,
            checkpoint_action,
            policy_action,
            policy_allowed,
            block_reason,
            partial_component_only,
            production_release_allowed,
            relative_path,
            source_key,
            source_line_number
        FROM ml_issue_long_text_mixed_structural_token_policy_lifecycle_items
        WHERE run_id = ?
        ORDER BY policy_allowed DESC, microagent_key, relative_path, source_line_number
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_microagent": by_microagent,
        "samples": samples,
        "release_rate": percent(
            int(run["released_count"] or 0),
            int(run["candidate_count"] or 0),
        ),
        "microagent_counts": parse_json_dict(run.get("microagent_counts_json")),
        "policy_action_counts": parse_json_dict(run.get("policy_action_counts_json")),
    }


def summarize_issue_long_text_mixed_structural_token_policy_blocker_subsplit(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_long_text_mixed_structural_token_policy_blocker_subsplit_runs"):
        return None
    run = fetch_one(
        conn,
        """
        SELECT *
        FROM ml_issue_long_text_mixed_structural_token_policy_blocker_subsplit_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not run:
        return None
    run_id = int(run["id"])
    by_microagent = fetch_all(
        conn,
        """
        SELECT
            microagent_key,
            subsplit_status,
            subsplit_action,
            COALESCE(block_reason, '') AS block_reason,
            COALESCE(review_route, '') AS review_route,
            COUNT(*) AS count
        FROM ml_issue_long_text_mixed_structural_token_policy_blocker_subsplit_items
        WHERE run_id = ?
        GROUP BY microagent_key, subsplit_status, subsplit_action, COALESCE(block_reason, ''), COALESCE(review_route, '')
        ORDER BY subsplit_status DESC, count DESC, microagent_key
        """,
        (run_id,),
    )
    samples = fetch_all(
        conn,
        """
        SELECT
            microagent_key,
            micro_issue_kind,
            subcomponent_kind,
            subsplit_status,
            subsplit_action,
            subsplit_ready,
            block_reason,
            review_route,
            partial_component_only,
            production_release_allowed,
            relative_path,
            source_key,
            source_line_number
        FROM ml_issue_long_text_mixed_structural_token_policy_blocker_subsplit_items
        WHERE run_id = ?
        ORDER BY subsplit_ready DESC, subsplit_status DESC, microagent_key, relative_path, source_line_number
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_microagent": by_microagent,
        "samples": samples,
        "ready_rate": percent(
            int(run["subsplit_ready_count"] or 0),
            int(run["subsplit_unit_count"] or 0),
        ),
        "microagent_counts": parse_json_dict(run.get("microagent_counts_json")),
        "status_counts": parse_json_dict(run.get("status_counts_json")),
    }


def summarize_issue_long_text_subject_pronoun_form_checkpoint(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_long_text_subject_pronoun_form_checkpoint_runs"):
        return None
    run = fetch_one(
        conn,
        """
        SELECT *
        FROM ml_issue_long_text_subject_pronoun_form_checkpoint_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not run:
        return None
    run_id = int(run["id"])
    by_microagent = fetch_all(
        conn,
        """
        SELECT
            microagent_key,
            checkpoint_action,
            checkpoint_allowed,
            COALESCE(block_reason, '') AS block_reason,
            COUNT(*) AS count
        FROM ml_issue_long_text_subject_pronoun_form_checkpoint_items
        WHERE run_id = ?
        GROUP BY microagent_key, checkpoint_action, checkpoint_allowed, COALESCE(block_reason, '')
        ORDER BY checkpoint_allowed DESC, count DESC, microagent_key
        """,
        (run_id,),
    )
    samples = fetch_all(
        conn,
        """
        SELECT
            microagent_key,
            micro_issue_kind,
            subcomponent_kind,
            subsplit_action,
            checkpoint_action,
            checkpoint_allowed,
            block_reason,
            review_route,
            partial_component_only,
            production_release_allowed,
            relative_path,
            source_key,
            source_line_number
        FROM ml_issue_long_text_subject_pronoun_form_checkpoint_items
        WHERE run_id = ?
        ORDER BY checkpoint_allowed DESC, microagent_key, relative_path, source_line_number
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_microagent": by_microagent,
        "samples": samples,
        "checkpoint_rate": percent(
            int(run["checkpoint_allowed_count"] or 0),
            int(run["candidate_count"] or 0),
        ),
        "microagent_counts": parse_json_dict(run.get("microagent_counts_json")),
        "checkpoint_action_counts": parse_json_dict(run.get("checkpoint_action_counts_json")),
    }


def summarize_issue_long_text_subject_pronoun_form_lifecycle(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_long_text_subject_pronoun_form_lifecycle_runs"):
        return None
    run = fetch_one(
        conn,
        """
        SELECT *
        FROM ml_issue_long_text_subject_pronoun_form_lifecycle_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not run:
        return None
    run_id = int(run["id"])
    by_microagent = fetch_all(
        conn,
        """
        SELECT
            microagent_key,
            policy_action,
            policy_allowed,
            COALESCE(block_reason, '') AS block_reason,
            COUNT(*) AS count
        FROM ml_issue_long_text_subject_pronoun_form_lifecycle_items
        WHERE run_id = ?
        GROUP BY microagent_key, policy_action, policy_allowed, COALESCE(block_reason, '')
        ORDER BY policy_allowed DESC, count DESC, microagent_key
        """,
        (run_id,),
    )
    samples = fetch_all(
        conn,
        """
        SELECT
            microagent_key,
            micro_issue_kind,
            subcomponent_kind,
            checkpoint_action,
            policy_action,
            policy_allowed,
            block_reason,
            review_route,
            partial_component_only,
            production_release_allowed,
            relative_path,
            source_key,
            source_line_number
        FROM ml_issue_long_text_subject_pronoun_form_lifecycle_items
        WHERE run_id = ?
        ORDER BY policy_allowed DESC, microagent_key, relative_path, source_line_number
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_microagent": by_microagent,
        "samples": samples,
        "release_rate": percent(
            int(run["released_count"] or 0),
            int(run["candidate_count"] or 0),
        ),
        "microagent_counts": parse_json_dict(run.get("microagent_counts_json")),
        "policy_action_counts": parse_json_dict(run.get("policy_action_counts_json")),
    }


def summarize_issue_long_text_partial_composition_impact(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_long_text_partial_composition_impact_runs"):
        return None
    run = fetch_one(
        conn,
        """
        SELECT *
        FROM ml_issue_long_text_partial_composition_impact_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not run:
        return None
    run_id = int(run["id"])
    by_state = fetch_all(
        conn,
        """
        SELECT
            coverage_state,
            COUNT(*) AS segment_count,
            SUM(released_component_count) AS released_component_count,
            SUM(blocker_count) AS blocker_count,
            SUM(potential_close_after_recheck) AS potential_close_after_recheck
        FROM ml_issue_long_text_partial_composition_impact_items
        WHERE run_id = ?
        GROUP BY coverage_state
        ORDER BY potential_close_after_recheck DESC, segment_count DESC, coverage_state
        """,
        (run_id,),
    )
    samples = fetch_all(
        conn,
        """
        SELECT
            coverage_state,
            normalized_decision,
            released_component_count,
            blocker_count,
            potential_close_after_recheck,
            relative_path,
            source_key,
            source_line_number
        FROM ml_issue_long_text_partial_composition_impact_items
        WHERE run_id = ?
        ORDER BY potential_close_after_recheck DESC, blocker_count ASC, released_component_count DESC, segment_id
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_state": by_state,
        "samples": samples,
        "potential_close_rate": percent(
            int(run["potential_close_after_recheck_count"] or 0),
            int(run["candidate_segment_count"] or 0),
        ),
        "component_segment_rate": percent(
            int(run["released_component_segment_count"] or 0),
            int(run["candidate_segment_count"] or 0),
        ),
        "component_source_counts": parse_json_dict(run.get("component_source_counts_json")),
        "blocker_route_counts": parse_json_dict(run.get("blocker_route_counts_json")),
        "coverage_state_counts": parse_json_dict(run.get("coverage_state_counts_json")),
    }


def summarize_issue_long_text_composition_recheck_queue(conn) -> dict[str, Any] | None:
    if not table_exists(conn, "ml_issue_long_text_composition_recheck_queue_runs"):
        return None
    run = fetch_one(
        conn,
        """
        SELECT *
        FROM ml_issue_long_text_composition_recheck_queue_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    if not run:
        return None
    run_id = int(run["id"])
    by_bucket = fetch_all(
        conn,
        """
        SELECT
            queue_bucket,
            suggested_decision,
            COUNT(*) AS count,
            SUM(released_component_count) AS released_component_count
        FROM ml_issue_long_text_composition_recheck_queue_items
        WHERE run_id = ?
        GROUP BY queue_bucket, suggested_decision
        ORDER BY count DESC, queue_bucket
        """,
        (run_id,),
    )
    samples = fetch_all(
        conn,
        """
        SELECT
            review_queue_item_id,
            segment_id,
            relative_path,
            source_key,
            source_line_number,
            released_component_count,
            queue_bucket,
            suggested_decision,
            priority_score
        FROM ml_issue_long_text_composition_recheck_queue_items
        WHERE run_id = ?
        ORDER BY priority_score DESC, segment_id
        LIMIT 20
        """,
        (run_id,),
    )
    return {
        "run": run,
        "by_bucket": by_bucket,
        "samples": samples,
        "selected_rate": percent(
            int(run["selected_count"] or 0),
            int(run["candidate_count"] or 0),
        ),
        "component_source_counts": parse_json_dict(run.get("component_source_counts_json")),
        "bucket_counts": parse_json_dict(run.get("bucket_counts_json")),
    }


def build_next_actions(
    text_audit: dict[str, Any] | None,
    weak_auto_split_agents: dict[str, Any] | None,
    text_shadow_policies: dict[str, Any] | None,
    text_boundary_policies: dict[str, Any] | None,
    text_boundary_repair_queues: dict[str, Any] | None,
    text_boundary_repair_shadow_policies: dict[str, Any] | None,
    text_boundary_repair_checkpoints: dict[str, Any] | None,
    text_boundary_repair_lifecycle_policies: dict[str, Any] | None,
    text_boundary_token_policy_bridges: dict[str, Any] | None,
    text_boundary_token_subpolicy_shadows: dict[str, Any] | None,
    text_boundary_token_subpolicy_checkpoints: dict[str, Any] | None,
    text_boundary_token_subpolicy_lifecycle_policies: dict[str, Any] | None,
    text_boundary_token_subpolicy_production_audits: dict[str, Any] | None,
    text_boundary_repair_production_audits: dict[str, Any] | None,
    text_policy_checkpoints: dict[str, Any] | None,
    reopen_lifecycle_policies: dict[str, Any] | None,
    divine_realm_policy: dict[str, Any] | None,
    boundary_correction_queues: dict[str, Any] | None,
    recommendations: dict[str, Any] | None,
    specialist_policy: dict[str, Any] | None,
    issue_ledger: dict[str, Any] | None,
    issue_partial_coverage: dict[str, Any] | None,
    issue_review_queue: dict[str, Any] | None,
    issue_review_decisions: dict[str, Any] | None,
    issue_review_dataset_bridge: dict[str, Any] | None,
    short_label_positive_release: dict[str, Any] | None,
    short_label_positive_checkpoint: dict[str, Any] | None,
    issue_gender_subpolicy_shadow: dict[str, Any] | None,
    issue_gender_boundary_checkpoint: dict[str, Any] | None,
    issue_gender_boundary_lifecycle: dict[str, Any] | None,
    issue_trigger_gender_role_surface: dict[str, Any] | None,
    issue_trigger_gender_role_checkpoint: dict[str, Any] | None,
    issue_trigger_gender_role_lifecycle: dict[str, Any] | None,
    issue_long_text_repair_route_checkpoint: dict[str, Any] | None,
    issue_long_text_repair_route_lifecycle: dict[str, Any] | None,
    issue_long_text_structural_subpolicy_shadow: dict[str, Any] | None,
    issue_long_text_structural_subpolicy_checkpoint: dict[str, Any] | None,
    issue_long_text_structural_subpolicy_lifecycle: dict[str, Any] | None,
    issue_long_text_mixed_structural_split: dict[str, Any] | None,
    issue_long_text_mixed_structural_split_checkpoint: dict[str, Any] | None,
    issue_long_text_mixed_structural_split_lifecycle: dict[str, Any] | None,
    issue_long_text_mixed_structural_token_policy_shadow: dict[str, Any] | None,
    issue_long_text_mixed_structural_token_policy_checkpoint: dict[str, Any] | None,
    issue_long_text_mixed_structural_token_policy_lifecycle: dict[str, Any] | None,
    issue_long_text_mixed_structural_token_policy_blocker_subsplit: dict[str, Any] | None,
    issue_long_text_subject_pronoun_form_checkpoint: dict[str, Any] | None,
    issue_long_text_subject_pronoun_form_lifecycle: dict[str, Any] | None,
    issue_long_text_partial_composition_impact: dict[str, Any] | None,
    issue_long_text_composition_recheck_queue: dict[str, Any] | None,
    macro_model_status: dict[str, Any] | None,
    latest_holdout_eval: dict[str, Any] | None,
    latest_model_promotion: dict[str, Any] | None,
    active_policy_gap: dict[str, Any] | None,
) -> list[str]:
    actions: list[str] = []
    if latest_holdout_eval:
        run = latest_holdout_eval["run"]
        false_safe = int(run["false_safe_count"] or 0)
        if false_safe:
            top_paths = "; ".join(
                str(row).lstrip("- ").strip()
                for row in latest_holdout_eval.get("per_path_summary", [])[:2]
            )
            suffix = f" Top riscos: {top_paths}." if top_paths else ""
            actions.append(
                f"Bloquear promocao macro: holdout dataset {run['dataset_run_id']} tem {false_safe:,} falso-seguros; priorizar especialistas dos caminhos de maior risco.{suffix}"
            )
    if latest_model_promotion:
        run = latest_model_promotion["run"]
        if run["decision"] in {"rejected", "do_not_promote"} and "operational_coverage_regression" in (run["reason"] or ""):
            actions.append(
                f"Macro candidato {run['candidate_model_run_id']} seguro, mas ainda sem promocao por regressao de cobertura operacional."
            )
    if macro_model_status and macro_model_status.get("promotion_hint") == "do_not_promote_false_safe_regression":
        latest = macro_model_status["latest"]
        actions.append(
            f"Nao promover modelo macro candidato {latest['id']}: regressao de falso seguro detectada."
        )
    if issue_review_dataset_bridge:
        run = issue_review_dataset_bridge["run"]
        trainable = int(issue_review_dataset_bridge["trainable_count"] or 0)
        actions.append(
            f"Dataset {run['id']} ja inclui ponte issue_review_decision: {trainable:,} exemplos treinaveis para o macro."
        )
    if short_label_positive_release:
        run = short_label_positive_release["run"]
        released = int(run["released_shadow_count"] or 0)
        blocked = int(run["blocked_count"] or 0)
        if released and not blocked:
            actions.append(
                f"Subpolitica {run['policy_name']} esta limpa em shadow: {released:,} liberados, ganho estimado {int(run['estimated_closed_gain'] or 0):,}."
            )
        elif blocked:
            actions.append(
                f"Auditar bloqueios da subpolitica {run['policy_name']}: {blocked:,} itens bloqueados antes de qualquer checkpoint."
            )
    if short_label_positive_checkpoint:
        run = short_label_positive_checkpoint["run"]
        allowed = int(run["checkpoint_allowed_count"] or 0)
        blocked = int(run["checkpoint_blocked_count"] or 0)
        if run["checkpoint_status"] == "ready_for_guarded_lifecycle_policy":
            actions.append(
                f"Checkpoint {run['checkpoint_name']} pronto: {allowed:,} itens allowlistados para futura integracao lifecycle."
            )
        elif blocked:
            actions.append(
                f"Resolver bloqueios do checkpoint {run['checkpoint_name']}: {blocked:,} itens bloqueados."
            )
    if issue_gender_subpolicy_shadow:
        run = issue_gender_subpolicy_shadow["run"]
        ready = int(run["shadow_ready_count"] or 0)
        boundary = int(run["boundary_count"] or 0)
        blocked = int(run["blocked_count"] or 0)
        if ready:
            actions.append(
                f"Subpolitica shadow de genero pronta para observacao: {ready:,}/{int(run['candidate_count'] or 0):,} itens ready, incluindo {boundary:,} boundaries."
            )
        if blocked:
            actions.append(
                f"Auditar bloqueios da subpolitica shadow de genero: {blocked:,} itens bloqueados antes de qualquer checkpoint."
            )
    if issue_gender_boundary_checkpoint:
        run = issue_gender_boundary_checkpoint["run"]
        allowed = int(run["checkpoint_allowed_count"] or 0)
        blocked = int(run["checkpoint_blocked_count"] or 0)
        if run["checkpoint_status"] == "ready_for_boundary_observation":
            actions.append(
                f"Checkpoint de boundaries de genero pronto em observacao: {allowed:,} guards shadow allowlistados, sem autorizacao de producao."
            )
        elif blocked:
            actions.append(
                f"Resolver bloqueios do checkpoint de boundaries de genero: {blocked:,} itens bloqueados."
            )
    if issue_gender_boundary_lifecycle:
        run = issue_gender_boundary_lifecycle["run"]
        released = int(run["released_count"] or 0)
        blocked = int(run["blocked_count"] or 0)
        if released and not blocked:
            actions.append(
                f"Lifecycle shadow de boundaries de genero ativo: {released:,} guards monitorados, sem autorizacao de producao."
            )
        elif blocked:
            actions.append(
                f"Auditar bloqueios do lifecycle shadow de genero: {blocked:,} itens bloqueados."
            )
    if issue_trigger_gender_role_surface:
        run = issue_trigger_gender_role_surface["run"]
        ready = int(run["shadow_ready_count"] or 0)
        blocked = int(run["blocked_count"] or 0)
        if ready and not blocked:
            actions.append(
                f"Microagente {run['agent_key']} maduro para checkpoint observacional: {ready:,}/{int(run['candidate_count'] or 0):,} itens prontos, sem efeito de producao."
            )
        elif blocked:
            actions.append(
                f"Auditar bloqueios do microagente {run['agent_key']}: {blocked:,} itens bloqueados antes de qualquer checkpoint."
            )
    if issue_trigger_gender_role_checkpoint:
        run = issue_trigger_gender_role_checkpoint["run"]
        allowed = int(run["checkpoint_allowed_count"] or 0)
        blocked = int(run["checkpoint_blocked_count"] or 0)
        if run["checkpoint_status"] == "ready_for_trigger_gender_role_observation":
            actions.append(
                f"Checkpoint {run['checkpoint_name']} pronto: {allowed:,} itens trigger/genero monitoraveis, sem autorizacao de producao."
            )
        elif blocked:
            actions.append(
                f"Resolver bloqueios do checkpoint {run['checkpoint_name']}: {blocked:,} itens bloqueados."
            )
    if issue_trigger_gender_role_lifecycle:
        run = issue_trigger_gender_role_lifecycle["run"]
        released = int(run["released_count"] or 0)
        blocked = int(run["blocked_count"] or 0)
        if released and not blocked:
            actions.append(
                f"Lifecycle shadow de trigger/genero ativo: {released:,} observacoes monitoradas, sem autorizacao de producao."
            )
        elif blocked:
            actions.append(
                f"Auditar bloqueios do lifecycle shadow de trigger/genero: {blocked:,} itens bloqueados."
            )
    if issue_long_text_repair_route_checkpoint:
        run = issue_long_text_repair_route_checkpoint["run"]
        allowed = int(run["allowed_count"] or 0)
        blocked = int(run["blocked_count"] or 0)
        if allowed:
            actions.append(
                (
                    "Roteador long-text separou reparos observaveis: "
                    f"{allowed:,}/{int(run['candidate_count'] or 0):,} permitidos em checkpoint shadow."
                )
            )
        if blocked:
            actions.append(
                (
                    "Criar subpoliticas para bloqueios long-text estruturais: "
                    f"{blocked:,} reparos ainda exigem token/context policy."
                )
            )
    if issue_long_text_repair_route_lifecycle:
        run = issue_long_text_repair_route_lifecycle["run"]
        released = int(run["released_count"] or 0)
        blocked = int(run["blocked_count"] or 0)
        if released and not blocked:
            actions.append(
                f"Lifecycle shadow long-text ativo: {released:,} reparos monitorados, sem autorizacao de producao."
            )
        elif blocked:
            actions.append(
                f"Auditar bloqueios do lifecycle shadow long-text: {blocked:,} itens bloqueados."
            )
    if issue_long_text_structural_subpolicy_shadow:
        run = issue_long_text_structural_subpolicy_shadow["run"]
        ready = int(run["shadow_ready_count"] or 0)
        blocked = int(run["blocked_count"] or 0)
        if ready:
            actions.append(
                (
                    "Subpolitica estrutural long-text encontrou reparos atomicos observaveis: "
                    f"{ready:,}/{int(run['candidate_count'] or 0):,} shadow_ready."
                )
            )
        if blocked:
            actions.append(
                (
                    "Separar bloqueios long-text mistos em novos neuronios parciais: "
                    f"{blocked:,} casos ainda combinam token, semantica ou superficie."
                )
            )
    if issue_long_text_structural_subpolicy_checkpoint:
        run = issue_long_text_structural_subpolicy_checkpoint["run"]
        allowed = int(run["checkpoint_allowed_count"] or 0)
        blocked = int(run["checkpoint_blocked_count"] or 0)
        if run["checkpoint_status"] == "ready_for_shadow_lifecycle_policy":
            actions.append(
                (
                    "Checkpoint atomico long-text pronto para lifecycle shadow: "
                    f"{allowed:,}/{int(run['total_candidates'] or 0):,} allowlistados."
                )
            )
        elif blocked:
            actions.append(
                f"Resolver bloqueios do checkpoint atomico long-text: {blocked:,} itens bloqueados."
            )
    if issue_long_text_structural_subpolicy_lifecycle:
        run = issue_long_text_structural_subpolicy_lifecycle["run"]
        released = int(run["released_count"] or 0)
        blocked = int(run["blocked_count"] or 0)
        if released and not blocked:
            actions.append(
                f"Lifecycle shadow atomico long-text ativo: {released:,} reparos estruturais monitorados, sem autorizacao de producao."
            )
        elif blocked:
            actions.append(
                f"Auditar bloqueios do lifecycle atomico long-text: {blocked:,} itens bloqueados."
            )
    if issue_long_text_mixed_structural_split:
        run = issue_long_text_mixed_structural_split["run"]
        units = int(run["split_unit_count"] or 0)
        ready = int(run["split_ready_count"] or 0)
        token = int(run["needs_token_policy_count"] or 0)
        semantic = int(run["needs_semantic_review_count"] or 0)
        evidence = int(run["needs_more_evidence_count"] or 0)
        actions.append(
            (
                "Split misto long-text ativo: "
                f"{int(run['candidate_count'] or 0):,} bloqueios viraram {units:,} unidades parciais; "
                f"{ready:,} split_ready, {token:,} precisam token-policy, {semantic:,} semantica, "
                f"{evidence:,} mais evidencia."
            )
        )
    if issue_long_text_mixed_structural_split_checkpoint:
        run = issue_long_text_mixed_structural_split_checkpoint["run"]
        allowed = int(run["checkpoint_allowed_count"] or 0)
        blocked = int(run["checkpoint_blocked_count"] or 0)
        if run["checkpoint_status"] == "ready_for_partial_shadow_lifecycle_policy":
            actions.append(
                (
                    "Checkpoint parcial long-text pronto para lifecycle shadow: "
                    f"{allowed:,}/{int(run['candidate_count'] or 0):,} componentes allowlistados, "
                    "sem autorizacao de producao."
                )
            )
        elif blocked:
            actions.append(
                f"Resolver bloqueios do checkpoint parcial long-text: {blocked:,} componentes bloqueados."
            )
    if issue_long_text_mixed_structural_split_lifecycle:
        run = issue_long_text_mixed_structural_split_lifecycle["run"]
        released = int(run["released_count"] or 0)
        blocked = int(run["blocked_count"] or 0)
        if released and not blocked:
            actions.append(
                (
                    "Lifecycle parcial long-text ativo: "
                    f"{released:,} componentes observados em shadow, sem producao e sem fechamento de segmento."
                )
            )
        elif blocked:
            actions.append(
                f"Auditar bloqueios do lifecycle parcial long-text: {blocked:,} componentes bloqueados."
            )
    if issue_long_text_mixed_structural_token_policy_shadow:
        run = issue_long_text_mixed_structural_token_policy_shadow["run"]
        ready = int(run["shadow_ready_count"] or 0)
        superseded = int(run["superseded_count"] or 0)
        blocked = int(run["blocked_count"] or 0)
        actions.append(
            (
                "Token-policy long-text triado: "
                f"{ready:,} shadow_ready, {superseded:,} superseded por neuronios irmaos, "
                f"{blocked:,} bloqueios reais para subsplit/contexto."
            )
        )
    if issue_long_text_mixed_structural_token_policy_checkpoint:
        run = issue_long_text_mixed_structural_token_policy_checkpoint["run"]
        allowed = int(run["checkpoint_allowed_count"] or 0)
        blocked = int(run["checkpoint_blocked_count"] or 0)
        if run["checkpoint_status"] == "ready_for_partial_token_policy_lifecycle":
            actions.append(
                (
                    "Checkpoint token-policy long-text pronto para lifecycle shadow: "
                    f"{allowed:,}/{int(run['candidate_count'] or 0):,} componentes token allowlistados, "
                    "sem autorizacao de producao."
                )
            )
        elif blocked:
            actions.append(
                f"Resolver bloqueios do checkpoint token-policy long-text: {blocked:,} componentes bloqueados."
            )
    if issue_long_text_mixed_structural_token_policy_lifecycle:
        run = issue_long_text_mixed_structural_token_policy_lifecycle["run"]
        released = int(run["released_count"] or 0)
        blocked = int(run["blocked_count"] or 0)
        if released and not blocked:
            actions.append(
                (
                    "Lifecycle token-policy long-text ativo: "
                    f"{released:,} componentes token observados em shadow, sem producao e sem fechamento de segmento."
                )
            )
        elif blocked:
            actions.append(
                f"Auditar bloqueios do lifecycle token-policy long-text: {blocked:,} componentes bloqueados."
            )
    if issue_long_text_mixed_structural_token_policy_blocker_subsplit:
        run = issue_long_text_mixed_structural_token_policy_blocker_subsplit["run"]
        ready = int(run["subsplit_ready_count"] or 0)
        phrase = int(run["needs_phrase_mapping_count"] or 0)
        context = int(run["needs_context_review_count"] or 0)
        semantic = int(run["needs_semantic_review_count"] or 0)
        actions.append(
            (
                "Subsplit dos bloqueios token-policy long-text: "
                f"{ready:,} unidade pronta para checkpoint, {phrase:,} phrase-mapping, "
                f"{context:,} contexto de escopo e {semantic:,} semantica de concept reference."
            )
        )
    if issue_long_text_subject_pronoun_form_checkpoint:
        run = issue_long_text_subject_pronoun_form_checkpoint["run"]
        allowed = int(run["checkpoint_allowed_count"] or 0)
        blocked = int(run["checkpoint_blocked_count"] or 0)
        if run["checkpoint_status"] == "ready_for_subject_pronoun_lifecycle":
            actions.append(
                (
                    "Checkpoint subject-pronoun long-text pronto para lifecycle shadow: "
                    f"{allowed:,}/{int(run['candidate_count'] or 0):,} componentes allowlistados, "
                    "sem autorizacao de producao."
                )
            )
        elif blocked:
            actions.append(
                f"Resolver bloqueios do checkpoint subject-pronoun long-text: {blocked:,} componentes bloqueados."
            )
    if issue_long_text_subject_pronoun_form_lifecycle:
        run = issue_long_text_subject_pronoun_form_lifecycle["run"]
        released = int(run["released_count"] or 0)
        blocked = int(run["blocked_count"] or 0)
        if released and not blocked:
            actions.append(
                (
                    "Lifecycle subject-pronoun long-text ativo: "
                    f"{released:,} componente observado em shadow, sem producao e sem fechamento de segmento."
                )
            )
        elif blocked:
            actions.append(
                f"Auditar bloqueios do lifecycle subject-pronoun long-text: {blocked:,} componentes bloqueados."
            )
    if issue_long_text_partial_composition_impact:
        run = issue_long_text_partial_composition_impact["run"]
        potential = int(run["potential_close_after_recheck_count"] or 0)
        total = int(run["candidate_segment_count"] or 0)
        recheck = int(run["candidate_recheck_segment_count"] or 0)
        blockers = int(run["blocker_segment_count"] or 0)
        actions.append(
            (
                "Impacto de composicao long-text medido: "
                f"{potential:,}/{total:,} segmentos com fechamento potencial em shadow "
                f"({recheck:,} exigem recheck final); {blockers:,} ainda travados por rotas especialistas."
            )
        )
    if issue_long_text_composition_recheck_queue:
        run = issue_long_text_composition_recheck_queue["run"]
        selected = int(run["selected_count"] or 0)
        open_count = int(run["open_count"] or 0)
        actions.append(
            (
                "Fila de recheck de composicao long-text pronta: "
                f"{selected:,} segmentos selecionados no review_queue_run {run['review_queue_run_id']}, "
                f"{open_count:,} ainda pendentes de validacao humana/assistida."
            )
        )
    if issue_review_decisions:
        run = issue_review_decisions["run"]
        accepted = int(run["accepted_count"] or 0)
        if accepted:
            if issue_review_dataset_bridge:
                actions.append(
                    f"Decisoes ingeridas da fila {run['queue_run_id']} ja foram consolidadas pelo dataset bridge; usar o diagnostico para reforcar especialistas/roteamento."
                )
            else:
                actions.append(
                    f"Usar {accepted:,} decisoes ingeridas da fila {run['queue_run_id']} para recalibrar o microagente {run['agent_key'] or 'unknown'}."
                )
        else:
            actions.append(
                f"Preencher o template da fila {run['queue_run_id']} antes de tentar novo aprendizado desse microagente."
            )
    if issue_review_queue:
        run = issue_review_queue["run"]
        open_count = int(run["open_count"] or 0)
        reviewed_count = int(run["reviewed_count"] or 0)
        selected_count = int(run["selected_count"] or 0)
        family_coverage = {}
        if issue_partial_coverage:
            family_coverage = issue_partial_coverage.get("family_counts", {}).get(run.get("issue_family"), {})
        covered_count = int(family_coverage.get("covered") or 0) if family_coverage else 0
        if open_count:
            if run.get("queue_strategy") == "partial_coverage_composition":
                if selected_count and covered_count >= selected_count:
                    actions.append(
                        (
                            f"Fila de composicao do microagente {run['agent_key']} tem checkpoint/politica ativo; "
                            f"a familia {run.get('issue_family')} ja acumula {covered_count:,} itens cobertos. "
                            "Usar a auditoria especifica da fila para separar itens absorvidos dos bloqueados."
                        )
                    )
                elif covered_count:
                    actions.append(
                        (
                            f"Fila de composicao do microagente {run['agent_key']} parcialmente atendida por checkpoint/politica; "
                            f"a familia {run.get('issue_family')} ja acumula {covered_count:,} itens cobertos. "
                            "Revisar os itens ainda bloqueados na auditoria da fila."
                        )
                    )
                else:
                    actions.append(
                        (
                            f"Revisar fila de composicao do microagente {run['agent_key']}: "
                            f"{open_count:,}/{selected_count:,} itens abertos em segmentos parcialmente cobertos."
                        )
                    )
            else:
                actions.append(
                    f"Revisar fila ativa do microagente {run['agent_key']}: {open_count:,}/{selected_count:,} itens abertos."
                )
        elif selected_count:
            if run.get("queue_strategy") == "partial_coverage_composition":
                actions.append(
                    (
                        f"Fila de composicao do microagente {run['agent_key']} revisada: "
                        f"{reviewed_count:,}/{selected_count:,} itens fechados."
                    )
                )
            else:
                actions.append(
                    f"Fila do microagente {run['agent_key']} revisada: {reviewed_count:,}/{selected_count:,} itens fechados."
                )
    if issue_ledger:
        actions.append(
            "Usar o ledger como base das proximas filas: primeiro micro_short_label_style, depois micro_gender_token."
        )
    if issue_partial_coverage:
        run = issue_partial_coverage["run"]
        partial = int(run["partially_covered_segments"] or 0)
        full = int(run["fully_covered_segments"] or 0)
        uncovered = int(run["uncovered_segments"] or 0)
        weighted = float(run["weighted_coverage_ratio"] or 0.0)
        if partial:
            actions.append(
                (
                    "Usar cobertura parcial como fila de composicao: "
                    f"{partial:,} segmentos ja tem algum neuronio cobrindo parte dos problemas."
                )
            )
        if full:
            actions.append(
                f"Auditar segmentos com cobertura total de issues: {full:,} candidatos para fechamento governado futuro."
            )
        if uncovered:
            top_family = issue_partial_coverage.get("top_open_families", [])
            suffix = ""
            if top_family:
                family, values = top_family[0]
                suffix = f" Principal familia aberta: {family} ({int(values.get('open') or 0):,})."
            actions.append(
                (
                    f"Priorizar novos microagentes/padroes para {uncovered:,} segmentos sem cobertura exata; "
                    f"cobertura ponderada atual {weighted:.2%}."
                    f"{suffix}"
                )
            )
    if text_audit:
        clean = text_audit["clean_candidates"]
        if clean:
            names = ", ".join(row["agent_key"] for row in clean[:3])
            actions.append(
                f"Promover apenas candidatos limpos para mais evidencia/shadow: {names}."
            )
        evidence_queues = [
            row
            for row in text_audit["rows"]
            if int(row["queued_count"] or 0) > 0 and int(row["reviewed_count"] or 0) == 0
        ]
        for row in evidence_queues[:3]:
            actions.append(
                (
                    f"Revisar fila pronta: {row['agent_key']}/{row['text_subfamily']} "
                    f"({int(row['queued_count'] or 0)} itens)."
                )
            )
    if text_shadow_policies:
        for row in text_shadow_policies["rows"]:
            total = int(row["total_candidates"] or 0)
            ready = int(row["shadow_ready_count"] or 0)
            blocked = int(row["blocked_count"] or 0)
            if total > 0 and ready == total and blocked == 0:
                actions.append(
                    f"Monitorar shadow policy limpa: {row['policy_name']} ({ready}/{total} shadow_ready)."
                )
    if text_boundary_policies:
        totals = text_boundary_policies["totals"]
        queued_same_token = 0
        queued_token_change = 0
        if text_boundary_repair_queues:
            queue_totals = text_boundary_repair_queues["totals"]
            queued_same_token = int(queue_totals.get("same_token", 0))
            queued_token_change = int(queue_totals.get("token_change", 0))
        if int(totals.get("unclassified", 0)):
            actions.append(
                f"Classificar boundaries residuais: {int(totals.get('unclassified', 0))} itens ainda sem taxonomia."
            )
        missing_token_change = max(int(totals.get("token_change", 0)) - queued_token_change, 0)
        if missing_token_change:
            actions.append(
                (
                    "Preparar fila de reparo com token-policy para boundaries negativos: "
                    f"{missing_token_change} candidatos exigem revisao estrutural."
                )
            )
        missing_same_token = max(int(totals.get("same_token", 0)) - queued_same_token, 0)
        if missing_same_token:
            actions.append(
                (
                    "Preparar reparo shadow same-token para boundaries negativos: "
                    f"{missing_same_token} candidatos preservam tokens estruturais."
                )
            )
    if text_boundary_repair_queues:
        totals = text_boundary_repair_queues["totals"]
        if int(totals.get("same_token", 0)):
            ready_count = 0
            blocked_count = 0
            if text_boundary_repair_shadow_policies:
                shadow_totals = text_boundary_repair_shadow_policies["totals"]
                ready_count = int(shadow_totals.get("shadow_ready", 0))
                blocked_count = int(shadow_totals.get("blocked", 0))
            missing_shadow = max(int(totals.get("same_token", 0)) - ready_count - blocked_count, 0)
            if missing_shadow:
                actions.append(
                    f"Revisar/validar fila same-token boundary repair: {missing_shadow} candidatos."
                )
        if int(totals.get("token_change", 0)) and not text_boundary_token_policy_bridges:
            actions.append(
                f"Encaminhar fila boundary repair com mudanca estrutural para token-policy: {int(totals.get('token_change', 0))} candidatos."
            )
    if text_boundary_repair_shadow_policies:
        totals = text_boundary_repair_shadow_policies["totals"]
        lifecycle_released = 0
        if text_boundary_repair_lifecycle_policies:
            lifecycle_totals = text_boundary_repair_lifecycle_policies["totals"]
            lifecycle_released = int(lifecycle_totals.get("released", 0))
        if int(totals.get("shadow_ready", 0)) and not lifecycle_released:
            actions.append(
                f"Promover somente em shadow controlado os reparos same-token aprovados: {int(totals.get('shadow_ready', 0))} candidatos."
            )
        if int(totals.get("blocked", 0)) and not (
            text_boundary_repair_checkpoints or text_boundary_repair_lifecycle_policies
        ):
            actions.append(
                f"Investigar reparos same-token bloqueados: {int(totals.get('blocked', 0))} candidatos."
            )
    if text_boundary_repair_checkpoints:
        totals = text_boundary_repair_checkpoints["totals"]
        lifecycle_released = 0
        if text_boundary_repair_lifecycle_policies:
            lifecycle_totals = text_boundary_repair_lifecycle_policies["totals"]
            lifecycle_released = int(lifecycle_totals.get("released", 0))
        if int(totals.get("allowed", 0)) and not lifecycle_released:
            actions.append(
                f"Criar lifecycle shadow para checkpoint de reparos same-token: {int(totals.get('allowed', 0))} candidatos."
            )
        if int(totals.get("checkpoint_blocked", 0)) and not text_boundary_repair_lifecycle_policies:
            actions.append(
                f"Auditar bloqueios no checkpoint same-token repair: {int(totals.get('checkpoint_blocked', 0))} candidatos."
            )
    if text_boundary_repair_lifecycle_policies:
        totals = text_boundary_repair_lifecycle_policies["totals"]
        if int(totals.get("released", 0)):
            actions.append(
                f"Monitorar lifecycle shadow de reparos same-token: {int(totals.get('released', 0))} liberados em shadow."
            )
        if int(totals.get("blocked", 0)):
            actions.append(
                f"Investigar reparos same-token sem delta ou bloqueados: {int(totals.get('blocked', 0))} candidatos."
            )
    if text_boundary_token_policy_bridges:
        totals = text_boundary_token_policy_bridges["totals"]
        if int(totals.get("review_required", 0)):
            raw_buckets = Counter(text_boundary_token_policy_bridges["by_bucket"])
            governed_buckets = Counter()
            if text_boundary_token_subpolicy_checkpoints:
                governed_buckets.update(text_boundary_token_subpolicy_checkpoints.get("by_bucket_allowed", {}))
            remaining_buckets = {
                name: max(0, int(count) - int(governed_buckets.get(name, 0)))
                for name, count in raw_buckets.items()
            }
            remaining_buckets = {name: count for name, count in remaining_buckets.items() if count}
            if remaining_buckets:
                top_buckets = sorted(
                    remaining_buckets.items(),
                    key=lambda item: item[1],
                    reverse=True,
                )[:3]
                bucket_text = ", ".join(f"{name}={count}" for name, count in top_buckets)
                actions.append(
                    (
                        "Revisar ponte token-policy restante dos boundary repairs estruturais: "
                        f"{sum(remaining_buckets.values())} nao governados ({bucket_text})."
                    )
                )
            else:
                actions.append(
                    (
                        "Ponte token-policy estrutural coberta por subpoliticas/checkpoints: "
                        f"{int(totals.get('review_required', 0))} candidatos brutos governados."
                    )
                )
        if int(totals.get("blocked", 0)):
            actions.append(
                f"Auditar bloqueios da ponte token-policy: {int(totals.get('blocked', 0))} candidatos."
            )
    if text_boundary_token_subpolicy_shadows:
        totals = text_boundary_token_subpolicy_shadows["totals"]
        checkpoint_allowed = 0
        if text_boundary_token_subpolicy_checkpoints:
            checkpoint_totals = text_boundary_token_subpolicy_checkpoints["totals"]
            checkpoint_allowed = int(checkpoint_totals.get("allowed", 0))
        if int(totals.get("shadow_ready", 0)) and not checkpoint_allowed:
            top_names = ", ".join(
                str(row["subpolicy_name"]) for row in text_boundary_token_subpolicy_shadows["rows"][:3]
            )
            actions.append(
                (
                    "Preparar checkpoint shadow para subpoliticas estruturais: "
                    f"{int(totals.get('shadow_ready', 0))} candidatos prontos ({top_names})."
                )
            )
        unique_coverage = text_boundary_token_subpolicy_shadows.get("unique_bridge_coverage") or {}
        unique_unresolved = int(unique_coverage.get("unresolved_unique") or 0)
        blocked_attempts = int(totals.get("blocked", 0))
        if unique_unresolved:
            actions.append(
                (
                    "Revisar pendencias estruturais unicas de token-policy: "
                    f"{unique_unresolved} itens sem subpolitica pronta "
                    f"({blocked_attempts} tentativas bloqueadas no total)."
                )
            )
        elif blocked_attempts:
            actions.append(
                (
                    "Monitorar tentativas bloqueadas de subpoliticas estruturais: "
                    f"{blocked_attempts} tentativas bloqueadas, sem pendencia unica descoberta."
                )
            )
    if text_boundary_token_subpolicy_checkpoints:
        totals = text_boundary_token_subpolicy_checkpoints["totals"]
        lifecycle_released = 0
        if text_boundary_token_subpolicy_lifecycle_policies:
            lifecycle_totals = text_boundary_token_subpolicy_lifecycle_policies["totals"]
            lifecycle_released = int(lifecycle_totals.get("released", 0))
        if int(totals.get("allowed", 0)) and not lifecycle_released:
            actions.append(
                f"Checkpoint shadow estrutural pronto: {int(totals.get('allowed', 0))} candidatos governados."
            )
        if int(totals.get("checkpoint_blocked", 0)):
            actions.append(
                f"Auditar bloqueios de checkpoint estrutural: {int(totals.get('checkpoint_blocked', 0))} candidatos."
            )
    if text_boundary_token_subpolicy_lifecycle_policies:
        totals = text_boundary_token_subpolicy_lifecycle_policies["totals"]
        if int(totals.get("released", 0)):
            actions.append(
                (
                    "Lifecycle shadow estrutural ativo: "
                    f"{int(totals.get('released', 0))}/{int(totals.get('candidates', 0))} candidatos monitorados."
                )
            )
        if int(totals.get("blocked", 0)):
            actions.append(
                f"Auditar bloqueios de lifecycle estrutural: {int(totals.get('blocked', 0))} candidatos."
            )
    if text_boundary_token_subpolicy_production_audits:
        totals = text_boundary_token_subpolicy_production_audits["totals"]
        if int(totals.get("eligible", 0)):
            actions.append(
                (
                    "Auditoria de producao controlada pronta: "
                    f"{int(totals.get('eligible', 0))}/{int(totals.get('candidates', 0))} elegiveis, "
                    f"ganho estimado {int(totals.get('estimated_closed_gain', 0))} fechamentos."
                )
            )
        actionable_blocked = int(totals.get("actionable_blocked", 0))
        already_applied_non_eligible = int(totals.get("already_applied_non_eligible", 0))
        quote_escape_only_blocked = int(totals.get("quote_escape_only_blocked", 0))
        if actionable_blocked:
            if actionable_blocked == quote_escape_only_blocked:
                actions.append(
                    (
                        "Corrigir normalizacao de aspas no comparador da producao controlada estrutural: "
                        f"{quote_escape_only_blocked} bloqueios acionaveis sao apenas escape de aspas."
                    )
                )
            else:
                actions.append(
                    (
                        "Auditar bloqueios acionaveis da producao controlada estrutural: "
                        f"{actionable_blocked} candidatos "
                        f"({already_applied_non_eligible} nao elegiveis ja aplicados)."
                    )
                )
        elif already_applied_non_eligible:
            actions.append(
                (
                    "Monitorar nao elegiveis ja aplicados na producao controlada estrutural: "
                    f"{already_applied_non_eligible} candidatos sem acao pendente."
                )
            )
        if actionable_blocked and already_applied_non_eligible:
            actions.append(
                (
                    "Monitorar nao elegiveis ja aplicados na producao controlada estrutural: "
                    f"{already_applied_non_eligible} candidatos sem acao pendente."
                )
            )
    if text_boundary_repair_production_audits:
        totals = text_boundary_repair_production_audits["totals"]
        if int(totals.get("eligible", 0)):
            actions.append(
                (
                    "Auditoria de producao controlada same-token pronta: "
                    f"{int(totals.get('eligible', 0))}/{int(totals.get('candidates', 0))} elegiveis, "
                    f"ganho estimado {int(totals.get('estimated_closed_gain', 0))} fechamentos."
                )
            )
        actionable_blocked = int(totals.get("actionable_blocked", 0))
        if actionable_blocked:
            actions.append(
                f"Corrigir bloqueios acionaveis da producao same-token: {actionable_blocked} candidatos."
            )
        elif int(totals.get("blocked", 0)):
            actions.append(
                (
                    "Monitorar bloqueios historicos same-token: "
                    f"{int(totals.get('blocked', 0))} itens sem ganho aplicavel atual."
                )
            )
    if text_policy_checkpoints:
        for row in text_policy_checkpoints["rows"]:
            total = int(row["total_candidates"] or 0)
            allowed = int(row["checkpoint_allowed_count"] or 0)
            blocked = int(row["checkpoint_blocked_count"] or 0)
            if total > 0 and allowed == total and blocked == 0:
                actions.append(
                    f"Checkpoint guardado pronto: {row['checkpoint_name']} ({allowed}/{total} candidatos)."
                )
            elif blocked:
                actions.append(
                    f"Revisar bloqueios do checkpoint {row['checkpoint_name']}: {blocked}/{total} bloqueados."
                )
    if reopen_lifecycle_policies:
        for row in reopen_lifecycle_policies["rows"]:
            actions.append(
                (
                    f"Policy lifecycle ativa: {row['policy_name']} "
                    f"({int(row['released_count'] or 0)}/{int(row['candidate_count'] or 0)} releases)."
                )
            )
    if text_audit:
        for row in text_audit["blockers"][:3]:
            actions.append(
                (
                    f"Manter {row['agent_key']} como boundary: "
                    f"{int(row['negative_count'] or 0)} negativos em {int(row['reviewed_count'] or 0)} revisados."
                )
            )
    if weak_auto_split_agents:
        for row in weak_auto_split_agents["positive_candidates"][:3]:
            notes = row.get("notes") or {}
            if notes.get("latest_shadow_policy_run_id"):
                actions.append(
                    (
                        f"Monitorar shadow scoped de {row['agent_key']}: "
                        f"ready={int(notes.get('latest_shadow_ready') or 0)}, "
                        f"blocked={int(notes.get('latest_shadow_blocked') or 0)}."
                    )
                )
            else:
                actions.append(
                    (
                        f"Preparar shadow scoped para {row['agent_key']}: "
                        f"{row['positive_count']} positivos e {row['negative_count']} negativos."
                    )
                )
        for row in weak_auto_split_agents["hard_boundaries"][:3]:
            actions.append(
                (
                    f"Converter boundary em blocker/repair: {row['agent_key']} "
                    f"({row['negative_count']}/{row['reviewed_count']} negativos)."
                )
            )
    if divine_realm_policy:
        run = divine_realm_policy["run"]
        total = int(run["total_rows"] or 0)
        ready = int(run["shadow_ready_count"] or 0)
        blocked = int(run["blocked_count"] or 0)
        if total > 0 and ready == total and blocked == 0:
            actions.append(
                f"Monitorar politica simbolica divine_realm limpa: {ready}/{total} shadow_ready."
            )
        elif blocked:
            actions.append(
                f"Revisar bloqueios divine_realm: {blocked}/{total} ainda fora da politica simbolica."
            )
    if boundary_correction_queues:
        totals = boundary_correction_queues["totals"]
        pending = int(totals.get("pending", 0))
        if pending:
            actions.append(
                (
                    "Revisar fila boundary correction: "
                    f"{pending}/{int(totals.get('items', 0))} segmentos aguardando corrected_text."
                )
            )
    if active_policy_gap:
        totals = active_policy_gap.get("totals") or {}
        gap_total = int(totals.get("total") or 0)
        if gap_total:
            buckets = {
                row["bucket"]: int(row["count"] or 0)
                for row in active_policy_gap.get("candidate_probability_buckets", [])
            }
            near_threshold = int(buckets.get(">=0.85", 0))
            top_paths = active_policy_gap.get("by_path", [])
            top_path = top_paths[0]["relative_path"] if top_paths else "unknown"
            actions.append(
                (
                    "Auditar fronteira active-safe/policy-autofix: "
                    f"{gap_total:,} casos, {near_threshold:,} quase no threshold atual, "
                    f"principal foco {top_path}."
                )
            )
        policy_blocks = active_policy_gap.get("policy_safe_candidate_autofix_blocks") or []
        policy_block_total = sum(int(row["count"] or 0) for row in policy_blocks)
        if policy_block_total:
            top_block = policy_blocks[0]
            actions.append(
                (
                    "Ajustar gate de ciclo de vida para respeitar policy new_safe revisada: "
                    f"{policy_block_total:,} casos continuam pendentes por candidate_autofix, "
                    f"principal foco {top_block['policy_group']}."
                )
            )
    if recommendations and recommendations["rows"]:
        ready_specialists = {
            row["specialist_key"]
            for row in (specialist_policy or {}).get("rows", [])
            if str(row["status"]).startswith("READY")
        }
        next_recommendations = [
            row
            for row in recommendations["rows"]
            if row["proposed_agent_key"] not in ready_specialists
            and row["status"] == "proposed"
        ]
        top = next_recommendations[0] if next_recommendations else recommendations["rows"][0]
        prefix = (
            "Proximo treino de alto impacto"
            if next_recommendations
            else "Maior foco ja pronto/monitorar"
        )
        actions.append(
            (
                f"{prefix}: {top['proposed_agent_key']} "
                f"({int(top['evidence_count'] or 0)} evidencias)."
            )
        )
    if not actions:
        actions.append("Gerar nova fila de evidencia antes de treinar ou promover politicas.")
    return actions


def write_report(
    *,
    txt_path: Path,
    json_path: Path,
    payload: dict[str, Any],
) -> None:
    registry = payload["registry"]
    routing = payload.get("routing")
    text_audit = payload.get("text_audit")
    weak_auto_split_agents = payload.get("weak_auto_split_agents")
    text_shadow_policies = payload.get("text_shadow_policies")
    text_boundary_policies = payload.get("text_boundary_policies")
    text_boundary_repair_queues = payload.get("text_boundary_repair_queues")
    text_boundary_repair_shadow_policies = payload.get("text_boundary_repair_shadow_policies")
    text_boundary_repair_checkpoints = payload.get("text_boundary_repair_checkpoints")
    text_boundary_repair_lifecycle_policies = payload.get("text_boundary_repair_lifecycle_policies")
    text_boundary_token_policy_bridges = payload.get("text_boundary_token_policy_bridges")
    text_boundary_token_subpolicy_shadows = payload.get("text_boundary_token_subpolicy_shadows")
    text_boundary_token_subpolicy_checkpoints = payload.get("text_boundary_token_subpolicy_checkpoints")
    text_boundary_token_subpolicy_lifecycle_policies = payload.get("text_boundary_token_subpolicy_lifecycle_policies")
    text_boundary_token_subpolicy_production_audits = payload.get("text_boundary_token_subpolicy_production_audits")
    text_boundary_repair_production_audits = payload.get("text_boundary_repair_production_audits")
    text_policy_checkpoints = payload.get("text_policy_checkpoints")
    reopen_lifecycle_policies = payload.get("reopen_lifecycle_policies")
    divine_realm_policy = payload.get("divine_realm_policy")
    boundary_correction_queues = payload.get("boundary_correction_queues")
    specialist_policy = payload.get("specialist_policy")
    recommendations = payload.get("recommendations")
    segment_state = payload.get("segment_state")
    issue_ledger = payload.get("issue_ledger")
    issue_review_queue = payload.get("issue_review_queue")
    issue_review_decisions = payload.get("issue_review_decisions")
    issue_dynamic_literal_repair = payload.get("issue_dynamic_literal_repair")
    issue_select_cstring_auxiliary_rewrite_checkpoint = payload.get("issue_select_cstring_auxiliary_rewrite_checkpoint")
    issue_select_cstring_antpath_relation_rewrite_checkpoint = payload.get(
        "issue_select_cstring_antpath_relation_rewrite_checkpoint"
    )
    issue_select_cstring_same_token_lifecycle = payload.get("issue_select_cstring_same_token_lifecycle")
    issue_select_cstring_residual_literal_cleanup_checkpoint = payload.get(
        "issue_select_cstring_residual_literal_cleanup_checkpoint"
    )
    issue_select_cstring_same_token_composition_overlay = payload.get(
        "issue_select_cstring_same_token_composition_overlay"
    )
    issue_select_cstring_dynamic_literal_payload_checkpoint = payload.get(
        "issue_select_cstring_dynamic_literal_payload_checkpoint"
    )
    issue_select_cstring_multistage_composition_overlay = payload.get(
        "issue_select_cstring_multistage_composition_overlay"
    )
    issue_select_cstring_local_player_pronoun_checkpoint = payload.get(
        "issue_select_cstring_local_player_pronoun_checkpoint"
    )
    issue_select_cstring_final_composition_overlay = payload.get(
        "issue_select_cstring_final_composition_overlay"
    )
    issue_select_cstring_final_composition_maturity_audit = payload.get(
        "issue_select_cstring_final_composition_maturity_audit"
    )
    issue_select_cstring_governed_bridge_proposal = payload.get(
        "issue_select_cstring_governed_bridge_proposal"
    )
    issue_select_cstring_label_rewrite_checkpoint = payload.get("issue_select_cstring_label_rewrite_checkpoint")
    issue_select_cstring_object_sentence_rewrite_checkpoint = payload.get(
        "issue_select_cstring_object_sentence_rewrite_checkpoint"
    )
    issue_nickname_pauper_king_title_semantic_checkpoint = payload.get(
        "issue_nickname_pauper_king_title_semantic_checkpoint"
    )
    issue_review_dataset_bridge = payload.get("issue_review_dataset_bridge")
    short_label_positive_release = payload.get("short_label_positive_release")
    short_label_positive_checkpoint = payload.get("short_label_positive_checkpoint")
    issue_long_text_repair_route_checkpoint = payload.get("issue_long_text_repair_route_checkpoint")
    issue_long_text_repair_route_lifecycle = payload.get("issue_long_text_repair_route_lifecycle")
    issue_long_text_structural_subpolicy_shadow = payload.get("issue_long_text_structural_subpolicy_shadow")
    issue_long_text_structural_subpolicy_checkpoint = payload.get("issue_long_text_structural_subpolicy_checkpoint")
    issue_long_text_structural_subpolicy_lifecycle = payload.get("issue_long_text_structural_subpolicy_lifecycle")
    issue_long_text_mixed_structural_split = payload.get("issue_long_text_mixed_structural_split")
    issue_long_text_mixed_structural_split_checkpoint = payload.get("issue_long_text_mixed_structural_split_checkpoint")
    issue_long_text_mixed_structural_split_lifecycle = payload.get("issue_long_text_mixed_structural_split_lifecycle")
    issue_long_text_mixed_structural_token_policy_shadow = payload.get("issue_long_text_mixed_structural_token_policy_shadow")
    issue_long_text_mixed_structural_token_policy_checkpoint = payload.get("issue_long_text_mixed_structural_token_policy_checkpoint")
    issue_long_text_mixed_structural_token_policy_lifecycle = payload.get("issue_long_text_mixed_structural_token_policy_lifecycle")
    issue_long_text_mixed_structural_token_policy_blocker_subsplit = payload.get("issue_long_text_mixed_structural_token_policy_blocker_subsplit")
    issue_long_text_subject_pronoun_form_checkpoint = payload.get("issue_long_text_subject_pronoun_form_checkpoint")
    issue_long_text_subject_pronoun_form_lifecycle = payload.get("issue_long_text_subject_pronoun_form_lifecycle")
    issue_long_text_partial_composition_impact = payload.get("issue_long_text_partial_composition_impact")
    issue_long_text_composition_recheck_queue = payload.get("issue_long_text_composition_recheck_queue")
    macro_model_status = payload.get("macro_model_status")
    active_policy_gap = payload.get("active_policy_gap")

    lines = [
        "Learning network diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {payload['generated_at']}",
        "",
        "Network registry:",
        f"- Registered agents: {registry['total']:,}",
        f"- By status: {json.dumps(registry['by_status'], ensure_ascii=False, sort_keys=True)}",
        f"- By state: {json.dumps(registry['by_operational_state'], ensure_ascii=False, sort_keys=True)}",
        f"- By dashboard group: {json.dumps(registry['by_dashboard_group'], ensure_ascii=False, sort_keys=True)}",
        "",
    ]
    if routing:
        run = routing["run"]
        lines.extend(
            [
                "Latest routing:",
                f"- Routing run id: {run['id']}",
                f"- Agents considered: {int(run['agents_considered_count'] or 0):,}",
                f"- Scope-sum routed count: {int(routing['scope_sum_count'] or 0):,}",
                f"- Materialized routing sample: {int(routing['materialized_count'] or 0):,}",
                f"- Recommendations: {int(run['recommendation_count'] or 0):,}",
                "- Top routed agents:",
                *[
                    f"  - {row['route_agent_key']} [{row['route_status']}]: {int(row['count'] or 0):,}"
                    for row in routing["top_agents"][:10]
                ],
                "",
            ]
        )
    if text_audit:
        totals = text_audit["totals"]
        lines.extend(
            [
                "Auto-confirmation text learning:",
                f"- Audit run id: {text_audit['run']['id']}",
                f"- Diagnostic rows: {int(totals.get('diagnostic', 0)):,}",
                f"- Reviewed rows: {int(totals.get('reviewed', 0)):,} ({text_audit['review_coverage']:.2%})",
                f"- Positive evidence: {int(totals.get('positive', 0)):,} ({text_audit['positive_rate']:.2%})",
                f"- Negative/boundary evidence: {int(totals.get('negative', 0)):,} ({text_audit['negative_rate']:.2%})",
                f"- Maturity statuses: {json.dumps(text_audit['status_counts'], ensure_ascii=False, sort_keys=True)}",
                "- Evidence queues awaiting review:",
                *[
                    (
                        f"  - {row['agent_key']}/{row['text_subfamily']}: "
                        f"diag={int(row['diagnostic_count'] or 0)}, queued={int(row['queued_count'] or 0)}, "
                        f"reviewed={int(row['reviewed_count'] or 0)}, status={row['maturity_status']}"
                    )
                    for row in text_audit["rows"]
                    if int(row["queued_count"] or 0) > 0 and int(row["reviewed_count"] or 0) == 0
                ],
                "- Clean candidates:",
                *[
                    (
                        f"  - {row['agent_key']}/{row['text_subfamily']}: "
                        f"{int(row['positive_count'] or 0)} pos, {int(row['negative_count'] or 0)} neg, "
                        f"status={row['maturity_status']}"
                    )
                    for row in text_audit["clean_candidates"]
                ],
                "- Boundaries:",
                *[
                    (
                        f"  - {row['agent_key']}/{row['text_subfamily']}: "
                        f"{int(row['negative_count'] or 0)} neg de {int(row['reviewed_count'] or 0)} revisados"
                    )
                    for row in text_audit["blockers"]
                ],
                "",
            ]
        )
    if weak_auto_split_agents:
        totals = weak_auto_split_agents["totals"]
        lines.extend(
            [
                "Weak-auto split subagents:",
                f"- Split agents: {int(totals.get('agents', 0)):,}",
                f"- Reviewed evidence encoded: {int(totals.get('reviewed', 0)):,}",
                f"- Positive evidence: {int(totals.get('positive', 0)):,}",
                f"- Negative/boundary evidence: {int(totals.get('negative', 0)):,}",
                f"- By maturity: {json.dumps(weak_auto_split_agents['by_maturity'], ensure_ascii=False, sort_keys=True)}",
                "- Subagents:",
                *[
                    (
                        f"  - {row['agent_key']} -> {row['parent_agent_key']}: "
                        f"role={row['decision_role']}, maturity={row['maturity_status']}, "
                        f"pos={row['positive_count']}, neg={row['negative_count']}, "
                        f"reviewed={row['reviewed_count']}"
                    )
                    for row in weak_auto_split_agents["rows"]
                ],
                "",
            ]
        )
    if text_shadow_policies:
        totals = text_shadow_policies["totals"]
        lines.extend(
            [
                "Auto-confirmation text shadow policies:",
                f"- Latest policies: {int(totals.get('policies', 0)):,}",
                f"- Candidates: {int(totals.get('candidates', 0)):,}",
                f"- Shadow ready: {int(totals.get('shadow_ready', 0)):,} ({text_shadow_policies['ready_rate']:.2%})",
                f"- Blocked: {int(totals.get('blocked', 0)):,}",
                "- Policies:",
                *[
                    (
                        f"  - {row['policy_name']} [{row['agent_key']}]: "
                        f"ready={int(row['shadow_ready_count'] or 0)}/{int(row['total_candidates'] or 0)}, "
                        f"blocked={int(row['blocked_count'] or 0)}, status={row['policy_status']}"
                    )
                    for row in text_shadow_policies["rows"]
                ],
                "",
            ]
        )
    if text_boundary_policies:
        totals = text_boundary_policies["totals"]
        lines.extend(
            [
                "Auto-confirmation text boundary policies:",
                f"- Snapshot mode: {text_boundary_policies.get('snapshot_mode', 'unknown')}",
                f"- Latest policies: {int(totals.get('policies', 0)):,}",
                f"- Candidates: {int(totals.get('candidates', 0)):,}",
                f"- Repair candidates: {int(totals.get('repair_candidates', 0)):,} ({text_boundary_policies['repair_rate']:.2%})",
                f"- Same-token repairs: {int(totals.get('same_token', 0)):,}",
                f"- Token-change repairs: {int(totals.get('token_change', 0)):,}",
                f"- Block-only: {int(totals.get('block_only', 0)):,}",
                f"- Unclassified: {int(totals.get('unclassified', 0)):,}",
                f"- By policy: {json.dumps(text_boundary_policies['by_policy'], ensure_ascii=False, sort_keys=True)}",
                f"- By status: {json.dumps(text_boundary_policies['by_status'], ensure_ascii=False, sort_keys=True)}",
                f"- By token status: {json.dumps(text_boundary_policies['by_token'], ensure_ascii=False, sort_keys=True)}",
                "- Top boundary rows:",
                *[
                    (
                        f"  - {row['boundary_policy']} [{row['boundary_agent_key']}]: "
                        f"{int(row['count'] or 0):,} rows, status={row['boundary_status']}, "
                        f"token={row['token_status']}"
                    )
                    for row in text_boundary_policies["items"][:12]
                ],
                "",
            ]
        )
    if text_boundary_repair_queues:
        totals = text_boundary_repair_queues["totals"]
        lines.extend(
            [
                "Auto-confirmation text boundary repair queues:",
                f"- Latest queues: {int(totals.get('queues', 0)):,}",
                f"- Candidates: {int(totals.get('candidates', 0)):,}",
                f"- Selected: {int(totals.get('selected', 0)):,} ({text_boundary_repair_queues['selection_rate']:.2%})",
                f"- Same-token repair queue items: {int(totals.get('same_token', 0)):,}",
                f"- Token-change repair queue items: {int(totals.get('token_change', 0)):,}",
                f"- By scope: {json.dumps(text_boundary_repair_queues['by_scope'], ensure_ascii=False, sort_keys=True)}",
                f"- By route: {json.dumps(text_boundary_repair_queues['by_route'], ensure_ascii=False, sort_keys=True)}",
                f"- By policy: {json.dumps(text_boundary_repair_queues['by_policy'], ensure_ascii=False, sort_keys=True)}",
                f"- By risk: {json.dumps(text_boundary_repair_queues['by_risk'], ensure_ascii=False, sort_keys=True)}",
                "- Top repair queues:",
                *[
                    (
                        f"  - run {row['id']} scope={row['queue_scope']}: "
                        f"selected={int(row['selected_count'] or 0)}/{int(row['candidate_count'] or 0)}, "
                        f"same_token={int(row['same_token_count'] or 0)}, "
                        f"token_change={int(row['token_change_count'] or 0)}"
                    )
                    for row in text_boundary_repair_queues["rows"]
                ],
                "",
            ]
        )
    if text_boundary_repair_shadow_policies:
        totals = text_boundary_repair_shadow_policies["totals"]
        lines.extend(
            [
                "Auto-confirmation text boundary repair shadow policies:",
                f"- Latest policies: {int(totals.get('policies', 0)):,}",
                f"- Candidates: {int(totals.get('candidates', 0)):,}",
                f"- Shadow ready: {int(totals.get('shadow_ready', 0)):,} ({text_boundary_repair_shadow_policies['ready_rate']:.2%})",
                f"- Blocked: {int(totals.get('blocked', 0)):,}",
                f"- No text delta: {int(totals.get('no_text_delta', 0)):,}",
                f"- Validation issue rows: {int(totals.get('validation_issue', 0)):,}",
                f"- Same-token rechecked: {int(totals.get('same_token', 0)):,}",
                f"- By status: {json.dumps(text_boundary_repair_shadow_policies['by_status'], ensure_ascii=False, sort_keys=True)}",
                f"- By policy: {json.dumps(text_boundary_repair_shadow_policies['by_policy'], ensure_ascii=False, sort_keys=True)}",
                f"- By text delta: {json.dumps(text_boundary_repair_shadow_policies['by_delta'], ensure_ascii=False, sort_keys=True)}",
                "- Top shadow rows:",
                *[
                    (
                        f"  - {row['boundary_policy']} [{row['boundary_agent_key']}]: "
                        f"{int(row['count'] or 0):,} rows, status={row['shadow_status']}, "
                        f"delta={row['text_delta_kind']}, block={row['block_reason'] or 'none'}"
                    )
                    for row in text_boundary_repair_shadow_policies["items"][:12]
                ],
                "",
            ]
        )
    if text_boundary_repair_checkpoints:
        totals = text_boundary_repair_checkpoints["totals"]
        lines.extend(
            [
                "Auto-confirmation text boundary repair checkpoints:",
                f"- Latest checkpoints: {int(totals.get('checkpoints', 0)):,}",
                f"- Candidates: {int(totals.get('candidates', 0)):,}",
                f"- Ready rows: {int(totals.get('ready', 0)):,}",
                f"- Pre-check blocked rows: {int(totals.get('blocked', 0)):,}",
                f"- Checkpoint allowed: {int(totals.get('allowed', 0)):,} ({text_boundary_repair_checkpoints['allowed_rate']:.2%})",
                f"- Checkpoint blocked: {int(totals.get('checkpoint_blocked', 0)):,}",
                "- Checkpoints:",
                *[
                    (
                        f"  - {row['checkpoint_name']}: "
                        f"allowed={int(row['checkpoint_allowed_count'] or 0)}/{int(row['total_candidates'] or 0)}, "
                        f"ready={int(row['ready_count'] or 0)}, "
                        f"blocked={int(row['blocked_count'] or 0)}, "
                        f"checkpoint_blocked={int(row['checkpoint_blocked_count'] or 0)}, "
                        f"status={row['checkpoint_status']}, promotion={row['promotion_status']}"
                    )
                    for row in text_boundary_repair_checkpoints["rows"]
                ],
                "",
            ]
        )
    if text_boundary_repair_lifecycle_policies:
        totals = text_boundary_repair_lifecycle_policies["totals"]
        lines.extend(
            [
                "Auto-confirmation text boundary repair lifecycle policies:",
                f"- Latest policies: {int(totals.get('policies', 0)):,}",
                f"- Candidates: {int(totals.get('candidates', 0)):,}",
                f"- Released shadow: {int(totals.get('released', 0)):,} ({text_boundary_repair_lifecycle_policies['release_rate']:.2%})",
                f"- Blocked: {int(totals.get('blocked', 0)):,}",
                f"- By status: {json.dumps(text_boundary_repair_lifecycle_policies['by_status'], ensure_ascii=False, sort_keys=True)}",
                f"- By policy: {json.dumps(text_boundary_repair_lifecycle_policies['by_policy'], ensure_ascii=False, sort_keys=True)}",
                "- Policies:",
                *[
                    (
                        f"  - {row['policy_name']}: "
                        f"released={int(row['released_count'] or 0)}/{int(row['candidate_count'] or 0)}, "
                        f"blocked={int(row['blocked_count'] or 0)}, "
                        f"status={row['policy_status']}, action={row['policy_action']}"
                    )
                    for row in text_boundary_repair_lifecycle_policies["rows"]
                ],
                "",
            ]
        )
    if text_boundary_token_policy_bridges:
        totals = text_boundary_token_policy_bridges["totals"]
        lines.extend(
            [
                "Auto-confirmation text boundary token-policy bridges:",
                f"- Latest bridges: {int(totals.get('bridges', 0)):,}",
                f"- Candidates: {int(totals.get('candidates', 0)):,}",
                f"- Token-change rows: {int(totals.get('token_change', 0)):,}",
                f"- Same-token rows: {int(totals.get('same_token', 0)):,}",
                f"- Review required: {int(totals.get('review_required', 0)):,} ({text_boundary_token_policy_bridges['review_rate']:.2%})",
                f"- Blocked: {int(totals.get('blocked', 0)):,}",
                f"- By status: {json.dumps(text_boundary_token_policy_bridges['by_status'], ensure_ascii=False, sort_keys=True)}",
                f"- By risk: {json.dumps(text_boundary_token_policy_bridges['by_risk'], ensure_ascii=False, sort_keys=True)}",
                f"- By bucket: {json.dumps(text_boundary_token_policy_bridges['by_bucket'], ensure_ascii=False, sort_keys=True)}",
                f"- By boundary policy: {json.dumps(text_boundary_token_policy_bridges['by_policy'], ensure_ascii=False, sort_keys=True)}",
                f"- By diff kind: {json.dumps(text_boundary_token_policy_bridges['by_diff'], ensure_ascii=False, sort_keys=True)}",
                "- Bridges:",
                *[
                    (
                        f"  - {row['bridge_name']}: "
                        f"review={int(row['review_required_count'] or 0)}/{int(row['total_candidates'] or 0)}, "
                        f"blocked={int(row['blocked_count'] or 0)}, "
                        f"status={row['bridge_status']}, queue={row['repair_queue_run_id']}"
                    )
                    for row in text_boundary_token_policy_bridges["rows"]
                ],
                "",
            ]
        )
    if text_boundary_token_subpolicy_shadows:
        totals = text_boundary_token_subpolicy_shadows["totals"]
        unique_coverage = text_boundary_token_subpolicy_shadows.get("unique_bridge_coverage") or {}
        unique_unresolved_items = text_boundary_token_subpolicy_shadows.get("unique_unresolved_items") or []
        lines.extend(
            [
                "Auto-confirmation text boundary token subpolicy shadows:",
                f"- Latest subpolicies: {int(totals.get('subpolicies', 0)):,}",
                f"- Candidate attempts: {int(totals.get('candidates', 0)):,}",
                f"- Shadow ready: {int(totals.get('shadow_ready', 0)):,} ({text_boundary_token_subpolicy_shadows['ready_rate']:.2%})",
                f"- Blocked attempts: {int(totals.get('blocked', 0)):,}",
                (
                    "- Unique bridge items: "
                    f"{int(unique_coverage.get('unique_bridge_items') or 0):,}; "
                    f"ready={int(unique_coverage.get('ready_unique') or 0):,} "
                    f"({text_boundary_token_subpolicy_shadows['unique_ready_rate']:.2%}); "
                    f"unresolved={int(unique_coverage.get('unresolved_unique') or 0):,}"
                ),
                (
                    "- Covered items with rejected attempts: "
                    f"{int(unique_coverage.get('covered_items_with_rejected_attempts') or 0):,}"
                ),
                f"- Validation issue rows: {int(totals.get('validation_issue', 0)):,}",
                f"- By status: {json.dumps(text_boundary_token_subpolicy_shadows['by_status'], ensure_ascii=False, sort_keys=True)}",
                f"- By action: {json.dumps(text_boundary_token_subpolicy_shadows['by_action'], ensure_ascii=False, sort_keys=True)}",
                f"- By bucket: {json.dumps(text_boundary_token_subpolicy_shadows['by_bucket'], ensure_ascii=False, sort_keys=True)}",
                f"- By boundary policy: {json.dumps(text_boundary_token_subpolicy_shadows['by_policy'], ensure_ascii=False, sort_keys=True)}",
                "- Unique unresolved items:",
                *[
                    (
                        f"  - bridge {row['bridge_item_id']} segment {row['segment_id']} "
                        f"{row['relative_path']}::{row['source_key']} "
                        f"buckets={row['policy_buckets']} policies={row['boundary_policies']}"
                    )
                    for row in unique_unresolved_items
                ],
                "- Subpolicies:",
                *[
                    (
                        f"  - {row['subpolicy_name']}: "
                        f"ready={int(row['shadow_ready_count'] or 0)}/{int(row['total_candidates'] or 0)}, "
                        f"blocked={int(row['blocked_count'] or 0)}, status={row['subpolicy_status']}"
                    )
                    for row in text_boundary_token_subpolicy_shadows["rows"]
                ],
                "",
            ]
        )
    if text_boundary_token_subpolicy_checkpoints:
        totals = text_boundary_token_subpolicy_checkpoints["totals"]
        lines.extend(
            [
                "Auto-confirmation text boundary token subpolicy checkpoints:",
                f"- Latest checkpoints: {int(totals.get('checkpoints', 0)):,}",
                f"- Candidates: {int(totals.get('candidates', 0)):,}",
                f"- Ready rows: {int(totals.get('ready', 0)):,}",
                f"- Pre-check blocked rows: {int(totals.get('blocked', 0)):,}",
                f"- Checkpoint allowed: {int(totals.get('allowed', 0)):,} ({text_boundary_token_subpolicy_checkpoints['allowed_rate']:.2%})",
                f"- Checkpoint blocked: {int(totals.get('checkpoint_blocked', 0)):,}",
                f"- Validation issue rows: {int(totals.get('validation_issue', 0)):,}",
                f"- By status: {json.dumps(text_boundary_token_subpolicy_checkpoints['by_status'], ensure_ascii=False, sort_keys=True)}",
                f"- By subpolicy: {json.dumps(text_boundary_token_subpolicy_checkpoints['by_subpolicy'], ensure_ascii=False, sort_keys=True)}",
                "- Checkpoints:",
                *[
                    (
                        f"  - {row['checkpoint_name']} [{row['subpolicy_name']}]: "
                        f"allowed={int(row['checkpoint_allowed_count'] or 0)}/{int(row['total_candidates'] or 0)}, "
                        f"blocked={int(row['checkpoint_blocked_count'] or 0)}, "
                        f"status={row['checkpoint_status']}, promotion={row['promotion_status']}"
                    )
                    for row in text_boundary_token_subpolicy_checkpoints["rows"]
                ],
                "",
            ]
        )
    if text_boundary_token_subpolicy_lifecycle_policies:
        totals = text_boundary_token_subpolicy_lifecycle_policies["totals"]
        lines.extend(
            [
                "Auto-confirmation text boundary token subpolicy lifecycle policies:",
                f"- Policies: {int(totals.get('policies', 0)):,}",
                f"- Checkpoints covered: {int(totals.get('checkpoints', 0)):,}",
                f"- Subpolicies covered: {int(totals.get('subpolicies', 0)):,}",
                f"- Candidates: {int(totals.get('candidates', 0)):,}",
                f"- Released shadow: {int(totals.get('released', 0)):,} ({text_boundary_token_subpolicy_lifecycle_policies['release_rate']:.2%})",
                f"- Blocked: {int(totals.get('blocked', 0)):,}",
                f"- By status: {json.dumps(text_boundary_token_subpolicy_lifecycle_policies['by_status'], ensure_ascii=False, sort_keys=True)}",
                f"- By subpolicy: {json.dumps(text_boundary_token_subpolicy_lifecycle_policies['by_subpolicy'], ensure_ascii=False, sort_keys=True)}",
                f"- By bucket: {json.dumps(text_boundary_token_subpolicy_lifecycle_policies['by_bucket'], ensure_ascii=False, sort_keys=True)}",
                "- Policies:",
                *[
                    (
                        f"  - {row['policy_name']}: "
                        f"released={int(row['released_count'] or 0)}/{int(row['candidate_count'] or 0)}, "
                        f"blocked={int(row['blocked_count'] or 0)}, status={row['policy_status']}"
                    )
                    for row in text_boundary_token_subpolicy_lifecycle_policies["rows"]
                ],
                "",
            ]
        )
    if text_boundary_token_subpolicy_production_audits:
        totals = text_boundary_token_subpolicy_production_audits["totals"]
        lines.extend(
            [
                "Auto-confirmation text boundary token subpolicy production audits:",
                f"- Audits: {int(totals.get('audits', 0)):,}",
                f"- Candidates: {int(totals.get('candidates', 0)):,}",
                f"- Eligible controlled production: {int(totals.get('eligible', 0)):,} ({text_boundary_token_subpolicy_production_audits['eligible_rate']:.2%})",
                f"- Estimated closed gain: {int(totals.get('estimated_closed_gain', 0)):,} ({text_boundary_token_subpolicy_production_audits['gain_rate']:.2%})",
                f"- Blocked: {int(totals.get('blocked', 0)):,}",
                f"- Actionable blocked: {int(totals.get('actionable_blocked', 0)):,}",
                f"- Quote-escape-only blocked: {int(totals.get('quote_escape_only_blocked', 0)):,}",
                f"- Already applied non-eligible: {int(totals.get('already_applied_non_eligible', 0)):,}",
                f"- Requires confirmation promotion: {int(totals.get('requires_confirmation_promotion', 0)):,}",
                f"- Requires output apply: {int(totals.get('requires_output_apply', 0)):,}",
                f"- Requires segment-state lifecycle integration: {int(totals.get('requires_segment_state_lifecycle_integration', 0)):,}",
                f"- By status: {json.dumps(text_boundary_token_subpolicy_production_audits['by_status'], ensure_ascii=False, sort_keys=True)}",
                f"- By block reason: {json.dumps(text_boundary_token_subpolicy_production_audits['by_block_reason'], ensure_ascii=False, sort_keys=True)}",
                f"- Eligible by subpolicy: {json.dumps(text_boundary_token_subpolicy_production_audits['by_subpolicy_eligible'], ensure_ascii=False, sort_keys=True)}",
                f"- Eligible by bucket: {json.dumps(text_boundary_token_subpolicy_production_audits['by_bucket_eligible'], ensure_ascii=False, sort_keys=True)}",
                f"- Gain by subpolicy: {json.dumps(text_boundary_token_subpolicy_production_audits['by_subpolicy_gain'], ensure_ascii=False, sort_keys=True)}",
                "- Actionable blocked items:",
                *[
                    (
                        f"  - segment {row['segment_id']} {row['relative_path']}::{row['source_key']} "
                        f"subpolicy={row['subpolicy_name']} reason={row['block_reason']} "
                        f"state={row['current_final_state']}/{row['current_apply_state']} "
                        f"quote_escape_only={int(row.get('quote_escape_only_mismatch') or 0)}"
                    )
                    for row in text_boundary_token_subpolicy_production_audits["actionable_items"]
                ],
                "- Audits:",
                *[
                    (
                        f"  - run {row['id']} lifecycle={row['lifecycle_run_id']} state={row['state_run_id']}: "
                        f"eligible={int(row['eligible_controlled_production_count'] or 0)}/{int(row['candidate_count'] or 0)}, "
                        f"gain={int(row['estimated_closed_gain_count'] or 0)}, "
                        f"confirm_promote={int(row['requires_confirmation_promotion_count'] or 0)}, "
                        f"output_apply={int(row['requires_output_apply_count'] or 0)}, "
                        f"state_integration={int(row['requires_segment_state_lifecycle_integration_count'] or 0)}, "
                        f"blocked={int(row['blocked_count'] or 0)}, status={row['audit_status']}"
                    )
                    for row in text_boundary_token_subpolicy_production_audits["rows"]
                ],
                "",
            ]
        )
    if text_boundary_repair_production_audits:
        totals = text_boundary_repair_production_audits["totals"]
        lines.extend(
            [
                "Auto-confirmation text boundary same-token repair production audits:",
                f"- Audits: {int(totals.get('audits', 0)):,}",
                f"- Candidates: {int(totals.get('candidates', 0)):,}",
                f"- Eligible controlled production: {int(totals.get('eligible', 0)):,} ({text_boundary_repair_production_audits['eligible_rate']:.2%})",
                f"- Estimated closed gain: {int(totals.get('estimated_closed_gain', 0)):,} ({text_boundary_repair_production_audits['gain_rate']:.2%})",
                f"- Real repairs: {int(totals.get('real_repair', 0)):,}",
                f"- No-op observations: {int(totals.get('noop_observation', 0)):,}",
                f"- Blocked: {int(totals.get('blocked', 0)):,}",
                f"- Actionable blocked: {int(totals.get('actionable_blocked', 0)):,}",
                f"- Requires confirmation promotion: {int(totals.get('requires_confirmation_promotion', 0)):,}",
                f"- Requires output apply: {int(totals.get('requires_output_apply', 0)):,}",
                f"- Requires segment-state lifecycle integration: {int(totals.get('requires_segment_state_lifecycle_integration', 0)):,}",
                f"- By status: {json.dumps(text_boundary_repair_production_audits['by_status'], ensure_ascii=False, sort_keys=True)}",
                f"- By block reason: {json.dumps(text_boundary_repair_production_audits['by_block_reason'], ensure_ascii=False, sort_keys=True)}",
                f"- Eligible by boundary policy: {json.dumps(text_boundary_repair_production_audits['by_policy_eligible'], ensure_ascii=False, sort_keys=True)}",
                f"- Gain by boundary policy: {json.dumps(text_boundary_repair_production_audits['by_policy_gain'], ensure_ascii=False, sort_keys=True)}",
                f"- By shadow action: {json.dumps(text_boundary_repair_production_audits['by_action'], ensure_ascii=False, sort_keys=True)}",
                f"- By text delta: {json.dumps(text_boundary_repair_production_audits['by_delta'], ensure_ascii=False, sort_keys=True)}",
                "- Block groups:",
                *[
                    (
                        f"  - {row['block_reason']} | group={row['current_state_group']} | "
                        f"state={row['current_final_state']} | apply={row['current_apply_state']}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in text_boundary_repair_production_audits.get("block_items", [])[:8]
                ],
                "- Audits:",
                *[
                    (
                        f"  - run {row['id']} lifecycle={row['lifecycle_run_id']} state={row['state_run_id']}: "
                        f"eligible={int(row['eligible_controlled_production_count'] or 0)}/{int(row['candidate_count'] or 0)}, "
                        f"gain={int(row['estimated_closed_gain_count'] or 0)}, "
                        f"real_repair={int(row['real_repair_count'] or 0)}, "
                        f"noop={int(row['noop_observation_count'] or 0)}, "
                        f"confirm_promote={int(row['requires_confirmation_promotion_count'] or 0)}, "
                        f"output_apply={int(row['requires_output_apply_count'] or 0)}, "
                        f"state_integration={int(row['requires_segment_state_lifecycle_integration_count'] or 0)}, "
                        f"blocked={int(row['blocked_count'] or 0)}, status={row['audit_status']}"
                    )
                    for row in text_boundary_repair_production_audits["rows"]
                ],
                "",
            ]
        )
    if text_policy_checkpoints:
        totals = text_policy_checkpoints["totals"]
        lines.extend(
            [
                "Auto-confirmation text policy checkpoints:",
                f"- Latest checkpoints: {int(totals.get('checkpoints', 0)):,}",
                f"- Candidates: {int(totals.get('candidates', 0)):,}",
                f"- Checkpoint allowed: {int(totals.get('allowed', 0)):,} ({text_policy_checkpoints['allowed_rate']:.2%})",
                f"- Checkpoint blocked: {int(totals.get('blocked', 0)):,}",
                f"- Production release allowed flags: {int(totals.get('production_release_allowed', 0)):,}",
                "- Checkpoints:",
                *[
                    (
                        f"  - {row['checkpoint_name']} [{row['agent_key']}]: "
                        f"allowed={int(row['checkpoint_allowed_count'] or 0)}/{int(row['total_candidates'] or 0)}, "
                        f"blocked={int(row['checkpoint_blocked_count'] or 0)}, "
                        f"status={row['checkpoint_status']}, promotion={row['promotion_status']}, "
                        f"production_release_allowed={int(row['production_release_allowed'] or 0)}"
                    )
                    for row in text_policy_checkpoints["rows"]
                ],
                "",
            ]
        )
    if reopen_lifecycle_policies:
        totals = reopen_lifecycle_policies["totals"]
        lines.extend(
            [
                "Auto-confirmation reopen lifecycle policies:",
                f"- Active policies: {int(totals.get('policies', 0)):,}",
                f"- Candidates: {int(totals.get('candidates', 0)):,}",
                f"- Released: {int(totals.get('released', 0)):,} ({reopen_lifecycle_policies['release_rate']:.2%})",
                f"- Blocked: {int(totals.get('blocked', 0)):,}",
                "- Policies:",
                *[
                    (
                        f"  - {row['policy_name']} [{row['label_family']}]: "
                        f"released={int(row['released_count'] or 0)}/{int(row['candidate_count'] or 0)}, "
                        f"blocked={int(row['blocked_count'] or 0)}, status={row['policy_status']}"
                    )
                    for row in reopen_lifecycle_policies["rows"]
                ],
                "",
            ]
        )
    if divine_realm_policy:
        run = divine_realm_policy["run"]
        lines.extend(
            [
                "Religion divine-realm symbolic policy:",
                f"- Policy run id: {run['id']}",
                f"- Rows: {int(run['total_rows'] or 0):,}",
                f"- Shadow ready: {int(run['shadow_ready_count'] or 0):,} ({divine_realm_policy['ready_rate']:.2%})",
                f"- Blocked: {int(run['blocked_count'] or 0):,}",
                "- Statuses:",
                *[
                    f"  - {row['policy_status']}: {int(row['count'] or 0):,}"
                    for row in divine_realm_policy["statuses"]
                ],
                "- Text families:",
                *[
                    f"  - {row['text_family']}: {int(row['count'] or 0):,}"
                    for row in divine_realm_policy["families"]
                ],
                "",
            ]
        )
    if boundary_correction_queues:
        run = boundary_correction_queues["run"]
        totals = boundary_correction_queues["totals"]
        lines.extend(
            [
                "Boundary correction review queue:",
                f"- Queue run id: {run['id']}",
                f"- Selected: {int(totals.get('items', 0)):,}",
                f"- Reviewed: {int(totals.get('reviewed', 0)):,} ({boundary_correction_queues['review_rate']:.2%})",
                f"- Pending review: {int(totals.get('pending', 0)):,}",
                f"- By boundary agent: {json.dumps(boundary_correction_queues['by_agent'], ensure_ascii=False, sort_keys=True)}",
                f"- By suggested label: {json.dumps(boundary_correction_queues['by_label'], ensure_ascii=False, sort_keys=True)}",
                f"- By risk: {json.dumps(boundary_correction_queues['by_risk'], ensure_ascii=False, sort_keys=True)}",
                "- Top queue groups:",
                *[
                    (
                        f"  - {row['suggested_agent_key']} / {row['suggested_review_label']} / {row['risk_level']}: "
                        f"{int(row['count'] or 0):,} rows, pending={int(row['pending_count'] or 0):,}, "
                        f"reviewed={int(row['reviewed_count'] or 0):,}"
                    )
                    for row in boundary_correction_queues["items"][:10]
                ],
                "",
            ]
        )
    if specialist_policy:
        lines.extend(
            [
                "Specialist policy safety:",
                f"- Snapshot created at: {specialist_policy['created_at']}",
                f"- Statuses: {json.dumps(specialist_policy['status_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Ready specialists: {specialist_policy['ready_count']:,}",
                f"- Safety-blocked specialists: {specialist_policy['safety_block_count']:,}",
                "- Safety blocks:",
                *[
                    (
                        f"  - {row['specialist_key']}: {row['status']} "
                        f"model={row['model_run_id']} score={row['score_run_id']} "
                        f"auto={int(row['final_auto_safe_count'] or 0):,}/{int(row['scored_count'] or 0):,}"
                    )
                    for row in specialist_policy["safety_blocks"]
                ],
                "",
            ]
        )
    if recommendations:
        totals = recommendations["totals"]
        lines.extend(
            [
                "Training recommendations:",
                f"- Recommendation run id: {recommendations['run_id']}",
                f"- Evidence: {int(totals.get('evidence', 0)):,}",
                f"- Positive: {int(totals.get('positive', 0)):,}",
                f"- Negative: {int(totals.get('negative', 0)):,}",
                f"- Corrected: {int(totals.get('corrected', 0)):,}",
                "- Top recommendations:",
                *[
                    (
                        f"  - {row['proposed_agent_key']} -> {row['parent_agent_key']}: "
                        f"status={row['status']}, "
                        f"evidence={int(row['evidence_count'] or 0)}, "
                        f"pos={int(row['positive_count'] or 0)}, neg={int(row['negative_count'] or 0)}, "
                        f"corrected={int(row['corrected_count'] or 0)}"
                    )
                    for row in recommendations["rows"][:10]
                ],
                "",
            ]
        )
    if segment_state:
        lines.extend(
            [
                "Latest segment-state snapshot:",
                f"- Segment-state run id: {segment_state['id']}",
                f"- Total segments: {int(segment_state['total_segments'] or 0):,}",
                f"- Closed: {int(segment_state['closed_count'] or 0):,}",
                f"- Pending: {int(segment_state['pending_count'] or 0):,}",
                f"- Needs apply: {int(segment_state['output_apply_pending_count'] or 0):,}",
                "",
            ]
        )
    if issue_ledger:
        run = issue_ledger["run"]
        lines.extend(
            [
                "Latest issue ledger:",
                f"- Ledger run id: {run['id']}",
                f"- Segment-state run id: {run['segment_state_run_id']}",
                f"- Pending segments: {int(run['pending_segments_count'] or 0):,}",
                f"- Ledger segments: {int(run['ledger_segment_count'] or 0):,}",
                f"- Ledger items: {int(run['ledger_item_count'] or 0):,}",
                f"- Actionable items: {int(run['actionable_item_count'] or 0):,}",
                f"- Blocked items: {int(run['blocked_item_count'] or 0):,}",
                "- Top families:",
                *[
                    (
                        f"  - {row['issue_family']}: {int(row['count'] or 0):,} items, "
                        f"{int(row['segment_count'] or 0):,} segments"
                    )
                    for row in issue_ledger["by_family"][:12]
                ],
                "- Top agents:",
                *[
                    (
                        f"  - {row['agent_key']}: {int(row['count'] or 0):,} items, "
                        f"{int(row['segment_count'] or 0):,} segments"
                    )
                    for row in issue_ledger["by_agent"][:12]
                ],
                f"- Status: {json.dumps(issue_ledger['by_status'], ensure_ascii=False, sort_keys=True)}",
                "",
            ]
        )
    issue_partial_coverage = payload.get("issue_partial_coverage")
    if issue_partial_coverage:
        run = issue_partial_coverage["run"]
        lines.extend(
            [
                "Issue partial coverage:",
                f"- Coverage run id: {run['id']}",
                f"- Ledger run id: {run['ledger_run_id']}",
                f"- Segment-state run id: {run['segment_state_run_id']}",
                f"- Segments with issues: {int(run['total_segments_with_issues'] or 0):,}",
                f"- Issue items: {int(run['total_issue_items'] or 0):,}",
                f"- Covered issue items: {int(run['covered_issue_items'] or 0):,} ({float(run['weighted_coverage_ratio'] or 0.0):.2%})",
                f"- Reviewed issue items: {int(run['reviewed_issue_items'] or 0):,}",
                f"- Open issue items: {int(run['open_issue_items'] or 0):,}",
                f"- Fully covered segments: {int(run['fully_covered_segments'] or 0):,}",
                f"- Partially covered segments: {int(run['partially_covered_segments'] or 0):,}",
                f"- Uncovered segments: {int(run['uncovered_segments'] or 0):,}",
                f"- Multi-issue segments: {int(run['multi_issue_segments'] or 0):,}",
                f"- Partial multi-issue segments: {int(run['partially_covered_multi_issue_segments'] or 0):,}",
                f"- Buckets: {json.dumps(issue_partial_coverage['bucket_counts'], ensure_ascii=False, sort_keys=True)}",
                "- Coverage sources:",
                *[
                    f"  - {source}: {int(count or 0):,}"
                    for source, count in sorted(
                        issue_partial_coverage["source_counts"].items(),
                        key=lambda item: (-int(item[1] or 0), item[0]),
                    )[:10]
                ],
                "- Top open families:",
                *[
                    (
                        f"  - {family}: open={int(values.get('open') or 0):,}, "
                        f"covered={int(values.get('covered') or 0):,}, total={int(values.get('total') or 0):,}, "
                        f"coverage={float(values.get('coverage_ratio') or 0.0):.2%}"
                    )
                    for family, values in issue_partial_coverage["top_open_families"][:10]
                ],
                "- Partial samples:",
                *[
                    (
                        f"  - {row['relative_path']}::{row['source_key']} "
                        f"covered={int(row['covered_issue_count'] or 0)}/{int(row['total_issue_count'] or 0)}, "
                        f"open={int(row['open_issue_count'] or 0)}"
                    )
                    for row in issue_partial_coverage["partial_samples"][:8]
                ],
                "",
            ]
        )
    if issue_review_queue:
        run = issue_review_queue["run"]
        lines.extend(
            [
                "Latest issue review queue:",
                f"- Queue run id: {run['id']}",
                f"- Ledger run id: {run['ledger_run_id']}",
                f"- Agent: {run['agent_key']}",
                f"- Strategy: {run['queue_strategy']}",
                f"- Selected: {int(run['selected_count'] or 0):,}",
                f"- Open: {int(run['open_count'] or 0):,}",
                f"- Reviewed: {int(run['reviewed_count'] or 0):,}",
                f"- Report: {run['report_path']}",
                "- Buckets:",
                *[
                    (
                        f"  - {row['queue_bucket']}: {int(row['count'] or 0):,}, "
                        f"avg_priority={float(row['avg_priority'] or 0.0):.2f}"
                    )
                    for row in issue_review_queue["by_bucket"][:14]
                ],
                "- Suggested decisions:",
                *[
                    (
                        f"  - {row['suggested_decision']} [{row['review_status']}]: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_review_queue["by_decision"][:12]
                ],
                "",
            ]
        )
    if issue_review_decisions:
        run = issue_review_decisions["run"]
        queue_progress = issue_review_decisions.get("queue_progress")
        lines.extend(
            [
                "Latest issue review decision ingest:",
                f"- Decision run id: {run['id']}",
                f"- Queue run id: {run['queue_run_id'] or 'mixed/unknown'}",
                f"- Agent: {run['agent_key'] or 'unknown'}",
                f"- Rows loaded: {int(run['total_rows'] or 0):,}",
                f"- Accepted: {int(run['accepted_count'] or 0):,}",
                f"- Skipped: {int(run['skipped_count'] or 0):,}",
                f"- Invalid: {int(run['invalid_count'] or 0):,}",
                f"- Safe: {int(run['safe_count'] or 0):,}",
                f"- False-positive reopen: {int(run['false_positive_count'] or 0):,}",
                f"- Repair: {int(run['repair_count'] or 0):,}",
                f"- Context: {int(run['context_count'] or 0):,}",
                f"- New microagent: {int(run['new_microagent_count'] or 0):,}",
                f"- Manual exception: {int(run['manual_exception_count'] or 0):,}",
                f"- Report: {run['report_path']}",
            ]
        )
        if queue_progress:
            lines.extend(
                [
                    (
                        "- Queue progress: "
                        f"selected={int(queue_progress['selected_count'] or 0):,}, "
                        f"reviewed={int(queue_progress['reviewed_count'] or 0):,}, "
                        f"open={int(queue_progress['open_count'] or 0):,}"
                    ),
                ]
            )
        lines.extend(
            [
                "- Evidence:",
                *[
                    (
                        f"  - {row['evidence_label']} / {row['normalized_decision']}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_review_decisions["by_evidence"][:12]
                ],
                "",
            ]
        )
    if issue_dynamic_literal_repair:
        run = issue_dynamic_literal_repair["run"]
        lines.extend(
            [
                "Dynamic literal repair diagnostic:",
                f"- Repair run id: {run['id']}",
                f"- Decision run id: {run['decision_run_id']}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                f"- Ready shadow: {int(run['ready_shadow_count'] or 0):,}",
                f"- Needs context: {int(run['needs_context_count'] or 0):,}",
                f"- Token policy review: {int(run['token_policy_review_count'] or 0):,}",
                f"- Same structural tokens: {int(run['same_structural_token_count'] or 0):,}",
                f"- Report: {run['report_path']}",
                "- Status:",
                *[
                    (
                        f"  - {row['repair_status']} / {row['token_status']}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_dynamic_literal_repair["by_status"][:8]
                ],
                "- Buckets:",
                *[
                    (
                        f"  - {row['queue_bucket']} / {row['repair_status']}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_dynamic_literal_repair["by_bucket"][:8]
                ],
                "- Context rows:",
                *[
                    (
                        f"  - {row['segment_id']} {row['relative_path']}::{row['source_key']} "
                        f"{row['repair_status']} / {row['token_status']}"
                    )
                    for row in issue_dynamic_literal_repair["context_rows"][:8]
                ],
                "",
            ]
        )
    if issue_select_cstring_auxiliary_rewrite_checkpoint:
        run = issue_select_cstring_auxiliary_rewrite_checkpoint["run"]
        lines.extend(
            [
                "Select_CString auxiliary rewrite checkpoint:",
                f"- Checkpoint run id: {run['id']}",
                f"- Source checkpoint run id: {run['source_checkpoint_run_id']}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                (
                    f"- Allowed shadow: {int(run['allowed_count'] or 0):,} "
                    f"({issue_select_cstring_auxiliary_rewrite_checkpoint['allowed_rate']:.2%})"
                ),
                f"- Blocked: {int(run['blocked_count'] or 0):,}",
                f"- Report: {run['report_path']}",
                "- Subpolicies:",
                *[
                    (
                        f"  - {row['subpolicy_name']} allowed={int(row['checkpoint_allowed'] or 0)} "
                        f"{row['token_status']} block={row['block_reason'] or 'none'}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_select_cstring_auxiliary_rewrite_checkpoint["by_subpolicy"][:10]
                ],
                "- Remaining blockers:",
                *(
                    [
                        (
                            f"  - {row['segment_id']} {row['relative_path']}::{row['source_key']} "
                            f"{row['block_reason']}"
                        )
                        for row in issue_select_cstring_auxiliary_rewrite_checkpoint["blocked_rows"][:8]
                    ]
                    or ["  - none"]
                ),
                "",
            ]
        )
    if issue_select_cstring_antpath_relation_rewrite_checkpoint:
        run = issue_select_cstring_antpath_relation_rewrite_checkpoint["run"]
        lines.extend(
            [
                "Select_CString antpath relation rewrite checkpoint:",
                f"- Checkpoint run id: {run['id']}",
                f"- Source auxiliary checkpoint run id: {run['source_auxiliary_checkpoint_run_id']}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                (
                    f"- Allowed shadow: {int(run['allowed_count'] or 0):,} "
                    f"({issue_select_cstring_antpath_relation_rewrite_checkpoint['allowed_rate']:.2%})"
                ),
                f"- Blocked: {int(run['blocked_count'] or 0):,}",
                f"- Report: {run['report_path']}",
                "- Subpolicies:",
                *[
                    (
                        f"  - {row['subpolicy_name']} allowed={int(row['checkpoint_allowed'] or 0)} "
                        f"{row['token_status']} block={row['block_reason'] or 'none'}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_select_cstring_antpath_relation_rewrite_checkpoint["by_subpolicy"][:10]
                ],
                "- Ready samples:",
                *(
                    [
                        f"  - {row['segment_id']} {row['relative_path']}::{row['source_key']} {row['token_status']}"
                        for row in issue_select_cstring_antpath_relation_rewrite_checkpoint["ready_samples"][:8]
                    ]
                    or ["  - none"]
                ),
                "- Remaining blockers:",
                *(
                    [
                        (
                            f"  - {row['segment_id']} {row['relative_path']}::{row['source_key']} "
                            f"{row['block_reason']}"
                        )
                        for row in issue_select_cstring_antpath_relation_rewrite_checkpoint["blocked_rows"][:8]
                    ]
                    or ["  - none"]
                ),
                "",
            ]
        )
    if issue_select_cstring_same_token_lifecycle:
        run = issue_select_cstring_same_token_lifecycle["run"]
        lines.extend(
            [
                "Select_CString same-token lifecycle policy:",
                f"- Lifecycle run id: {run['id']}",
                f"- Policy: {run['policy_name']} ({run['policy_status']})",
                f"- Policy action: {run['policy_action']}",
                f"- Production release allowed: {int(run['production_release_allowed'] or 0)}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                (
                    f"- Released shadow: {int(run['released_count'] or 0):,} "
                    f"({issue_select_cstring_same_token_lifecycle['release_rate']:.2%})"
                ),
                f"- Blocked: {int(run['blocked_count'] or 0):,}",
                f"- Source run ids: {run['source_run_ids_json']}",
                f"- Source counts: {json.dumps(issue_select_cstring_same_token_lifecycle['source_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Block counts: {json.dumps(issue_select_cstring_same_token_lifecycle['block_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Report: {run['report_path']}",
                "- Source families:",
                *[
                    (
                        f"  - {row['source_family']} allowed={int(row['policy_allowed'] or 0)} "
                        f"block={row['block_reason'] or 'none'}: {int(row['count'] or 0):,}"
                    )
                    for row in issue_select_cstring_same_token_lifecycle["by_source"][:12]
                ],
                "- Top subpolicies:",
                *[
                    (
                        f"  - {row['subpolicy_name']} allowed={int(row['policy_allowed'] or 0)} "
                        f"block={row['block_reason'] or 'none'}: {int(row['count'] or 0):,}"
                    )
                    for row in issue_select_cstring_same_token_lifecycle["by_subpolicy"][:12]
                ],
                "- Blocked samples:",
                *(
                    [
                        (
                            f"  - {row['segment_id']} {row['relative_path']}::{row['source_key']} "
                            f"{row['block_reason']} ({row['token_status']})"
                        )
                        for row in issue_select_cstring_same_token_lifecycle["blocked_samples"][:8]
                    ]
                    or ["  - none"]
                ),
                "",
            ]
        )
    if issue_select_cstring_residual_literal_cleanup_checkpoint:
        run = issue_select_cstring_residual_literal_cleanup_checkpoint["run"]
        lines.extend(
            [
                "Select_CString residual literal cleanup checkpoint:",
                f"- Checkpoint run id: {run['id']}",
                f"- Source lifecycle run id: {run['source_lifecycle_run_id']}",
                f"- Policy: {run['policy_name']} ({run['policy_status']})",
                "- Production release allowed: 0",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                (
                    f"- Allowed shadow: {int(run['allowed_count'] or 0):,} "
                    f"({issue_select_cstring_residual_literal_cleanup_checkpoint['allowed_rate']:.2%})"
                ),
                f"- Blocked: {int(run['blocked_count'] or 0):,}",
                f"- Potential lifecycle lift after composition: +{int(run['allowed_count'] or 0):,} shadow rows",
                f"- Report: {run['report_path']}",
                "- Token statuses:",
                *[
                    (
                        f"  - {row['token_status']} allowed={int(row['checkpoint_allowed'] or 0)}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_select_cstring_residual_literal_cleanup_checkpoint["by_token_status"][:8]
                ],
                "- Subpolicies:",
                *[
                    (
                        f"  - {row['subpolicy_name']} allowed={int(row['checkpoint_allowed'] or 0)} "
                        f"{row['token_status']} block={row['block_reason'] or 'none'}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_select_cstring_residual_literal_cleanup_checkpoint["by_subpolicy"][:10]
                ],
                "- Ready samples:",
                *(
                    [
                        (
                            f"  - {row['segment_id']} {row['relative_path']}::{row['source_key']} "
                            f"({row['token_status']})"
                        )
                        for row in issue_select_cstring_residual_literal_cleanup_checkpoint["ready_samples"][:8]
                    ]
                    or ["  - none"]
                ),
                "- Remaining blockers:",
                *(
                    [
                        (
                            f"  - {row['segment_id']} {row['relative_path']}::{row['source_key']} "
                            f"{row['block_reason']}"
                        )
                        for row in issue_select_cstring_residual_literal_cleanup_checkpoint["blocked_rows"][:8]
                    ]
                    or ["  - none"]
                ),
                "",
            ]
        )
    if issue_select_cstring_same_token_composition_overlay:
        run = issue_select_cstring_same_token_composition_overlay["run"]
        lines.extend(
            [
                "Select_CString same-token composition overlay:",
                f"- Overlay run id: {run['id']}",
                f"- Overlay: {run['overlay_name']} ({run['overlay_status']})",
                f"- Source lifecycle run id: {run['source_lifecycle_run_id']}",
                f"- Source cleanup checkpoint run id: {run['source_cleanup_checkpoint_run_id'] or ''}",
                f"- Production release allowed: {int(run['production_release_allowed'] or 0)}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                (
                    f"- Baseline released shadow: {int(run['baseline_released_count'] or 0):,} "
                    f"({issue_select_cstring_same_token_composition_overlay['baseline_rate']:.2%})"
                ),
                (
                    f"- Cleanup lift: {int(run['cleanup_lift_count'] or 0):,} "
                    f"({issue_select_cstring_same_token_composition_overlay['lift_rate']:.2%})"
                ),
                (
                    f"- Composed released shadow: {int(run['composed_released_count'] or 0):,} "
                    f"({issue_select_cstring_same_token_composition_overlay['composed_rate']:.2%})"
                ),
                f"- Blocked after composition: {int(run['blocked_count'] or 0):,}",
                f"- Source counts: {json.dumps(issue_select_cstring_same_token_composition_overlay['source_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Block counts: {json.dumps(issue_select_cstring_same_token_composition_overlay['block_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Report: {run['report_path']}",
                "- Composition sources:",
                *[
                    (
                        f"  - {row['composition_source']} allowed={int(row['composition_allowed'] or 0)} "
                        f"block={row['block_reason'] or 'none'}: {int(row['count'] or 0):,}"
                    )
                    for row in issue_select_cstring_same_token_composition_overlay["by_source"][:10]
                ],
                "- Recovered samples:",
                *(
                    [
                        (
                            f"  - {row['segment_id']} {row['relative_path']}::{row['source_key']} "
                            f"{row['source_family']} ({row['token_status']})"
                        )
                        for row in issue_select_cstring_same_token_composition_overlay["recovered_samples"][:8]
                    ]
                    or ["  - none"]
                ),
                "- Remaining blockers:",
                *(
                    [
                        (
                            f"  - {row['segment_id']} {row['relative_path']}::{row['source_key']} "
                            f"{row['block_reason']} ({row['token_status']})"
                        )
                        for row in issue_select_cstring_same_token_composition_overlay["blocked_rows"][:8]
                    ]
                    or ["  - none"]
                ),
                "",
            ]
        )
    if issue_select_cstring_dynamic_literal_payload_checkpoint:
        run = issue_select_cstring_dynamic_literal_payload_checkpoint["run"]
        lines.extend(
            [
                "Select_CString dynamic literal payload checkpoint:",
                f"- Checkpoint run id: {run['id']}",
                f"- Source composition overlay run id: {run['source_overlay_run_id']}",
                f"- Policy: {run['policy_name']} ({run['policy_status']})",
                f"- Production release allowed: {int(run['production_release_allowed'] or 0)}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                (
                    f"- Allowed shadow: {int(run['allowed_count'] or 0):,} "
                    f"({issue_select_cstring_dynamic_literal_payload_checkpoint['allowed_rate']:.2%})"
                ),
                f"- Blocked: {int(run['blocked_count'] or 0):,}",
                f"- Block counts: {json.dumps(issue_select_cstring_dynamic_literal_payload_checkpoint['block_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Report: {run['report_path']}",
                "- Subpolicies:",
                *[
                    (
                        f"  - {row['subpolicy_name']} allowed={int(row['checkpoint_allowed'] or 0)} "
                        f"{row['token_status']} block={row['block_reason'] or 'none'}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_select_cstring_dynamic_literal_payload_checkpoint["by_subpolicy"][:10]
                ],
                "- Ready samples:",
                *(
                    [
                        (
                            f"  - {row['segment_id']} {row['relative_path']}::{row['source_key']} "
                            f"({row['token_status']})"
                        )
                        for row in issue_select_cstring_dynamic_literal_payload_checkpoint["ready_samples"][:8]
                    ]
                    or ["  - none"]
                ),
                "- Remaining blockers:",
                *(
                    [
                        (
                            f"  - {row['segment_id']} {row['relative_path']}::{row['source_key']} "
                            f"{row['block_reason']} ({row['token_status']})"
                        )
                        for row in issue_select_cstring_dynamic_literal_payload_checkpoint["blocked_rows"][:8]
                    ]
                    or ["  - none"]
                ),
                "",
            ]
        )
    if issue_select_cstring_multistage_composition_overlay:
        run = issue_select_cstring_multistage_composition_overlay["run"]
        lines.extend(
            [
                "Select_CString multistage composition overlay:",
                f"- Overlay run id: {run['id']}",
                f"- Overlay: {run['overlay_name']} ({run['overlay_status']})",
                f"- Source lifecycle run id: {run['source_lifecycle_run_id']}",
                f"- Source cleanup checkpoint run id: {run['source_cleanup_checkpoint_run_id'] or ''}",
                f"- Source dynamic payload checkpoint run id: {run['source_dynamic_payload_checkpoint_run_id'] or ''}",
                f"- Production release allowed: {int(run['production_release_allowed'] or 0)}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                (
                    f"- Baseline released shadow: {int(run['baseline_released_count'] or 0):,} "
                    f"({issue_select_cstring_multistage_composition_overlay['baseline_rate']:.2%})"
                ),
                (
                    f"- Cleanup lift: {int(run['cleanup_lift_count'] or 0):,} "
                    f"({issue_select_cstring_multistage_composition_overlay['cleanup_lift_rate']:.2%})"
                ),
                (
                    f"- Dynamic payload lift: {int(run['dynamic_payload_lift_count'] or 0):,} "
                    f"({issue_select_cstring_multistage_composition_overlay['dynamic_lift_rate']:.2%})"
                ),
                (
                    f"- Composed released shadow: {int(run['composed_released_count'] or 0):,} "
                    f"({issue_select_cstring_multistage_composition_overlay['composed_rate']:.2%})"
                ),
                f"- Blocked after composition: {int(run['blocked_count'] or 0):,}",
                f"- Source counts: {json.dumps(issue_select_cstring_multistage_composition_overlay['source_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Block counts: {json.dumps(issue_select_cstring_multistage_composition_overlay['block_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Report: {run['report_path']}",
                "- Composition sources:",
                *[
                    (
                        f"  - {row['composition_source']} allowed={int(row['composition_allowed'] or 0)} "
                        f"block={row['block_reason'] or 'none'}: {int(row['count'] or 0):,}"
                    )
                    for row in issue_select_cstring_multistage_composition_overlay["by_source"][:10]
                ],
                "- Remaining blockers:",
                *(
                    [
                        (
                            f"  - {row['segment_id']} {row['relative_path']}::{row['source_key']} "
                            f"{row['block_reason']} ({row['token_status']})"
                        )
                        for row in issue_select_cstring_multistage_composition_overlay["blocked_rows"][:8]
                    ]
                    or ["  - none"]
                ),
                "",
            ]
        )
    if issue_select_cstring_local_player_pronoun_checkpoint:
        run = issue_select_cstring_local_player_pronoun_checkpoint["run"]
        lines.extend(
            [
                "Select_CString local-player pronoun literal checkpoint:",
                f"- Checkpoint run id: {run['id']}",
                f"- Source dynamic payload checkpoint run id: {run['source_dynamic_payload_checkpoint_run_id']}",
                f"- Policy: {run['policy_name']} ({run['policy_status']})",
                f"- Production release allowed: {int(run['production_release_allowed'] or 0)}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                (
                    f"- Allowed shadow: {int(run['allowed_count'] or 0):,} "
                    f"({issue_select_cstring_local_player_pronoun_checkpoint['allowed_rate']:.2%})"
                ),
                f"- Blocked: {int(run['blocked_count'] or 0):,}",
                f"- Block counts: {json.dumps(issue_select_cstring_local_player_pronoun_checkpoint['block_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Report: {run['report_path']}",
                "- Subpolicies:",
                *[
                    (
                        f"  - {row['subpolicy_name']} allowed={int(row['checkpoint_allowed'] or 0)} "
                        f"{row['token_status']} block={row['block_reason'] or 'none'}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_select_cstring_local_player_pronoun_checkpoint["by_subpolicy"][:10]
                ],
                "- Ready samples:",
                *(
                    [
                        (
                            f"  - {row['segment_id']} {row['relative_path']}::{row['source_key']} "
                            f"({row['token_status']})"
                        )
                        for row in issue_select_cstring_local_player_pronoun_checkpoint["ready_samples"][:8]
                    ]
                    or ["  - none"]
                ),
                "- Remaining blockers:",
                *(
                    [
                        (
                            f"  - {row['segment_id']} {row['relative_path']}::{row['source_key']} "
                            f"{row['block_reason']} ({row['token_status']})"
                        )
                        for row in issue_select_cstring_local_player_pronoun_checkpoint["blocked_rows"][:8]
                    ]
                    or ["  - none"]
                ),
                "",
            ]
        )
    if issue_select_cstring_final_composition_overlay:
        run = issue_select_cstring_final_composition_overlay["run"]
        lines.extend(
            [
                "Select_CString final composition overlay:",
                f"- Overlay run id: {run['id']}",
                f"- Overlay: {run['overlay_name']} ({run['overlay_status']})",
                f"- Source lifecycle run id: {run['source_lifecycle_run_id']}",
                f"- Source cleanup checkpoint run id: {run['source_cleanup_checkpoint_run_id'] or ''}",
                f"- Source dynamic payload checkpoint run id: {run['source_dynamic_payload_checkpoint_run_id'] or ''}",
                f"- Source local pronoun checkpoint run id: {run['source_local_pronoun_checkpoint_run_id'] or ''}",
                f"- Production release allowed: {int(run['production_release_allowed'] or 0)}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                (
                    f"- Baseline released shadow: {int(run['baseline_released_count'] or 0):,} "
                    f"({issue_select_cstring_final_composition_overlay['baseline_rate']:.2%})"
                ),
                (
                    f"- Cleanup lift: {int(run['cleanup_lift_count'] or 0):,} "
                    f"({issue_select_cstring_final_composition_overlay['cleanup_lift_rate']:.2%})"
                ),
                (
                    f"- Dynamic payload lift: {int(run['dynamic_payload_lift_count'] or 0):,} "
                    f"({issue_select_cstring_final_composition_overlay['dynamic_lift_rate']:.2%})"
                ),
                (
                    f"- Local pronoun lift: {int(run['local_pronoun_lift_count'] or 0):,} "
                    f"({issue_select_cstring_final_composition_overlay['local_pronoun_lift_rate']:.2%})"
                ),
                (
                    f"- Composed released shadow: {int(run['composed_released_count'] or 0):,} "
                    f"({issue_select_cstring_final_composition_overlay['composed_rate']:.2%})"
                ),
                f"- Blocked after composition: {int(run['blocked_count'] or 0):,}",
                f"- Source counts: {json.dumps(issue_select_cstring_final_composition_overlay['source_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Block counts: {json.dumps(issue_select_cstring_final_composition_overlay['block_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Report: {run['report_path']}",
                "- Composition sources:",
                *[
                    (
                        f"  - {row['composition_source']} allowed={int(row['composition_allowed'] or 0)} "
                        f"block={row['block_reason'] or 'none'}: {int(row['count'] or 0):,}"
                    )
                    for row in issue_select_cstring_final_composition_overlay["by_source"][:10]
                ],
                "- Remaining blockers:",
                *(
                    [
                        (
                            f"  - {row['segment_id']} {row['relative_path']}::{row['source_key']} "
                            f"{row['block_reason']} ({row['token_status']})"
                        )
                        for row in issue_select_cstring_final_composition_overlay["blocked_rows"][:8]
                    ]
                    or ["  - none"]
                ),
                "",
            ]
        )
    if issue_select_cstring_final_composition_maturity_audit:
        run = issue_select_cstring_final_composition_maturity_audit["run"]
        lines.extend(
            [
                "Select_CString final composition maturity audit:",
                f"- Audit run id: {run['id']}",
                f"- Source final overlay run id: {run['source_final_overlay_run_id']}",
                f"- Audit: {run['audit_name']} ({run['audit_status']})",
                f"- Production release allowed: {int(run['production_release_allowed'] or 0)}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                (
                    f"- Composed released shadow: {int(run['composed_released_count'] or 0):,} "
                    f"({issue_select_cstring_final_composition_maturity_audit['composed_rate']:.2%})"
                ),
                f"- Blocked: {int(run['blocked_count'] or 0):,}",
                f"- Duplicate segments: {int(run['duplicate_segment_count'] or 0):,}",
                f"- Missing source refs: {int(run['missing_source_ref_count'] or 0):,}",
                f"- Invalid source refs: {int(run['invalid_source_ref_count'] or 0):,}",
                f"- Unexpected sources: {int(run['unexpected_source_count'] or 0):,}",
                f"- Maturity status: {run['maturity_status']}",
                f"- Bridge candidate: {int(run['bridge_candidate'] or 0)}",
                f"- Source counts: {json.dumps(issue_select_cstring_final_composition_maturity_audit['source_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Issue counts: {json.dumps(issue_select_cstring_final_composition_maturity_audit['issue_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Report: {run['report_path']}",
                "- Issues:",
                *(
                    [
                        (
                            f"  - {row['issue_code']} {row['relative_path']}::{row['source_key']} "
                            f"{row['issue_detail'] or ''}"
                        )
                        for row in issue_select_cstring_final_composition_maturity_audit["issues"][:8]
                    ]
                    or ["  - none"]
                ),
                "",
            ]
        )
    if issue_select_cstring_governed_bridge_proposal:
        run = issue_select_cstring_governed_bridge_proposal["run"]
        lines.extend(
            [
                "Select_CString governed bridge proposal:",
                f"- Proposal run id: {run['id']}",
                f"- Bridge: {run['bridge_name']} ({run['bridge_status']})",
                f"- Source maturity audit run id: {run['source_maturity_audit_run_id']}",
                f"- Source final overlay run id: {run['source_final_overlay_run_id']}",
                f"- Production release allowed: {int(run['production_release_allowed'] or 0)}",
                f"- Bridge candidate: {int(run['bridge_candidate'] or 0)}",
                f"- Candidates: {int(run['total_candidates'] or 0):,}",
                (
                    f"- Ready: {int(run['ready_count'] or 0):,} "
                    f"({issue_select_cstring_governed_bridge_proposal['ready_rate']:.2%})"
                ),
                f"- Review required: {int(run['review_required_count'] or 0):,}",
                f"- Blocked: {int(run['blocked_count'] or 0):,}",
                f"- Same-token: {int(run['same_token_count'] or 0):,}",
                f"- Dynamic payload deltas: {int(run['dynamic_payload_delta_count'] or 0):,}",
                f"- Status counts: {json.dumps(issue_select_cstring_governed_bridge_proposal['status_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Source counts: {json.dumps(issue_select_cstring_governed_bridge_proposal['source_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Token statuses: {json.dumps(issue_select_cstring_governed_bridge_proposal['token_status_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Report: {run['report_path']}",
                f"- Decisions template: {run['decisions_template_path']}",
                "- Status by risk/token:",
                *[
                    (
                        f"  - {row['bridge_status']} risk={row['risk_level']} "
                        f"token={row['token_status']}: {int(row['count'] or 0):,}"
                    )
                    for row in issue_select_cstring_governed_bridge_proposal["by_status"][:10]
                ],
                "- Ready samples:",
                *(
                    [
                        (
                            f"  - {row['segment_id']} {row['relative_path']}::{row['source_key']} "
                            f"{row['composition_source']} ({row['token_status']}, risk={row['risk_level']})"
                        )
                        for row in issue_select_cstring_governed_bridge_proposal["ready_samples"][:8]
                    ]
                    or ["  - none"]
                ),
                "- Blocked samples:",
                *(
                    [
                        (
                            f"  - {row['segment_id']} {row['relative_path']}::{row['source_key']} "
                            f"{row['bridge_status']} {row['blocking_reason'] or ''}"
                        )
                        for row in issue_select_cstring_governed_bridge_proposal["blocked_rows"][:8]
                    ]
                    or ["  - none"]
                ),
                "",
            ]
        )
    if issue_select_cstring_label_rewrite_checkpoint:
        run = issue_select_cstring_label_rewrite_checkpoint["run"]
        lines.extend(
            [
                "Select_CString visible label rewrite checkpoint:",
                f"- Checkpoint run id: {run['id']}",
                f"- Source checkpoint run id: {run['source_checkpoint_run_id']}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                (
                    f"- Allowed shadow: {int(run['allowed_count'] or 0):,} "
                    f"({issue_select_cstring_label_rewrite_checkpoint['allowed_rate']:.2%})"
                ),
                f"- Blocked: {int(run['blocked_count'] or 0):,}",
                f"- Report: {run['report_path']}",
                "- Subpolicies:",
                *[
                    (
                        f"  - {row['subpolicy_name']} allowed={int(row['checkpoint_allowed'] or 0)} "
                        f"{row['token_status']} block={row['block_reason'] or 'none'}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_select_cstring_label_rewrite_checkpoint["by_subpolicy"][:10]
                ],
                "- Remaining blockers:",
                *(
                    [
                        (
                            f"  - {row['segment_id']} {row['relative_path']}::{row['source_key']} "
                            f"{row['block_reason']}"
                        )
                        for row in issue_select_cstring_label_rewrite_checkpoint["blocked_rows"][:8]
                    ]
                    or ["  - none"]
                ),
                "",
            ]
        )
    if issue_select_cstring_object_sentence_rewrite_checkpoint:
        run = issue_select_cstring_object_sentence_rewrite_checkpoint["run"]
        lines.extend(
            [
                "Select_CString object sentence rewrite checkpoint:",
                f"- Checkpoint run id: {run['id']}",
                f"- Source checkpoint run id: {run['source_checkpoint_run_id']}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                (
                    f"- Allowed shadow: {int(run['allowed_count'] or 0):,} "
                    f"({issue_select_cstring_object_sentence_rewrite_checkpoint['allowed_rate']:.2%})"
                ),
                f"- Blocked: {int(run['blocked_count'] or 0):,}",
                (
                    "- Observed mi persona family count: "
                    f"{issue_select_cstring_object_sentence_rewrite_checkpoint['observed_mi_persona_family_count']:,}"
                ),
                f"- Report: {run['report_path']}",
                "- Subpolicies:",
                *[
                    (
                        f"  - {row['subpolicy_name']} allowed={int(row['checkpoint_allowed'] or 0)} "
                        f"{row['token_status']} block={row['block_reason'] or 'none'}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_select_cstring_object_sentence_rewrite_checkpoint["by_subpolicy"][:10]
                ],
                "- Remaining blockers:",
                *(
                    [
                        (
                            f"  - {row['segment_id']} {row['relative_path']}::{row['source_key']} "
                            f"{row['block_reason']}"
                        )
                        for row in issue_select_cstring_object_sentence_rewrite_checkpoint["blocked_rows"][:8]
                    ]
                    or ["  - none"]
                ),
                "",
            ]
        )
    if issue_nickname_pauper_king_title_semantic_checkpoint:
        run = issue_nickname_pauper_king_title_semantic_checkpoint["run"]
        lines.extend(
            [
                "Nickname Pauper King title semantic checkpoint:",
                f"- Checkpoint run id: {run['id']}",
                f"- Source checkpoint run id: {run['source_checkpoint_run_id']}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                (
                    f"- Allowed shadow: {int(run['allowed_count'] or 0):,} "
                    f"({issue_nickname_pauper_king_title_semantic_checkpoint['allowed_rate']:.2%})"
                ),
                f"- Blocked: {int(run['blocked_count'] or 0):,}",
                f"- Report: {run['report_path']}",
                "- Subpolicies:",
                *[
                    (
                        f"  - {row['subpolicy_name']} allowed={int(row['checkpoint_allowed'] or 0)} "
                        f"{row['token_status']} block={row['block_reason'] or 'none'}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_nickname_pauper_king_title_semantic_checkpoint["by_subpolicy"][:10]
                ],
                "- Ready samples:",
                *(
                    [
                        (
                            f"  - {row['segment_id']} {row['relative_path']}::{row['source_key']} "
                            f"{row['token_status']} ref={row['reference_key']} -> {row['reference_translation']}"
                        )
                        for row in issue_nickname_pauper_king_title_semantic_checkpoint["ready_samples"][:8]
                    ]
                    or ["  - none"]
                ),
                "- Remaining blockers:",
                *(
                    [
                        (
                            f"  - {row['segment_id']} {row['relative_path']}::{row['source_key']} "
                            f"{row['block_reason']}"
                        )
                        for row in issue_nickname_pauper_king_title_semantic_checkpoint["blocked_rows"][:8]
                    ]
                    or ["  - none"]
                ),
                "",
            ]
        )
    if issue_review_dataset_bridge:
        run = issue_review_dataset_bridge["run"]
        lines.extend(
            [
                "Issue review dataset bridge:",
                f"- Dataset run id: {run['id']}",
                f"- Dataset version: {run['dataset_version']}",
                f"- Total dataset examples: {int(run['total_count'] or 0):,}",
                f"- Issue-review trainable macro examples: {int(issue_review_dataset_bridge['trainable_count'] or 0):,}",
                "- Issue-review labels:",
                *[
                    (
                        f"  - {row['label']} / {row['action_label']} / {row['issue_label']}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_review_dataset_bridge["by_label"][:12]
                ],
                "",
            ]
        )
    if short_label_positive_release:
        run = short_label_positive_release["run"]
        lines.extend(
            [
                "Short-label positive release:",
                f"- Release run id: {run['id']}",
                f"- Policy: {run['policy_name']} ({run['policy_status']})",
                f"- Decision run id: {run['decision_run_id']}",
                f"- Queue run id: {run['queue_run_id']}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                f"- Released shadow: {int(run['released_shadow_count'] or 0):,}",
                f"- Blocked: {int(run['blocked_count'] or 0):,}",
                f"- Estimated closed gain: {int(run['estimated_closed_gain'] or 0):,}",
                f"- Release rate: {short_label_positive_release['release_rate']:.2%}",
                f"- Report: {run['report_path']}",
                "- Decisions:",
                *[
                    (
                        f"  - {row['normalized_decision']} allowed={int(row['policy_allowed'] or 0)}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in short_label_positive_release["by_decision"][:8]
                ],
                "- Buckets:",
                *[
                    (
                        f"  - {row['queue_bucket']} allowed={int(row['policy_allowed'] or 0)}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in short_label_positive_release["by_bucket"][:10]
                ],
                "",
            ]
        )
    if short_label_positive_checkpoint:
        run = short_label_positive_checkpoint["run"]
        lines.extend(
            [
                "Short-label positive checkpoint:",
                f"- Checkpoint run id: {run['id']}",
                f"- Checkpoint: {run['checkpoint_name']}",
                f"- Status: {run['checkpoint_status']}",
                f"- Promotion status: {run['promotion_status']}",
                f"- Release run id: {run['release_run_id']}",
                f"- Candidates: {int(run['total_candidates'] or 0):,}",
                f"- Checkpoint allowed: {int(run['checkpoint_allowed_count'] or 0):,}",
                f"- Checkpoint blocked: {int(run['checkpoint_blocked_count'] or 0):,}",
                f"- Estimated closed gain: {int(run['estimated_closed_gain'] or 0):,}",
                f"- Checkpoint rate: {short_label_positive_checkpoint['checkpoint_rate']:.2%}",
                f"- Production release allowed: {int(run['production_release_allowed'] or 0)}",
                f"- Report: {run['report_path']}",
                "- Decisions:",
                *[
                    (
                        f"  - {row['normalized_decision']} allowed={int(row['checkpoint_allowed'] or 0)}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in short_label_positive_checkpoint["by_decision"][:8]
                ],
                "- Buckets:",
                *[
                    (
                        f"  - {row['queue_bucket']} allowed={int(row['checkpoint_allowed'] or 0)}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in short_label_positive_checkpoint["by_bucket"][:10]
                ],
                "",
            ]
        )
    issue_gender_subpolicy_shadow = payload.get("issue_gender_subpolicy_shadow")
    if issue_gender_subpolicy_shadow:
        run = issue_gender_subpolicy_shadow["run"]
        lines.extend(
            [
                "Issue-review gender subpolicy shadow:",
                f"- Shadow run id: {run['id']}",
                f"- Policy: {run['policy_name']} ({run['policy_status']})",
                f"- Decision run id: {run['decision_run_id']}",
                f"- Queue run id: {run['queue_run_id']}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                f"- Shadow ready: {int(run['shadow_ready_count'] or 0):,} ({issue_gender_subpolicy_shadow['ready_rate']:.2%})",
                f"- Boundaries: {int(run['boundary_count'] or 0):,}",
                f"- Context routes: {int(run['context_count'] or 0):,}",
                f"- Positive candidates: {int(run['positive_count'] or 0):,}",
                f"- Blocked: {int(run['blocked_count'] or 0):,}",
                f"- Report: {run['report_path']}",
                "- Subpolicies:",
                *[
                    (
                        f"  - {row['subpolicy_name']} / {row['shadow_status']} / {row['shadow_action']}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_gender_subpolicy_shadow["by_subpolicy"][:12]
                ],
                "- Ready samples:",
                *[
                    (
                        f"  - {row['subpolicy_name']} | {row['relative_path']}::"
                        f"{row['source_key']} ({row['shadow_status']})"
                    )
                    for row in issue_gender_subpolicy_shadow["samples"][:10]
                ],
                "",
            ]
        )
    issue_gender_boundary_checkpoint = payload.get("issue_gender_boundary_checkpoint")
    if issue_gender_boundary_checkpoint:
        run = issue_gender_boundary_checkpoint["run"]
        lines.extend(
            [
                "Issue-review gender boundary checkpoint:",
                f"- Checkpoint run id: {run['id']}",
                f"- Checkpoint: {run['checkpoint_name']}",
                f"- Status: {run['checkpoint_status']}",
                f"- Promotion status: {run['promotion_status']}",
                f"- Shadow run id: {run['shadow_run_id']}",
                f"- Candidates: {int(run['total_candidates'] or 0):,}",
                f"- Checkpoint allowed: {int(run['checkpoint_allowed_count'] or 0):,} ({issue_gender_boundary_checkpoint['checkpoint_rate']:.2%})",
                f"- Checkpoint blocked: {int(run['checkpoint_blocked_count'] or 0):,}",
                f"- Select_CString residual: {int(run['select_cstring_count'] or 0):,}",
                f"- Extra-prefix: {int(run['extra_prefix_count'] or 0):,}",
                f"- Mismatch: {int(run['mismatch_count'] or 0):,}",
                f"- Visible residual: {int(run['visible_residual_count'] or 0):,}",
                f"- Surface boundary: {int(run['surface_boundary_count'] or 0):,}",
                f"- Production release allowed: {int(run['production_release_allowed'] or 0)}",
                f"- Report: {run['report_path']}",
                "- Subpolicies:",
                *[
                    (
                        f"  - {row['subpolicy_name']} / {row['checkpoint_action']} / "
                        f"allowed={int(row['checkpoint_allowed'] or 0)}: {int(row['count'] or 0):,}"
                    )
                    for row in issue_gender_boundary_checkpoint["by_subpolicy"][:12]
                ],
                "- Samples:",
                *[
                    (
                        f"  - {row['subpolicy_name']} | {row['relative_path']}::"
                        f"{row['source_key']} (allowed={int(row['checkpoint_allowed'] or 0)})"
                    )
                    for row in issue_gender_boundary_checkpoint["samples"][:10]
                ],
                "",
            ]
        )
    issue_gender_boundary_lifecycle = payload.get("issue_gender_boundary_lifecycle")
    if issue_gender_boundary_lifecycle:
        run = issue_gender_boundary_lifecycle["run"]
        lines.extend(
            [
                "Issue-review gender boundary lifecycle:",
                f"- Lifecycle run id: {run['id']}",
                f"- Policy: {run['policy_name']} ({run['policy_status']})",
                f"- Policy action: {run['policy_action']}",
                f"- Checkpoint run id: {run['checkpoint_run_id']}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                f"- Released shadow: {int(run['released_count'] or 0):,} ({issue_gender_boundary_lifecycle['release_rate']:.2%})",
                f"- Blocked: {int(run['blocked_count'] or 0):,}",
                f"- Subpolicies: {int(run['subpolicy_count'] or 0):,}",
                f"- Production release allowed: {int(run['production_release_allowed'] or 0)}",
                f"- Report: {run['report_path']}",
                "- Subpolicies:",
                *[
                    (
                        f"  - {row['subpolicy_name']} allowed={int(row['policy_allowed'] or 0)} "
                        f"block={row['block_reason'] or 'none'}: {int(row['count'] or 0):,}"
                    )
                    for row in issue_gender_boundary_lifecycle["by_subpolicy"][:12]
                ],
                "- Samples:",
                *[
                    (
                        f"  - {row['subpolicy_name']} | {row['relative_path']}::"
                        f"{row['source_key']} (allowed={int(row['policy_allowed'] or 0)})"
                    )
                    for row in issue_gender_boundary_lifecycle["samples"][:10]
                ],
                "",
            ]
        )
    issue_trigger_gender_role_surface = payload.get("issue_trigger_gender_role_surface")
    if issue_trigger_gender_role_surface:
        run = issue_trigger_gender_role_surface["run"]
        analytics = issue_trigger_gender_role_surface.get("analytics") or {}
        delegated_rate = float(analytics.get("trigger_gender_role_delegated_rate") or 0.0)
        lines.extend(
            [
                "Issue-review trigger gender-role surface:",
                f"- Shadow run id: {run['id']}",
                f"- Policy: {run['policy_name']} ({run['policy_status']})",
                f"- Decision run id: {run['decision_run_id']}",
                f"- Queue run id: {run['queue_run_id']}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                f"- Shadow ready: {int(run['shadow_ready_count'] or 0):,} ({issue_trigger_gender_role_surface['ready_rate']:.2%})",
                f"- Kinship: {int(run['kinship_count'] or 0):,}",
                f"- Role/article: {int(run['role_article_count'] or 0):,}",
                f"- Blocked: {int(run['blocked_count'] or 0):,}",
                f"- Protected from generic positives: {int(analytics.get('protected_from_generic_positive_count') or 0):,} ({delegated_rate:.2%})",
                f"- Report: {run['report_path']}",
                "- Subpolicies:",
                *[
                    (
                        f"  - {row['subpolicy_name']} / {row['shadow_status']} / {row['shadow_action']}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_trigger_gender_role_surface["by_subpolicy"][:12]
                ],
                "- Ready samples:",
                *[
                    (
                        f"  - {row['subpolicy_name']} | {row['relative_path']}::"
                        f"{row['source_key']} ({row['shadow_status']})"
                    )
                    for row in issue_trigger_gender_role_surface["samples"][:10]
                ],
                "",
            ]
        )
    issue_trigger_gender_role_checkpoint = payload.get("issue_trigger_gender_role_checkpoint")
    if issue_trigger_gender_role_checkpoint:
        run = issue_trigger_gender_role_checkpoint["run"]
        lines.extend(
            [
                "Issue-review trigger gender-role checkpoint:",
                f"- Checkpoint run id: {run['id']}",
                f"- Checkpoint: {run['checkpoint_name']}",
                f"- Status: {run['checkpoint_status']}",
                f"- Promotion status: {run['promotion_status']}",
                f"- Shadow run id: {run['shadow_run_id']}",
                f"- Candidates: {int(run['total_candidates'] or 0):,}",
                f"- Checkpoint allowed: {int(run['checkpoint_allowed_count'] or 0):,} ({issue_trigger_gender_role_checkpoint['checkpoint_rate']:.2%})",
                f"- Checkpoint blocked: {int(run['checkpoint_blocked_count'] or 0):,}",
                f"- Kinship: {int(run['kinship_count'] or 0):,}",
                f"- Role/article: {int(run['role_article_count'] or 0):,}",
                f"- Production release allowed: {int(run['production_release_allowed'] or 0)}",
                f"- Report: {run['report_path']}",
                "- Subpolicies:",
                *[
                    (
                        f"  - {row['subpolicy_name']} / {row['checkpoint_action']} / "
                        f"allowed={int(row['checkpoint_allowed'] or 0)}: {int(row['count'] or 0):,}"
                    )
                    for row in issue_trigger_gender_role_checkpoint["by_subpolicy"][:12]
                ],
                "- Samples:",
                *[
                    (
                        f"  - {row['subpolicy_name']} | {row['relative_path']}::"
                        f"{row['source_key']} (allowed={int(row['checkpoint_allowed'] or 0)})"
                    )
                    for row in issue_trigger_gender_role_checkpoint["samples"][:10]
                ],
                "",
            ]
        )
    issue_trigger_gender_role_lifecycle = payload.get("issue_trigger_gender_role_lifecycle")
    if issue_trigger_gender_role_lifecycle:
        run = issue_trigger_gender_role_lifecycle["run"]
        lines.extend(
            [
                "Issue-review trigger gender-role lifecycle:",
                f"- Lifecycle run id: {run['id']}",
                f"- Policy: {run['policy_name']} ({run['policy_status']})",
                f"- Policy action: {run['policy_action']}",
                f"- Checkpoint run id: {run['checkpoint_run_id']}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                f"- Released shadow: {int(run['released_count'] or 0):,} ({issue_trigger_gender_role_lifecycle['release_rate']:.2%})",
                f"- Blocked: {int(run['blocked_count'] or 0):,}",
                f"- Kinship: {int(run['kinship_count'] or 0):,}",
                f"- Role/article: {int(run['role_article_count'] or 0):,}",
                f"- Subpolicies: {int(run['subpolicy_count'] or 0):,}",
                f"- Production release allowed: {int(run['production_release_allowed'] or 0)}",
                f"- Report: {run['report_path']}",
                "- Subpolicies:",
                *[
                    (
                        f"  - {row['subpolicy_name']} allowed={int(row['policy_allowed'] or 0)} "
                        f"block={row['block_reason'] or 'none'}: {int(row['count'] or 0):,}"
                    )
                    for row in issue_trigger_gender_role_lifecycle["by_subpolicy"][:12]
                ],
                "- Samples:",
                *[
                    (
                        f"  - {row['subpolicy_name']} | {row['relative_path']}::"
                        f"{row['source_key']} (allowed={int(row['policy_allowed'] or 0)})"
                    )
                    for row in issue_trigger_gender_role_lifecycle["samples"][:10]
                ],
                "",
            ]
        )
    if issue_long_text_repair_route_checkpoint:
        run = issue_long_text_repair_route_checkpoint["run"]
        lines.extend(
            [
                "Issue long-text repair route checkpoint:",
                f"- Checkpoint run id: {run['id']}",
                f"- Policy: {run['policy_name']} ({run['policy_status']})",
                f"- Decision run id: {run['decision_run_id']}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                f"- Allowed shadow repairs: {int(run['allowed_count'] or 0):,} ({issue_long_text_repair_route_checkpoint['checkpoint_rate']:.2%})",
                f"- Blocked repairs: {int(run['blocked_count'] or 0):,}",
                f"- Composition-ready observations: {int(run['composition_ready_count'] or 0):,}",
                f"- Report: {run['report_path']}",
                "- Routes:",
                *[
                    (
                        f"  - {row['repair_route']} allowed={int(row['checkpoint_allowed'] or 0)} "
                        f"block={row['block_reason'] or 'none'}: {int(row['count'] or 0):,}"
                    )
                    for row in issue_long_text_repair_route_checkpoint["by_route"][:12]
                ],
                "- Token status:",
                *[
                    (
                        f"  - {row['token_status']} allowed={int(row['checkpoint_allowed'] or 0)}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_long_text_repair_route_checkpoint["by_token"][:8]
                ],
                "",
            ]
        )
    if issue_long_text_repair_route_lifecycle:
        run = issue_long_text_repair_route_lifecycle["run"]
        lines.extend(
            [
                "Issue long-text repair route lifecycle:",
                f"- Lifecycle run id: {run['id']}",
                f"- Policy: {run['policy_name']} ({run['policy_status']})",
                f"- Policy action: {run['policy_action']}",
                f"- Checkpoint run id: {run['checkpoint_run_id']}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                f"- Released shadow: {int(run['released_count'] or 0):,} ({issue_long_text_repair_route_lifecycle['release_rate']:.2%})",
                f"- Blocked: {int(run['blocked_count'] or 0):,}",
                f"- Route counts: {json.dumps(issue_long_text_repair_route_lifecycle['route_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Report: {run['report_path']}",
                "- Routes:",
                *[
                    (
                        f"  - {row['repair_route']} allowed={int(row['policy_allowed'] or 0)} "
                        f"block={row['block_reason'] or 'none'}: {int(row['count'] or 0):,}"
                    )
                    for row in issue_long_text_repair_route_lifecycle["by_route"][:12]
                ],
                "- Token status:",
                *[
                    (
                        f"  - {row['token_status']} allowed={int(row['policy_allowed'] or 0)}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_long_text_repair_route_lifecycle["by_token"][:8]
                ],
                "- Samples:",
                *[
                    (
                        f"  - {row['repair_route']} | {row['relative_path']}::"
                        f"{row['source_key']} (allowed={int(row['policy_allowed'] or 0)})"
                    )
                    for row in issue_long_text_repair_route_lifecycle["samples"][:10]
                ],
                "",
            ]
        )
    if issue_long_text_structural_subpolicy_shadow:
        run = issue_long_text_structural_subpolicy_shadow["run"]
        lines.extend(
            [
                "Issue long-text structural subpolicy shadow:",
                f"- Shadow run id: {run['id']}",
                f"- Policy: {run['policy_name']} ({run['policy_status']})",
                f"- Checkpoint run id: {run['checkpoint_run_id']}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                f"- Shadow ready: {int(run['shadow_ready_count'] or 0):,} ({issue_long_text_structural_subpolicy_shadow['ready_rate']:.2%})",
                f"- Blocked: {int(run['blocked_count'] or 0):,}",
                f"- Subpolicy counts: {json.dumps(issue_long_text_structural_subpolicy_shadow['subpolicy_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Report: {run['report_path']}",
                "- Subpolicies:",
                *[
                    (
                        f"  - {row['subpolicy_name']} / {row['shadow_status']} / "
                        f"{row['shadow_action']} / block={row['block_reason'] or 'none'}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_long_text_structural_subpolicy_shadow["by_subpolicy"][:12]
                ],
                "- Samples:",
                *[
                    (
                        f"  - {row['subpolicy_name']} | {row['relative_path']}::"
                        f"{row['source_key']} ({row['shadow_status']})"
                    )
                    for row in issue_long_text_structural_subpolicy_shadow["samples"][:10]
                ],
                "",
            ]
        )
    if issue_long_text_structural_subpolicy_checkpoint:
        run = issue_long_text_structural_subpolicy_checkpoint["run"]
        lines.extend(
            [
                "Issue long-text structural subpolicy checkpoint:",
                f"- Checkpoint run id: {run['id']}",
                f"- Checkpoint: {run['checkpoint_name']}",
                f"- Status: {run['checkpoint_status']}",
                f"- Promotion status: {run['promotion_status']}",
                f"- Shadow run id: {run['shadow_run_id']}",
                f"- Source checkpoint run id: {run['source_checkpoint_run_id']}",
                f"- Candidates: {int(run['total_candidates'] or 0):,}",
                f"- Checkpoint allowed: {int(run['checkpoint_allowed_count'] or 0):,} ({issue_long_text_structural_subpolicy_checkpoint['checkpoint_rate']:.2%})",
                f"- Checkpoint blocked: {int(run['checkpoint_blocked_count'] or 0):,}",
                f"- Source shadow blocked: {int(run['shadow_blocked_count'] or 0):,}",
                f"- Production release allowed: {int(run['production_release_allowed'] or 0)}",
                f"- Subpolicy counts: {json.dumps(issue_long_text_structural_subpolicy_checkpoint['subpolicy_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Report: {run['report_path']}",
                "- Subpolicies:",
                *[
                    (
                        f"  - {row['subpolicy_name']} / {row['checkpoint_action']} / "
                        f"allowed={int(row['checkpoint_allowed'] or 0)} / block={row['block_reason'] or 'none'}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_long_text_structural_subpolicy_checkpoint["by_subpolicy"][:12]
                ],
                "- Samples:",
                *[
                    (
                        f"  - {row['subpolicy_name']} | {row['relative_path']}::"
                        f"{row['source_key']} (allowed={int(row['checkpoint_allowed'] or 0)})"
                    )
                    for row in issue_long_text_structural_subpolicy_checkpoint["samples"][:10]
                ],
                "",
            ]
        )
    if issue_long_text_structural_subpolicy_lifecycle:
        run = issue_long_text_structural_subpolicy_lifecycle["run"]
        lines.extend(
            [
                "Issue long-text structural subpolicy lifecycle:",
                f"- Lifecycle run id: {run['id']}",
                f"- Policy: {run['policy_name']} ({run['policy_status']})",
                f"- Policy action: {run['policy_action']}",
                f"- Checkpoint run id: {run['checkpoint_run_id']}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                f"- Released shadow: {int(run['released_count'] or 0):,} ({issue_long_text_structural_subpolicy_lifecycle['release_rate']:.2%})",
                f"- Blocked: {int(run['blocked_count'] or 0):,}",
                f"- Production release allowed: {int(run['production_release_allowed'] or 0)}",
                f"- Subpolicy counts: {json.dumps(issue_long_text_structural_subpolicy_lifecycle['subpolicy_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Report: {run['report_path']}",
                "- Subpolicies:",
                *[
                    (
                        f"  - {row['subpolicy_name']} / {row['checkpoint_action']} / "
                        f"allowed={int(row['policy_allowed'] or 0)} / block={row['block_reason'] or 'none'}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_long_text_structural_subpolicy_lifecycle["by_subpolicy"][:12]
                ],
                "- Samples:",
                *[
                    (
                        f"  - {row['subpolicy_name']} | {row['relative_path']}::"
                        f"{row['source_key']} (allowed={int(row['policy_allowed'] or 0)})"
                    )
                    for row in issue_long_text_structural_subpolicy_lifecycle["samples"][:10]
                ],
                "",
            ]
        )
    if issue_long_text_mixed_structural_split:
        run = issue_long_text_mixed_structural_split["run"]
        lines.extend(
            [
                "Issue long-text mixed structural split:",
                f"- Split run id: {run['id']}",
                f"- Policy: {run['policy_name']} ({run['policy_status']})",
                f"- Structural shadow run id: {run['structural_shadow_run_id']}",
                f"- Mixed blockers: {int(run['candidate_count'] or 0):,}",
                f"- Split units: {int(run['split_unit_count'] or 0):,} ({issue_long_text_mixed_structural_split['unit_per_blocker_rate']:.2f} per blocker)",
                f"- Split-ready units: {int(run['split_ready_count'] or 0):,} ({issue_long_text_mixed_structural_split['split_ready_rate']:.2%})",
                f"- Needs token policy: {int(run['needs_token_policy_count'] or 0):,}",
                f"- Needs semantic review: {int(run['needs_semantic_review_count'] or 0):,}",
                f"- Needs more evidence: {int(run['needs_more_evidence_count'] or 0):,}",
                f"- Status counts: {json.dumps(issue_long_text_mixed_structural_split['status_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Microagent counts: {json.dumps(issue_long_text_mixed_structural_split['microagent_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Report: {run['report_path']}",
                "- Microagents:",
                *[
                    (
                        f"  - {row['microagent_key']} / {row['split_status']} / "
                        f"ready={int(row['split_ready'] or 0)}: {int(row['count'] or 0):,}"
                    )
                    for row in issue_long_text_mixed_structural_split["by_microagent"][:20]
                ],
                "- Source blockers:",
                *[
                    (
                        f"  - {row['original_subpolicy_name']} / {row['split_status']}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_long_text_mixed_structural_split["by_source"][:15]
                ],
                "- Samples:",
                *[
                    (
                        f"  - {row['microagent_key']} / {row['micro_issue_kind']} / "
                        f"{row['split_status']} | {row['relative_path']}::{row['source_key']}"
                    )
                    for row in issue_long_text_mixed_structural_split["samples"][:15]
                ],
                "",
            ]
        )
    if issue_long_text_mixed_structural_split_checkpoint:
        run = issue_long_text_mixed_structural_split_checkpoint["run"]
        lines.extend(
            [
                "Issue long-text mixed structural split checkpoint:",
                f"- Checkpoint run id: {run['id']}",
                f"- Checkpoint: {run['checkpoint_name']}",
                f"- Status: {run['checkpoint_status']}",
                f"- Promotion status: {run['promotion_status']}",
                f"- Split run id: {run['split_run_id']}",
                f"- Structural shadow run id: {run['structural_shadow_run_id']}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                f"- Source split-ready count: {int(run['source_split_ready_count'] or 0):,}",
                f"- Checkpoint allowed: {int(run['checkpoint_allowed_count'] or 0):,} ({issue_long_text_mixed_structural_split_checkpoint['checkpoint_rate']:.2%})",
                f"- Checkpoint blocked: {int(run['checkpoint_blocked_count'] or 0):,}",
                f"- Production release allowed: {int(run['production_release_allowed'] or 0)}",
                f"- Microagent counts: {json.dumps(issue_long_text_mixed_structural_split_checkpoint['microagent_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Report: {run['report_path']}",
                "- Microagents:",
                *[
                    (
                        f"  - {row['microagent_key']} / {row['checkpoint_action']} / "
                        f"allowed={int(row['checkpoint_allowed'] or 0)} / block={row['block_reason'] or 'none'}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_long_text_mixed_structural_split_checkpoint["by_microagent"][:15]
                ],
                "- Samples:",
                *[
                    (
                        f"  - {row['microagent_key']} / {row['micro_issue_kind']} / "
                        f"allowed={int(row['checkpoint_allowed'] or 0)} / partial={int(row['partial_component_only'] or 0)} | "
                        f"{row['relative_path']}::{row['source_key']}"
                    )
                    for row in issue_long_text_mixed_structural_split_checkpoint["samples"][:12]
                ],
                "",
            ]
        )
    if issue_long_text_mixed_structural_split_lifecycle:
        run = issue_long_text_mixed_structural_split_lifecycle["run"]
        lines.extend(
            [
                "Issue long-text mixed structural split lifecycle:",
                f"- Lifecycle run id: {run['id']}",
                f"- Policy: {run['policy_name']} ({run['policy_status']})",
                f"- Policy action: {run['policy_action']}",
                f"- Checkpoint run id: {run['checkpoint_run_id']}",
                f"- Split run id: {run['split_run_id']}",
                f"- Structural shadow run id: {run['structural_shadow_run_id']}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                f"- Released shadow: {int(run['released_count'] or 0):,} ({issue_long_text_mixed_structural_split_lifecycle['release_rate']:.2%})",
                f"- Blocked: {int(run['blocked_count'] or 0):,}",
                f"- Partial components: {int(run['partial_component_count'] or 0):,}",
                f"- Production release allowed: {int(run['production_release_allowed'] or 0)}",
                f"- Microagent counts: {json.dumps(issue_long_text_mixed_structural_split_lifecycle['microagent_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Report: {run['report_path']}",
                "- Microagents:",
                *[
                    (
                        f"  - {row['microagent_key']} / {row['policy_action']} / "
                        f"allowed={int(row['policy_allowed'] or 0)} / block={row['block_reason'] or 'none'}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_long_text_mixed_structural_split_lifecycle["by_microagent"][:15]
                ],
                "- Samples:",
                *[
                    (
                        f"  - {row['microagent_key']} / {row['micro_issue_kind']} / "
                        f"allowed={int(row['policy_allowed'] or 0)} / partial={int(row['partial_component_only'] or 0)} | "
                        f"{row['relative_path']}::{row['source_key']}"
                    )
                    for row in issue_long_text_mixed_structural_split_lifecycle["samples"][:12]
                ],
                "",
            ]
        )
    if issue_long_text_mixed_structural_token_policy_shadow:
        run = issue_long_text_mixed_structural_token_policy_shadow["run"]
        lines.extend(
            [
                "Issue long-text mixed structural token-policy shadow:",
                f"- Shadow run id: {run['id']}",
                f"- Policy: {run['policy_name']} ({run['policy_status']})",
                f"- Split run id: {run['split_run_id']}",
                f"- Structural shadow run id: {run['structural_shadow_run_id']}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                f"- Shadow ready: {int(run['shadow_ready_count'] or 0):,} ({issue_long_text_mixed_structural_token_policy_shadow['shadow_ready_rate']:.2%})",
                f"- Superseded: {int(run['superseded_count'] or 0):,}",
                f"- Blocked: {int(run['blocked_count'] or 0):,} ({issue_long_text_mixed_structural_token_policy_shadow['effective_block_rate']:.2%})",
                f"- Status counts: {json.dumps(issue_long_text_mixed_structural_token_policy_shadow['status_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Microagent counts: {json.dumps(issue_long_text_mixed_structural_token_policy_shadow['microagent_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Report: {run['report_path']}",
                "- Microagents:",
                *[
                    (
                        f"  - {row['microagent_key']} / {row['token_policy_status']} / "
                        f"{row['token_policy_action']} / superseded={row['superseded_by'] or 'none'} / "
                        f"block={row['block_reason'] or 'none'}: {int(row['count'] or 0):,}"
                    )
                    for row in issue_long_text_mixed_structural_token_policy_shadow["by_microagent"][:15]
                ],
                "- Samples:",
                *[
                    (
                        f"  - {row['microagent_key']} / {row['micro_issue_kind']} / "
                        f"{row['token_policy_status']} / ready={int(row['shadow_ready'] or 0)} | "
                        f"{row['relative_path']}::{row['source_key']}"
                    )
                    for row in issue_long_text_mixed_structural_token_policy_shadow["samples"][:15]
                ],
                "",
            ]
        )
    if issue_long_text_mixed_structural_token_policy_checkpoint:
        run = issue_long_text_mixed_structural_token_policy_checkpoint["run"]
        lines.extend(
            [
                "Issue long-text mixed structural token-policy checkpoint:",
                f"- Checkpoint run id: {run['id']}",
                f"- Checkpoint: {run['checkpoint_name']}",
                f"- Status: {run['checkpoint_status']}",
                f"- Promotion status: {run['promotion_status']}",
                f"- Token-policy shadow run id: {run['token_policy_shadow_run_id']}",
                f"- Split run id: {run['split_run_id']}",
                f"- Structural shadow run id: {run['structural_shadow_run_id']}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                f"- Source shadow-ready count: {int(run['source_shadow_ready_count'] or 0):,}",
                f"- Checkpoint allowed: {int(run['checkpoint_allowed_count'] or 0):,} ({issue_long_text_mixed_structural_token_policy_checkpoint['checkpoint_rate']:.2%})",
                f"- Checkpoint blocked: {int(run['checkpoint_blocked_count'] or 0):,}",
                f"- Partial components: {int(run['partial_component_only_count'] or 0):,}",
                f"- Production release allowed: {int(run['production_release_allowed'] or 0)}",
                f"- Microagent counts: {json.dumps(issue_long_text_mixed_structural_token_policy_checkpoint['microagent_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Checkpoint action counts: {json.dumps(issue_long_text_mixed_structural_token_policy_checkpoint['checkpoint_action_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Report: {run['report_path']}",
                "- Microagents:",
                *[
                    (
                        f"  - {row['microagent_key']} / {row['checkpoint_action']} / "
                        f"allowed={int(row['checkpoint_allowed'] or 0)} / block={row['block_reason'] or 'none'}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_long_text_mixed_structural_token_policy_checkpoint["by_microagent"][:15]
                ],
                "- Samples:",
                *[
                    (
                        f"  - {row['microagent_key']} / {row['micro_issue_kind']} / "
                        f"allowed={int(row['checkpoint_allowed'] or 0)} / partial={int(row['partial_component_only'] or 0)} | "
                        f"{row['relative_path']}::{row['source_key']}"
                    )
                    for row in issue_long_text_mixed_structural_token_policy_checkpoint["samples"][:12]
                ],
                "",
            ]
        )
    if issue_long_text_mixed_structural_token_policy_lifecycle:
        run = issue_long_text_mixed_structural_token_policy_lifecycle["run"]
        lines.extend(
            [
                "Issue long-text mixed structural token-policy lifecycle:",
                f"- Lifecycle run id: {run['id']}",
                f"- Policy: {run['policy_name']} ({run['policy_status']})",
                f"- Policy action: {run['policy_action']}",
                f"- Checkpoint run id: {run['checkpoint_run_id']}",
                f"- Token-policy shadow run id: {run['token_policy_shadow_run_id']}",
                f"- Split run id: {run['split_run_id']}",
                f"- Structural shadow run id: {run['structural_shadow_run_id']}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                f"- Released shadow: {int(run['released_count'] or 0):,} ({issue_long_text_mixed_structural_token_policy_lifecycle['release_rate']:.2%})",
                f"- Blocked: {int(run['blocked_count'] or 0):,}",
                f"- Partial components: {int(run['partial_component_count'] or 0):,}",
                f"- Production release allowed: {int(run['production_release_allowed'] or 0)}",
                f"- Microagent counts: {json.dumps(issue_long_text_mixed_structural_token_policy_lifecycle['microagent_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Policy action counts: {json.dumps(issue_long_text_mixed_structural_token_policy_lifecycle['policy_action_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Report: {run['report_path']}",
                "- Microagents:",
                *[
                    (
                        f"  - {row['microagent_key']} / {row['policy_action']} / "
                        f"allowed={int(row['policy_allowed'] or 0)} / block={row['block_reason'] or 'none'}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_long_text_mixed_structural_token_policy_lifecycle["by_microagent"][:15]
                ],
                "- Samples:",
                *[
                    (
                        f"  - {row['microagent_key']} / {row['micro_issue_kind']} / "
                        f"allowed={int(row['policy_allowed'] or 0)} / partial={int(row['partial_component_only'] or 0)} | "
                        f"{row['relative_path']}::{row['source_key']}"
                    )
                    for row in issue_long_text_mixed_structural_token_policy_lifecycle["samples"][:12]
                ],
                "",
            ]
        )
    if issue_long_text_mixed_structural_token_policy_blocker_subsplit:
        run = issue_long_text_mixed_structural_token_policy_blocker_subsplit["run"]
        lines.extend(
            [
                "Issue long-text mixed structural token-policy blocker subsplit:",
                f"- Subsplit run id: {run['id']}",
                f"- Policy: {run['policy_name']} ({run['policy_status']})",
                f"- Token-policy shadow run id: {run['token_policy_shadow_run_id']}",
                f"- Split run id: {run['split_run_id']}",
                f"- Structural shadow run id: {run['structural_shadow_run_id']}",
                f"- Source blocked rows: {int(run['source_blocked_count'] or 0):,}",
                f"- Duplicate inputs collapsed: {int(run['duplicate_input_count'] or 0):,}",
                f"- Subsplit units: {int(run['subsplit_unit_count'] or 0):,}",
                f"- Subsplit ready: {int(run['subsplit_ready_count'] or 0):,} ({issue_long_text_mixed_structural_token_policy_blocker_subsplit['ready_rate']:.2%})",
                f"- Needs phrase mapping: {int(run['needs_phrase_mapping_count'] or 0):,}",
                f"- Needs context review: {int(run['needs_context_review_count'] or 0):,}",
                f"- Needs semantic review: {int(run['needs_semantic_review_count'] or 0):,}",
                f"- Production release allowed: {int(run['production_release_allowed'] or 0)}",
                f"- Status counts: {json.dumps(issue_long_text_mixed_structural_token_policy_blocker_subsplit['status_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Microagent counts: {json.dumps(issue_long_text_mixed_structural_token_policy_blocker_subsplit['microagent_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Report: {run['report_path']}",
                "- Microagents:",
                *[
                    (
                        f"  - {row['microagent_key']} / {row['subsplit_status']} / "
                        f"{row['subsplit_action']} / block={row['block_reason'] or 'none'} / "
                        f"route={row['review_route'] or 'none'}: {int(row['count'] or 0):,}"
                    )
                    for row in issue_long_text_mixed_structural_token_policy_blocker_subsplit["by_microagent"][:15]
                ],
                "- Samples:",
                *[
                    (
                        f"  - {row['microagent_key']} / {row['subcomponent_kind']} / "
                        f"{row['subsplit_status']} / ready={int(row['subsplit_ready'] or 0)} | "
                        f"{row['relative_path']}::{row['source_key']}"
                    )
                    for row in issue_long_text_mixed_structural_token_policy_blocker_subsplit["samples"][:12]
                ],
                "",
            ]
        )
    if issue_long_text_subject_pronoun_form_checkpoint:
        run = issue_long_text_subject_pronoun_form_checkpoint["run"]
        lines.extend(
            [
                "Issue long-text subject pronoun form checkpoint:",
                f"- Checkpoint run id: {run['id']}",
                f"- Checkpoint: {run['checkpoint_name']}",
                f"- Status: {run['checkpoint_status']}",
                f"- Promotion status: {run['promotion_status']}",
                f"- Subsplit run id: {run['subsplit_run_id']}",
                f"- Token-policy shadow run id: {run['token_policy_shadow_run_id']}",
                f"- Split run id: {run['split_run_id']}",
                f"- Structural shadow run id: {run['structural_shadow_run_id']}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                f"- Source subsplit-ready count: {int(run['source_subsplit_ready_count'] or 0):,}",
                f"- Checkpoint allowed: {int(run['checkpoint_allowed_count'] or 0):,} ({issue_long_text_subject_pronoun_form_checkpoint['checkpoint_rate']:.2%})",
                f"- Checkpoint blocked: {int(run['checkpoint_blocked_count'] or 0):,}",
                f"- Partial components: {int(run['partial_component_only_count'] or 0):,}",
                f"- Production release allowed: {int(run['production_release_allowed'] or 0)}",
                f"- Microagent counts: {json.dumps(issue_long_text_subject_pronoun_form_checkpoint['microagent_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Checkpoint action counts: {json.dumps(issue_long_text_subject_pronoun_form_checkpoint['checkpoint_action_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Report: {run['report_path']}",
                "- Microagents:",
                *[
                    (
                        f"  - {row['microagent_key']} / {row['checkpoint_action']} / "
                        f"allowed={int(row['checkpoint_allowed'] or 0)} / block={row['block_reason'] or 'none'}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_long_text_subject_pronoun_form_checkpoint["by_microagent"][:15]
                ],
                "- Samples:",
                *[
                    (
                        f"  - {row['microagent_key']} / {row['subcomponent_kind']} / "
                        f"allowed={int(row['checkpoint_allowed'] or 0)} / partial={int(row['partial_component_only'] or 0)} | "
                        f"{row['relative_path']}::{row['source_key']}"
                    )
                    for row in issue_long_text_subject_pronoun_form_checkpoint["samples"][:12]
                ],
                "",
            ]
        )
    if issue_long_text_subject_pronoun_form_lifecycle:
        run = issue_long_text_subject_pronoun_form_lifecycle["run"]
        lines.extend(
            [
                "Issue long-text subject pronoun form lifecycle:",
                f"- Lifecycle run id: {run['id']}",
                f"- Policy: {run['policy_name']} ({run['policy_status']})",
                f"- Policy action: {run['policy_action']}",
                f"- Checkpoint run id: {run['checkpoint_run_id']}",
                f"- Subsplit run id: {run['subsplit_run_id']}",
                f"- Token-policy shadow run id: {run['token_policy_shadow_run_id']}",
                f"- Split run id: {run['split_run_id']}",
                f"- Structural shadow run id: {run['structural_shadow_run_id']}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                f"- Released shadow: {int(run['released_count'] or 0):,} ({issue_long_text_subject_pronoun_form_lifecycle['release_rate']:.2%})",
                f"- Blocked: {int(run['blocked_count'] or 0):,}",
                f"- Partial components: {int(run['partial_component_count'] or 0):,}",
                f"- Production release allowed: {int(run['production_release_allowed'] or 0)}",
                f"- Microagent counts: {json.dumps(issue_long_text_subject_pronoun_form_lifecycle['microagent_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Policy action counts: {json.dumps(issue_long_text_subject_pronoun_form_lifecycle['policy_action_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Report: {run['report_path']}",
                "- Microagents:",
                *[
                    (
                        f"  - {row['microagent_key']} / {row['policy_action']} / "
                        f"allowed={int(row['policy_allowed'] or 0)} / block={row['block_reason'] or 'none'}: "
                        f"{int(row['count'] or 0):,}"
                    )
                    for row in issue_long_text_subject_pronoun_form_lifecycle["by_microagent"][:15]
                ],
                "- Samples:",
                *[
                    (
                        f"  - {row['microagent_key']} / {row['subcomponent_kind']} / "
                        f"allowed={int(row['policy_allowed'] or 0)} / partial={int(row['partial_component_only'] or 0)} | "
                        f"{row['relative_path']}::{row['source_key']}"
                    )
                    for row in issue_long_text_subject_pronoun_form_lifecycle["samples"][:12]
                ],
                "",
            ]
        )
    if issue_long_text_partial_composition_impact:
        run = issue_long_text_partial_composition_impact["run"]
        lines.extend(
            [
                "Issue long-text partial composition impact:",
                f"- Impact run id: {run['id']}",
                f"- Policy: {run['policy_name']}",
                f"- Decision run id: {run['decision_run_id']}",
                f"- Candidate segments: {int(run['candidate_segment_count'] or 0):,}",
                f"- Review-closed baseline: {int(run['review_closed_baseline_count'] or 0):,}",
                f"- Released components: {int(run['released_component_count'] or 0):,}",
                f"- Segments with released components: {int(run['released_component_segment_count'] or 0):,} ({issue_long_text_partial_composition_impact['component_segment_rate']:.2%})",
                f"- Blocker components: {int(run['blocker_component_count'] or 0):,}",
                f"- Segments with blockers: {int(run['blocker_segment_count'] or 0):,}",
                f"- Candidate composition recheck: {int(run['candidate_recheck_segment_count'] or 0):,}",
                f"- Partial-covered but blocked: {int(run['partial_covered_blocked_segment_count'] or 0):,}",
                f"- Blocked without released component: {int(run['blocked_no_released_segment_count'] or 0):,}",
                f"- Unmapped needs-repair: {int(run['unmapped_needs_repair_segment_count'] or 0):,}",
                f"- Potential close after recheck: {int(run['potential_close_after_recheck_count'] or 0):,} ({issue_long_text_partial_composition_impact['potential_close_rate']:.2%})",
                f"- Coverage states: {json.dumps(issue_long_text_partial_composition_impact['coverage_state_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Component sources: {json.dumps(issue_long_text_partial_composition_impact['component_source_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Blocker routes: {json.dumps(issue_long_text_partial_composition_impact['blocker_route_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Report: {run['report_path']}",
                "- States:",
                *[
                    (
                        f"  - {row['coverage_state']}: segments={int(row['segment_count'] or 0):,}, "
                        f"components={int(row['released_component_count'] or 0):,}, "
                        f"blockers={int(row['blocker_count'] or 0):,}, "
                        f"potential={int(row['potential_close_after_recheck'] or 0):,}"
                    )
                    for row in issue_long_text_partial_composition_impact["by_state"]
                ],
                "- Samples:",
                *[
                    (
                        f"  - {row['coverage_state']} / components={int(row['released_component_count'] or 0)} / "
                        f"blockers={int(row['blocker_count'] or 0)} / potential={int(row['potential_close_after_recheck'] or 0)} | "
                        f"{row['relative_path']}::{row['source_key']}"
                    )
                    for row in issue_long_text_partial_composition_impact["samples"][:12]
                ],
                "",
            ]
        )
    if issue_long_text_composition_recheck_queue:
        run = issue_long_text_composition_recheck_queue["run"]
        lines.extend(
            [
                "Issue long-text composition recheck queue:",
                f"- Recheck run id: {run['id']}",
                f"- Queue: {run['queue_name']}",
                f"- Strategy: {run['queue_strategy']}",
                f"- Impact run id: {run['impact_run_id']}",
                f"- Decision run id: {run['decision_run_id']}",
                f"- Review queue run id: {run['review_queue_run_id']}",
                f"- Candidates: {int(run['candidate_count'] or 0):,}",
                f"- Selected: {int(run['selected_count'] or 0):,} ({issue_long_text_composition_recheck_queue['selected_rate']:.2%})",
                f"- Open: {int(run['open_count'] or 0):,}",
                f"- Reviewed: {int(run['reviewed_count'] or 0):,}",
                f"- Released components represented: {int(run['released_component_count'] or 0):,}",
                f"- Component sources: {json.dumps(issue_long_text_composition_recheck_queue['component_source_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Buckets: {json.dumps(issue_long_text_composition_recheck_queue['bucket_counts'], ensure_ascii=False, sort_keys=True)}",
                f"- Report: {run['report_path']}",
                f"- Decisions template: {run['decisions_template_path']}",
                "- Buckets:",
                *[
                    (
                        f"  - {row['queue_bucket']} / suggested={row['suggested_decision']}: "
                        f"{int(row['count'] or 0):,} segments, "
                        f"{int(row['released_component_count'] or 0):,} components"
                    )
                    for row in issue_long_text_composition_recheck_queue["by_bucket"]
                ],
                "- Samples:",
                *[
                    (
                        f"  - queue_item={row['review_queue_item_id']} / components={int(row['released_component_count'] or 0)} / "
                        f"{row['relative_path']}::{row['source_key']}"
                    )
                    for row in issue_long_text_composition_recheck_queue["samples"][:12]
                ],
                "",
            ]
        )
    if macro_model_status:
        active = macro_model_status.get("active")
        latest = macro_model_status.get("latest")
        lines.append("Macro model status:")
        if active:
            lines.extend(
                [
                    f"- Active model run id: {active['active_model_run_id']}",
                    f"- Active dataset run id: {active['dataset_run_id']}",
                    f"- Active macro F1: {float(active['macro_f1'] or 0.0):.4f}",
                    f"- Active false-safe: {int(active['false_safe_count'] or 0)} ({float(active['false_safe_rate'] or 0.0):.2%})",
                    f"- Active safe recall: {float(active['safe_recall'] or 0.0):.2%}",
                ]
            )
        if latest:
            lines.extend(
                [
                    f"- Latest candidate model run id: {latest['id']}",
                    f"- Candidate dataset run id: {latest['dataset_run_id']}",
                    f"- Candidate macro F1: {float(latest['macro_f1'] or 0.0):.4f}",
                    f"- Candidate false-safe: {int(latest['false_safe_count'] or 0)} ({float(latest['false_safe_rate'] or 0.0):.2%})",
                    f"- Candidate safe recall: {float(latest['safe_recall'] or 0.0):.2%}",
                    f"- Promotion hint: {macro_model_status['promotion_hint']}",
                ]
            )
        lines.append("")
    latest_holdout_eval = payload.get("latest_holdout_eval")
    if latest_holdout_eval:
        run = latest_holdout_eval["run"]
        lines.extend(
            [
                "Latest macro holdout evaluation:",
                f"- Holdout run id: {run['id']}",
                f"- Dataset run id: {run['dataset_run_id']}",
                f"- Feature set: {run['feature_set']}",
                f"- Train strategy: {run['train_strategy']}",
                f"- Holdout paths: {int(run['holdout_path_count'] or 0):,}",
                f"- Holdout examples: {int(run['holdout_examples'] or 0):,}",
                f"- Predicted safe: {int(run['predicted_safe_count'] or 0):,}",
                f"- False-safe: {int(run['false_safe_count'] or 0):,} ({float(run['false_safe_rate'] or 0.0):.2%})",
                f"- Safe precision: {float(run['safe_precision'] or 0.0):.2%}",
                f"- Safe recall: {float(run['safe_recall'] or 0.0):.2%}",
            ]
        )
        if latest_holdout_eval.get("per_path_summary"):
            lines.extend(
                [
                    "- Per-path risk summary:",
                    *[
                        f"  - {str(row).lstrip('- ').strip()}"
                        for row in latest_holdout_eval["per_path_summary"][:8]
                    ],
                ]
            )
        lines.append("")
    latest_model_promotion = payload.get("latest_model_promotion")
    if latest_model_promotion:
        run = latest_model_promotion["run"]
        metrics = latest_model_promotion.get("metrics") or {}
        candidate_score = metrics.get("candidate_score_run") or {}
        active_score = metrics.get("active_score_run") or {}
        lines.extend(
            [
                "Latest macro promotion gate:",
                f"- Promotion run id: {run['id']}",
                f"- Candidate model run id: {run['candidate_model_run_id']}",
                f"- Previous active model run id: {run['previous_model_run_id']}",
                f"- Decision: {run['decision']}",
                f"- Reason: {run['reason']}",
            ]
        )
        if candidate_score:
            lines.append(
                f"- Candidate operational auto-safe: {int(candidate_score.get('final_auto_safe_count') or 0):,}/"
                f"{int(candidate_score.get('scored_count') or 0):,}"
            )
        if active_score:
            lines.append(
                f"- Active operational auto-safe: {int(active_score.get('final_auto_safe_count') or 0):,}/"
                f"{int(active_score.get('scored_count') or 0):,}"
            )
        lines.append("")
    if active_policy_gap:
        totals = active_policy_gap.get("totals") or {}
        gap_total = int(totals.get("total") or 0)
        lines.extend(
            [
                "Active-safe versus current-policy gap:",
                f"- Segment-state run id: {active_policy_gap['segment_state_run_id']}",
                f"- Active score run id: {active_policy_gap['active_score_run_id']}",
                f"- Candidate score run id: {active_policy_gap['candidate_score_run_id']}",
                f"- Policy run id: {active_policy_gap['policy_run_id']}",
                f"- Pending active_auto_safe + policy_needs_autofix: {gap_total:,}",
                (
                    "- Safe probability averages: "
                    f"active={float(totals.get('active_avg_safe_probability') or 0.0):.4f}, "
                    f"candidate={float(totals.get('candidate_avg_safe_probability') or 0.0):.4f}"
                ),
                (
                    "- Learned evidence inside gap: "
                    f"positive={int(totals.get('learned_positive') or 0):,}, "
                    f"negative={int(totals.get('learned_negative') or 0):,}"
                ),
                f"- Candidate probability buckets: {json.dumps(active_policy_gap['candidate_probability_buckets'], ensure_ascii=False, sort_keys=True)}",
                "- Top paths:",
                *[
                    (
                        f"  - {row['relative_path']}: {int(row['count'] or 0):,}, "
                        f"active_avg={float(row['active_avg_safe_probability'] or 0.0):.4f}, "
                        f"candidate_avg={float(row['candidate_avg_safe_probability'] or 0.0):.4f}, "
                        f"pos={int(row['learned_positive'] or 0)}, neg={int(row['learned_negative'] or 0)}"
                    )
                    for row in active_policy_gap["by_path"][:12]
                ],
                "- Key shapes:",
                *[
                    f"  - {row['key_shape']}: {int(row['count'] or 0):,}"
                    for row in active_policy_gap["by_key_shape"][:12]
                ],
                "- Near-threshold samples:",
                *[
                    (
                        f"  - {row['relative_path']}::{row['source_key']} "
                        f"active={float(row['active_safe_probability'] or 0.0):.4f}, "
                        f"candidate={float(row['candidate_safe_probability'] or 0.0):.4f}"
                    )
                    for row in active_policy_gap["top_near_threshold"][:10]
                ],
                "- Policy-safe rows still blocked by candidate autofix:",
                *(
                    [
                        (
                            f"  - {row['policy_group']} | {row['relative_path']}: "
                            f"{int(row['count'] or 0):,}, "
                            f"avg={float(row['avg_safe_probability'] or 0.0):.4f}, "
                            f"active_safe={int(row['active_auto_safe_count'] or 0)}, "
                            f"active_human={int(row['active_needs_human_count'] or 0)}, "
                            f"pos={int(row['learned_positive'] or 0)}, "
                            f"neg={int(row['learned_negative'] or 0)}"
                        )
                        for row in active_policy_gap["policy_safe_candidate_autofix_blocks"][:12]
                    ]
                    or ["  - none"]
                ),
                "",
            ]
        )
    lines.extend(
        [
            "Recommended next actions:",
            *[f"- {action}" for action in payload["next_actions"]],
            "",
            "Safety note:",
            "- Diagnostic only: no source/output reads, no model promotion and no output writes.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> dict[str, Any]:
    settings = db.load_settings()
    generated_at = datetime.now().isoformat(timespec="seconds")
    txt_path, json_path = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        registry = summarize_agent_registry(conn)
        routing = summarize_latest_routing(conn)
        text_audit = summarize_text_audit(conn)
        weak_auto_split_agents = summarize_weak_auto_split_agents(conn)
        text_shadow_policies = summarize_text_shadow_policies(conn)
        text_boundary_policies = summarize_text_boundary_policies(conn)
        text_boundary_repair_queues = summarize_text_boundary_repair_queues(conn)
        text_boundary_repair_shadow_policies = summarize_text_boundary_repair_shadow_policies(conn)
        text_boundary_repair_checkpoints = summarize_text_boundary_repair_checkpoints(conn)
        text_boundary_repair_lifecycle_policies = summarize_text_boundary_repair_lifecycle_policies(conn)
        text_boundary_token_policy_bridges = summarize_text_boundary_token_policy_bridges(conn)
        text_boundary_token_subpolicy_shadows = summarize_text_boundary_token_subpolicy_shadows(conn)
        text_boundary_token_subpolicy_checkpoints = summarize_text_boundary_token_subpolicy_checkpoints(conn)
        text_boundary_token_subpolicy_lifecycle_policies = summarize_text_boundary_token_subpolicy_lifecycle_policies(conn)
        text_boundary_token_subpolicy_production_audits = summarize_text_boundary_token_subpolicy_production_audits(conn)
        text_boundary_repair_production_audits = summarize_text_boundary_repair_production_audits(conn)
        text_policy_checkpoints = summarize_text_policy_checkpoints(conn)
        reopen_lifecycle_policies = summarize_reopen_lifecycle_policies(conn)
        divine_realm_policy = summarize_divine_realm_symbolic_policy(conn)
        boundary_correction_queues = summarize_boundary_correction_queues(conn)
        specialist_policy = summarize_specialist_policy(conn)
        recommendations = summarize_recommendations(conn)
        segment_state = summarize_segment_state(conn)
        issue_ledger = summarize_issue_ledger(conn)
        issue_partial_coverage = summarize_issue_partial_coverage(conn)
        issue_review_queue = summarize_issue_review_queue(conn)
        issue_review_decisions = summarize_issue_review_decisions(conn)
        issue_dynamic_literal_repair = summarize_issue_dynamic_literal_repair(conn)
        issue_select_cstring_auxiliary_rewrite_checkpoint = (
            summarize_issue_select_cstring_auxiliary_rewrite_checkpoint(conn)
        )
        issue_select_cstring_antpath_relation_rewrite_checkpoint = (
            summarize_issue_select_cstring_antpath_relation_rewrite_checkpoint(conn)
        )
        issue_select_cstring_same_token_lifecycle = summarize_issue_select_cstring_same_token_lifecycle(conn)
        issue_select_cstring_residual_literal_cleanup_checkpoint = (
            summarize_issue_select_cstring_residual_literal_cleanup_checkpoint(conn)
        )
        issue_select_cstring_same_token_composition_overlay = (
            summarize_issue_select_cstring_same_token_composition_overlay(conn)
        )
        issue_select_cstring_dynamic_literal_payload_checkpoint = (
            summarize_issue_select_cstring_dynamic_literal_payload_checkpoint(conn)
        )
        issue_select_cstring_multistage_composition_overlay = (
            summarize_issue_select_cstring_multistage_composition_overlay(conn)
        )
        issue_select_cstring_local_player_pronoun_checkpoint = (
            summarize_issue_select_cstring_local_player_pronoun_checkpoint(conn)
        )
        issue_select_cstring_final_composition_overlay = (
            summarize_issue_select_cstring_final_composition_overlay(conn)
        )
        issue_select_cstring_final_composition_maturity_audit = (
            summarize_issue_select_cstring_final_composition_maturity_audit(conn)
        )
        issue_select_cstring_governed_bridge_proposal = (
            summarize_issue_select_cstring_governed_bridge_proposal(conn)
        )
        issue_select_cstring_label_rewrite_checkpoint = summarize_issue_select_cstring_label_rewrite_checkpoint(conn)
        issue_select_cstring_object_sentence_rewrite_checkpoint = (
            summarize_issue_select_cstring_object_sentence_rewrite_checkpoint(conn)
        )
        issue_nickname_pauper_king_title_semantic_checkpoint = (
            summarize_issue_nickname_pauper_king_title_semantic_checkpoint(conn)
        )
        issue_review_dataset_bridge = summarize_issue_review_dataset_bridge(conn)
        short_label_positive_release = summarize_short_label_positive_release(conn)
        short_label_positive_checkpoint = summarize_short_label_positive_checkpoint(conn)
        issue_gender_subpolicy_shadow = summarize_issue_gender_subpolicy_shadow(conn)
        issue_gender_boundary_checkpoint = summarize_issue_gender_boundary_checkpoint(conn)
        issue_gender_boundary_lifecycle = summarize_issue_gender_boundary_lifecycle(conn)
        issue_trigger_gender_role_surface = summarize_issue_trigger_gender_role_surface(conn)
        issue_trigger_gender_role_checkpoint = summarize_issue_trigger_gender_role_checkpoint(conn)
        issue_trigger_gender_role_lifecycle = summarize_issue_trigger_gender_role_lifecycle(conn)
        issue_long_text_repair_route_checkpoint = summarize_issue_long_text_repair_route_checkpoint(conn)
        issue_long_text_repair_route_lifecycle = summarize_issue_long_text_repair_route_lifecycle(conn)
        issue_long_text_structural_subpolicy_shadow = summarize_issue_long_text_structural_subpolicy_shadow(conn)
        issue_long_text_structural_subpolicy_checkpoint = summarize_issue_long_text_structural_subpolicy_checkpoint(conn)
        issue_long_text_structural_subpolicy_lifecycle = summarize_issue_long_text_structural_subpolicy_lifecycle(conn)
        issue_long_text_mixed_structural_split = summarize_issue_long_text_mixed_structural_split(conn)
        issue_long_text_mixed_structural_split_checkpoint = summarize_issue_long_text_mixed_structural_split_checkpoint(conn)
        issue_long_text_mixed_structural_split_lifecycle = summarize_issue_long_text_mixed_structural_split_lifecycle(conn)
        issue_long_text_mixed_structural_token_policy_shadow = summarize_issue_long_text_mixed_structural_token_policy_shadow(conn)
        issue_long_text_mixed_structural_token_policy_checkpoint = summarize_issue_long_text_mixed_structural_token_policy_checkpoint(conn)
        issue_long_text_mixed_structural_token_policy_lifecycle = summarize_issue_long_text_mixed_structural_token_policy_lifecycle(conn)
        issue_long_text_mixed_structural_token_policy_blocker_subsplit = summarize_issue_long_text_mixed_structural_token_policy_blocker_subsplit(conn)
        issue_long_text_subject_pronoun_form_checkpoint = summarize_issue_long_text_subject_pronoun_form_checkpoint(conn)
        issue_long_text_subject_pronoun_form_lifecycle = summarize_issue_long_text_subject_pronoun_form_lifecycle(conn)
        issue_long_text_partial_composition_impact = summarize_issue_long_text_partial_composition_impact(conn)
        issue_long_text_composition_recheck_queue = summarize_issue_long_text_composition_recheck_queue(conn)
        macro_model_status = summarize_macro_model_status(conn)
        latest_holdout_eval = summarize_latest_holdout_eval(conn)
        latest_model_promotion = summarize_latest_model_promotion(conn)
        active_policy_gap = summarize_active_policy_gap(conn)
        payload = {
            "rule_version": RULE_VERSION,
            "generated_at": generated_at,
            "registry": registry,
            "routing": routing,
            "text_audit": text_audit,
            "weak_auto_split_agents": weak_auto_split_agents,
            "text_shadow_policies": text_shadow_policies,
            "text_boundary_policies": text_boundary_policies,
            "text_boundary_repair_queues": text_boundary_repair_queues,
            "text_boundary_repair_shadow_policies": text_boundary_repair_shadow_policies,
            "text_boundary_repair_checkpoints": text_boundary_repair_checkpoints,
            "text_boundary_repair_lifecycle_policies": text_boundary_repair_lifecycle_policies,
            "text_boundary_token_policy_bridges": text_boundary_token_policy_bridges,
            "text_boundary_token_subpolicy_shadows": text_boundary_token_subpolicy_shadows,
            "text_boundary_token_subpolicy_checkpoints": text_boundary_token_subpolicy_checkpoints,
            "text_boundary_token_subpolicy_lifecycle_policies": text_boundary_token_subpolicy_lifecycle_policies,
            "text_boundary_token_subpolicy_production_audits": text_boundary_token_subpolicy_production_audits,
            "text_boundary_repair_production_audits": text_boundary_repair_production_audits,
            "text_policy_checkpoints": text_policy_checkpoints,
            "reopen_lifecycle_policies": reopen_lifecycle_policies,
            "divine_realm_policy": divine_realm_policy,
            "boundary_correction_queues": boundary_correction_queues,
            "specialist_policy": specialist_policy,
            "recommendations": recommendations,
            "segment_state": segment_state,
            "issue_ledger": issue_ledger,
            "issue_partial_coverage": issue_partial_coverage,
            "issue_review_queue": issue_review_queue,
            "issue_review_decisions": issue_review_decisions,
            "issue_dynamic_literal_repair": issue_dynamic_literal_repair,
            "issue_select_cstring_auxiliary_rewrite_checkpoint": issue_select_cstring_auxiliary_rewrite_checkpoint,
            "issue_select_cstring_antpath_relation_rewrite_checkpoint": issue_select_cstring_antpath_relation_rewrite_checkpoint,
            "issue_select_cstring_same_token_lifecycle": issue_select_cstring_same_token_lifecycle,
            "issue_select_cstring_residual_literal_cleanup_checkpoint": issue_select_cstring_residual_literal_cleanup_checkpoint,
            "issue_select_cstring_same_token_composition_overlay": issue_select_cstring_same_token_composition_overlay,
            "issue_select_cstring_dynamic_literal_payload_checkpoint": issue_select_cstring_dynamic_literal_payload_checkpoint,
            "issue_select_cstring_multistage_composition_overlay": issue_select_cstring_multistage_composition_overlay,
            "issue_select_cstring_local_player_pronoun_checkpoint": issue_select_cstring_local_player_pronoun_checkpoint,
            "issue_select_cstring_final_composition_overlay": issue_select_cstring_final_composition_overlay,
            "issue_select_cstring_final_composition_maturity_audit": issue_select_cstring_final_composition_maturity_audit,
            "issue_select_cstring_governed_bridge_proposal": issue_select_cstring_governed_bridge_proposal,
            "issue_select_cstring_label_rewrite_checkpoint": issue_select_cstring_label_rewrite_checkpoint,
            "issue_select_cstring_object_sentence_rewrite_checkpoint": issue_select_cstring_object_sentence_rewrite_checkpoint,
            "issue_nickname_pauper_king_title_semantic_checkpoint": issue_nickname_pauper_king_title_semantic_checkpoint,
            "issue_review_dataset_bridge": issue_review_dataset_bridge,
            "short_label_positive_release": short_label_positive_release,
            "short_label_positive_checkpoint": short_label_positive_checkpoint,
            "issue_gender_subpolicy_shadow": issue_gender_subpolicy_shadow,
            "issue_gender_boundary_checkpoint": issue_gender_boundary_checkpoint,
            "issue_gender_boundary_lifecycle": issue_gender_boundary_lifecycle,
            "issue_trigger_gender_role_surface": issue_trigger_gender_role_surface,
            "issue_trigger_gender_role_checkpoint": issue_trigger_gender_role_checkpoint,
            "issue_trigger_gender_role_lifecycle": issue_trigger_gender_role_lifecycle,
            "issue_long_text_repair_route_checkpoint": issue_long_text_repair_route_checkpoint,
            "issue_long_text_repair_route_lifecycle": issue_long_text_repair_route_lifecycle,
            "issue_long_text_structural_subpolicy_shadow": issue_long_text_structural_subpolicy_shadow,
            "issue_long_text_structural_subpolicy_checkpoint": issue_long_text_structural_subpolicy_checkpoint,
            "issue_long_text_structural_subpolicy_lifecycle": issue_long_text_structural_subpolicy_lifecycle,
            "issue_long_text_mixed_structural_split": issue_long_text_mixed_structural_split,
            "issue_long_text_mixed_structural_split_checkpoint": issue_long_text_mixed_structural_split_checkpoint,
            "issue_long_text_mixed_structural_split_lifecycle": issue_long_text_mixed_structural_split_lifecycle,
            "issue_long_text_mixed_structural_token_policy_shadow": issue_long_text_mixed_structural_token_policy_shadow,
            "issue_long_text_mixed_structural_token_policy_checkpoint": issue_long_text_mixed_structural_token_policy_checkpoint,
            "issue_long_text_mixed_structural_token_policy_lifecycle": issue_long_text_mixed_structural_token_policy_lifecycle,
            "issue_long_text_mixed_structural_token_policy_blocker_subsplit": issue_long_text_mixed_structural_token_policy_blocker_subsplit,
            "issue_long_text_subject_pronoun_form_checkpoint": issue_long_text_subject_pronoun_form_checkpoint,
            "issue_long_text_subject_pronoun_form_lifecycle": issue_long_text_subject_pronoun_form_lifecycle,
            "issue_long_text_partial_composition_impact": issue_long_text_partial_composition_impact,
            "issue_long_text_composition_recheck_queue": issue_long_text_composition_recheck_queue,
            "macro_model_status": macro_model_status,
            "latest_holdout_eval": latest_holdout_eval,
            "latest_model_promotion": latest_model_promotion,
            "active_policy_gap": active_policy_gap,
            "next_actions": build_next_actions(
                text_audit,
                weak_auto_split_agents,
                text_shadow_policies,
                text_boundary_policies,
                text_boundary_repair_queues,
                text_boundary_repair_shadow_policies,
                text_boundary_repair_checkpoints,
                text_boundary_repair_lifecycle_policies,
                text_boundary_token_policy_bridges,
                text_boundary_token_subpolicy_shadows,
                text_boundary_token_subpolicy_checkpoints,
                text_boundary_token_subpolicy_lifecycle_policies,
                text_boundary_token_subpolicy_production_audits,
                text_boundary_repair_production_audits,
                text_policy_checkpoints,
                reopen_lifecycle_policies,
                divine_realm_policy,
                boundary_correction_queues,
                recommendations,
                specialist_policy,
                issue_ledger,
                issue_partial_coverage,
                issue_review_queue,
                issue_review_decisions,
                issue_review_dataset_bridge,
                short_label_positive_release,
                short_label_positive_checkpoint,
                issue_gender_subpolicy_shadow,
                issue_gender_boundary_checkpoint,
                issue_gender_boundary_lifecycle,
                issue_trigger_gender_role_surface,
                issue_trigger_gender_role_checkpoint,
                issue_trigger_gender_role_lifecycle,
                issue_long_text_repair_route_checkpoint,
                issue_long_text_repair_route_lifecycle,
                issue_long_text_structural_subpolicy_shadow,
                issue_long_text_structural_subpolicy_checkpoint,
                issue_long_text_structural_subpolicy_lifecycle,
                issue_long_text_mixed_structural_split,
                issue_long_text_mixed_structural_split_checkpoint,
                issue_long_text_mixed_structural_split_lifecycle,
                issue_long_text_mixed_structural_token_policy_shadow,
                issue_long_text_mixed_structural_token_policy_checkpoint,
                issue_long_text_mixed_structural_token_policy_lifecycle,
                issue_long_text_mixed_structural_token_policy_blocker_subsplit,
                issue_long_text_subject_pronoun_form_checkpoint,
                issue_long_text_subject_pronoun_form_lifecycle,
                issue_long_text_partial_composition_impact,
                issue_long_text_composition_recheck_queue,
                macro_model_status,
                latest_holdout_eval,
                latest_model_promotion,
                active_policy_gap,
            ),
            "report_path": str(txt_path),
            "json_path": str(json_path),
        }
    write_report(txt_path=txt_path, json_path=json_path, payload=payload)
    print("[learning_network_diagnostic] Diagnostic generated")
    print(f"[learning_network_diagnostic] Report: {txt_path}")
    print(f"[learning_network_diagnostic] JSON: {json_path}")
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate a consolidated learning-network diagnostic report.")
    parser.parse_args()
    main()
