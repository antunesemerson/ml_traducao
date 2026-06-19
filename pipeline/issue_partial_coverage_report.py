from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_partial_coverage_report_v1"


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


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_partial_coverage"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".json")


def parse_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def ratio(numerator: int | float, denominator: int | float) -> float:
    if not denominator:
        return 0.0
    return float(numerator) / float(denominator)


def coverage_bucket(value: float) -> str:
    if value <= 0:
        return "0%"
    if value < 0.25:
        return "1-24%"
    if value < 0.50:
        return "25-49%"
    if value < 0.75:
        return "50-74%"
    if value < 1:
        return "75-99%"
    return "100%"


def fetch_latest_ledger(conn, *, ledger_run_id: int | None) -> dict[str, Any]:
    if ledger_run_id is None:
        ledger_run_id = latest_id(conn, "ml_issue_ledger_runs", "finished_at IS NOT NULL")
    if ledger_run_id is None:
        raise RuntimeError("No finished issue ledger run found.")
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_ledger_runs
        WHERE id = ?
        """,
        (ledger_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Issue ledger run not found: {ledger_run_id}")
    return dict(row)


def fetch_ledger_items(conn, *, ledger_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.*,
            COALESCE(state.is_closed, 0) AS state_is_closed
        FROM ml_issue_ledger_items item
        LEFT JOIN segment_state_items state ON state.id = item.state_item_id
        WHERE item.run_id = ?
        ORDER BY item.segment_id, item.issue_family, item.issue_kind, item.id
        """,
        (ledger_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_reviewed_decisions(conn, *, ledger_run_id: int) -> dict[int, list[dict[str, Any]]]:
    if not table_exists(conn, "ml_issue_review_decisions"):
        return {}

    current_items = conn.execute(
        """
        SELECT
            id,
            segment_id,
            relative_path,
            source_key,
            agent_key,
            issue_family,
            issue_kind
        FROM ml_issue_ledger_items
        WHERE run_id = ?
        """,
        (ledger_run_id,),
    ).fetchall()
    stable_item_ids: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    current_item_ids: set[int] = set()
    for item in current_items:
        item_id = int(item["id"])
        current_item_ids.add(item_id)
        stable_item_ids[
            (
                int(item["segment_id"]),
                str(item["relative_path"] or ""),
                str(item["source_key"] or ""),
                str(item["agent_key"] or ""),
                str(item["issue_family"] or ""),
                str(item["issue_kind"] or ""),
            )
        ].append(item_id)

    rows = conn.execute(
        """
        SELECT
            id,
            ledger_run_id,
            ledger_item_id,
            segment_id,
            relative_path,
            source_key,
            normalized_decision,
            evidence_label,
            agent_key,
            issue_family,
            issue_kind,
            created_at
        FROM ml_issue_review_decisions
        WHERE valid = 1
          AND validation_status = 'accepted'
        ORDER BY ledger_run_id DESC, ledger_item_id, id
        """
    ).fetchall()
    reviewed: dict[int, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[int, int]] = set()
    for row in rows:
        decision = dict(row)
        decision_id = int(row["id"])
        direct_item_id = int(row["ledger_item_id"])
        mapped_ids: list[int] = []
        if direct_item_id in current_item_ids:
            mapped_ids.append(direct_item_id)
        else:
            stable_key = (
                int(row["segment_id"]),
                str(row["relative_path"] or ""),
                str(row["source_key"] or ""),
                str(row["agent_key"] or ""),
                str(row["issue_family"] or ""),
                str(row["issue_kind"] or ""),
            )
            mapped_ids.extend(stable_item_ids.get(stable_key, []))

        for item_id in mapped_ids:
            marker = (item_id, decision_id)
            if marker in seen:
                continue
            seen.add(marker)
            reviewed[item_id].append(decision)
    return reviewed


def fetch_exact_policy_evidence(
    conn,
    *,
    ledger_run_id: int,
) -> tuple[dict[int, list[dict[str, Any]]], dict[int, list[dict[str, Any]]]]:
    specs = [
        {
            "source": "short_label_release_checkpoint",
            "table": "ml_issue_short_label_release_checkpoint_items",
            "run_table": "ml_issue_short_label_release_checkpoint_runs",
            "run_join": "item.checkpoint_run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.queue_bucket",
        },
        {
            "source": "short_label_route_checkpoint",
            "table": "ml_issue_short_label_route_checkpoint_items",
            "run_table": "ml_issue_short_label_route_checkpoint_runs",
            "run_join": "item.checkpoint_run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "run.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
            "item_filter": "item.checkpoint_allowed = 1",
        },
        {
            "source": "short_label_pure_no_token_checkpoint",
            "table": "ml_issue_short_label_pure_no_token_checkpoint_items",
            "run_table": "ml_issue_short_label_pure_no_token_checkpoint_runs",
            "run_join": "item.checkpoint_run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "'pure_no_token_nominal'",
            "item_filter": "item.checkpoint_allowed = 1",
        },
        {
            "source": "short_label_bold_no_repair_checkpoint",
            "table": "ml_issue_short_label_bold_no_repair_checkpoint_items",
            "run_table": "ml_issue_short_label_bold_no_repair_checkpoint_runs",
            "run_join": "item.checkpoint_run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
            "item_filter": (
                "item.checkpoint_allowed = 1 "
                "AND run.id = ("
                "SELECT MAX(latest.id) "
                "FROM ml_issue_short_label_bold_no_repair_checkpoint_runs latest "
                "WHERE latest.finished_at IS NOT NULL"
                ")"
            ),
        },
        {
            "source": "short_label_context_lane_checkpoint",
            "table": "ml_issue_short_label_context_lane_checkpoint_items",
            "run_table": "ml_issue_short_label_context_lane_checkpoint_runs",
            "run_join": "item.checkpoint_run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "'micro_short_label_style'",
            "subpolicy_expr": "item.subpolicy_name",
            "item_filter": "item.checkpoint_allowed = 1",
        },
        {
            "source": "short_label_dynamic_delegate_checkpoint",
            "table": "ml_issue_short_label_dynamic_delegate_checkpoint_items",
            "run_table": "ml_issue_short_label_dynamic_delegate_checkpoint_runs",
            "run_join": "item.checkpoint_run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "'micro_short_label_style'",
            "subpolicy_expr": "item.subpolicy_name",
            "item_filter": "item.checkpoint_allowed = 1",
        },
        {
            "source": "short_label_compact_ui_false_reopen_checkpoint",
            "table": "ml_issue_short_label_compact_ui_false_reopen_checkpoint_items",
            "run_table": "ml_issue_short_label_compact_ui_false_reopen_checkpoint_runs",
            "run_join": "item.checkpoint_run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
            "item_filter": "item.checkpoint_allowed = 1",
        },
        {
            "source": "short_label_compact_ui_semantic_reviewed_checkpoint",
            "table": "ml_issue_short_label_compact_ui_semantic_reviewed_checkpoint_items",
            "run_table": "ml_issue_short_label_compact_ui_semantic_reviewed_checkpoint_runs",
            "run_join": "item.checkpoint_run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
            "item_filter": "item.checkpoint_allowed = 1",
        },
        {
            "source": "custom_localization_flower_lexical_repair_checkpoint",
            "table": "ml_issue_custom_localization_lexical_domain_checkpoint_items",
            "run_table": "ml_issue_custom_localization_lexical_domain_checkpoint_runs",
            "run_join": "item.checkpoint_run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "run.agent_key",
            "subpolicy_expr": "item.category",
            "item_filter": (
                "item.checkpoint_allowed = 1 "
                "AND item.ledger_item_id IS NOT NULL "
                "AND run.id = ("
                "SELECT MAX(latest.id) "
                "FROM ml_issue_custom_localization_lexical_domain_checkpoint_runs latest "
                "WHERE latest.finished_at IS NOT NULL"
                ")"
            ),
        },
        {
            "source": "religion_semantic_pattern_checkpoint",
            "table": "ml_issue_religion_semantic_pattern_checkpoint_items",
            "run_table": "ml_issue_religion_semantic_pattern_checkpoint_runs",
            "run_join": "item.checkpoint_run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "run.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
            "item_filter": (
                "item.checkpoint_allowed = 1 "
                "AND run.id = ("
                "SELECT MAX(latest.id) "
                "FROM ml_issue_dynamic_concept_expression_guarded_checkpoint_runs latest "
                "WHERE latest.finished_at IS NOT NULL"
                ")"
            ),
        },
        {
            "source": "culture_semantic_pattern_checkpoint",
            "table": "ml_issue_culture_semantic_pattern_checkpoint_items",
            "run_table": "ml_issue_culture_semantic_pattern_checkpoint_runs",
            "run_join": "item.checkpoint_run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "run.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
            "item_filter": "item.checkpoint_allowed = 1",
        },
        {
            "source": "dynamic_ck3_pattern_checkpoint",
            "table": "ml_issue_dynamic_ck3_pattern_checkpoint_items",
            "run_table": "ml_issue_dynamic_ck3_pattern_checkpoint_runs",
            "run_join": "item.checkpoint_run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "run.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
            "item_filter": "item.checkpoint_allowed = 1",
        },
        {
            "source": "dynamic_concept_expression_guarded_checkpoint",
            "table": "ml_issue_dynamic_concept_expression_guarded_checkpoint_items",
            "run_table": "ml_issue_dynamic_concept_expression_guarded_checkpoint_runs",
            "run_join": "item.checkpoint_run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "run.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
            "item_filter": (
                "item.checkpoint_allowed = 1 "
                "AND run.id = ("
                "SELECT MAX(latest.id) "
                "FROM ml_issue_dynamic_concept_expression_guarded_checkpoint_runs latest "
                "WHERE latest.finished_at IS NOT NULL"
                ")"
            ),
        },
        {
            "source": "gender_boundary_lifecycle",
            "table": "ml_issue_gender_boundary_lifecycle_items",
            "run_table": "ml_issue_gender_boundary_lifecycle_runs",
            "run_join": "item.run_id = run.id",
            "allowed_column": "item.policy_allowed",
            "action_column": "item.policy_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
        },
        {
            "source": "gender_positive_shadow",
            "table": "ml_issue_gender_subpolicy_shadow_items",
            "run_table": "ml_issue_gender_subpolicy_shadow_runs",
            "run_join": "item.run_id = run.id",
            "allowed_column": "1",
            "action_column": "item.shadow_action",
            "agent_expr": "run.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
            "item_filter": (
                "item.shadow_status = 'shadow_ready_positive' "
                "AND item.shadow_action = 'would_release_false_reopen_shadow'"
            ),
        },
        {
            "source": "trigger_gender_role_lifecycle",
            "table": "ml_issue_trigger_gender_role_lifecycle_items",
            "run_table": "ml_issue_trigger_gender_role_lifecycle_runs",
            "run_join": "item.run_id = run.id",
            "allowed_column": "item.policy_allowed",
            "action_column": "item.policy_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
        },
        {
            "source": "dynamic_literal_repair_checkpoint",
            "table": "ml_issue_dynamic_literal_repair_checkpoint_items",
            "run_table": "ml_issue_dynamic_literal_repair_checkpoint_runs",
            "run_join": "item.checkpoint_run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
        },
        {
            "source": "spanish_residual_repair_checkpoint",
            "table": "ml_issue_spanish_residual_repair_checkpoint_items",
            "run_table": "ml_issue_spanish_residual_repair_checkpoint_runs",
            "run_join": "item.checkpoint_run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
            "item_filter": (
                "run.id = ("
                "SELECT MAX(latest_run.id) "
                "FROM ml_issue_spanish_residual_repair_checkpoint_items latest_item "
                "JOIN ml_issue_spanish_residual_repair_checkpoint_runs latest_run "
                "  ON latest_item.checkpoint_run_id = latest_run.id "
                "WHERE latest_run.finished_at IS NOT NULL "
                "  AND latest_item.ledger_item_id = item.ledger_item_id"
                ")"
            ),
        },
        {
            "source": "surface_boundary_checkpoint",
            "table": "ml_issue_surface_boundary_checkpoint_items",
            "run_table": "ml_issue_surface_boundary_checkpoint_runs",
            "run_join": "item.run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
        },
        {
            "source": "visible_ui_label_repair_checkpoint",
            "table": "ml_issue_visible_ui_label_repair_checkpoint_items",
            "run_table": "ml_issue_visible_ui_label_repair_checkpoint_runs",
            "run_join": "item.run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
        },
        {
            "source": "specialist_blocker_repair_checkpoint",
            "table": "ml_issue_specialist_blocker_repair_checkpoint_items",
            "run_table": "ml_issue_specialist_blocker_repair_checkpoint_runs",
            "run_join": "item.run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
        },
        {
            "source": "event_short_phrase_checkpoint",
            "table": "ml_issue_event_short_phrase_checkpoint_items",
            "run_table": "ml_issue_event_short_phrase_checkpoint_runs",
            "run_join": "item.run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
            "item_filter": (
                "run.id = ("
                "SELECT MAX(latest.id) "
                "FROM ml_issue_event_short_phrase_checkpoint_runs latest "
                "WHERE latest.finished_at IS NOT NULL"
                ")"
            ),
        },
        {
            "source": "event_surface_decision_checkpoint",
            "table": "ml_issue_event_surface_decision_checkpoint_items",
            "run_table": "ml_issue_event_surface_decision_checkpoint_runs",
            "run_join": "item.run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
            "item_filter": (
                "run.id = ("
                "SELECT MAX(latest.id) "
                "FROM ml_issue_event_surface_decision_checkpoint_runs latest "
                "WHERE latest.finished_at IS NOT NULL"
                ")"
            ),
        },
        {
            "source": "event_surface_repair_checkpoint",
            "table": "ml_issue_event_surface_repair_checkpoint_items",
            "run_table": "ml_issue_event_surface_repair_checkpoint_runs",
            "run_join": "item.checkpoint_run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
            "item_filter": (
                "item.checkpoint_allowed = 1 "
                "AND run.id = ("
                "SELECT MAX(latest.id) "
                "FROM ml_issue_event_surface_repair_checkpoint_runs latest "
                "WHERE latest.finished_at IS NOT NULL"
                ")"
            ),
        },
        {
            "source": "event_surface_reviewed_item_checkpoint",
            "table": "ml_issue_event_surface_reviewed_item_checkpoint_items",
            "run_table": "ml_issue_event_surface_reviewed_item_checkpoint_runs",
            "run_join": "item.checkpoint_run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
            "item_filter": (
                "item.checkpoint_allowed = 1 "
                "AND run.id = ("
                "SELECT MAX(latest.id) "
                "FROM ml_issue_event_surface_reviewed_item_checkpoint_runs latest "
                "WHERE latest.finished_at IS NOT NULL"
                ")"
            ),
        },
        {
            "source": "event_surface_pair_completion_checkpoint",
            "table": "ml_issue_event_surface_pair_completion_checkpoint_items",
            "run_table": "ml_issue_event_surface_pair_completion_checkpoint_runs",
            "run_join": "item.run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
            "item_filter": (
                "item.checkpoint_allowed = 1 "
                "AND run.id = ("
                "SELECT MAX(latest.id) "
                "FROM ml_issue_event_surface_pair_completion_checkpoint_runs latest "
                "WHERE latest.finished_at IS NOT NULL"
                ")"
            ),
        },
        {
            "source": "event_dialogue_option_boundary_checkpoint",
            "table": "ml_issue_event_dialogue_option_boundary_checkpoint_items",
            "run_table": "ml_issue_event_dialogue_option_boundary_checkpoint_runs",
            "run_join": "item.run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "'micro_event_dialogue_option'",
            "subpolicy_expr": "item.queue_bucket",
            "item_filter": "item.checkpoint_allowed = 1",
        },
        {
            "source": "event_context_sentence_boundary_checkpoint",
            "table": "ml_issue_event_context_sentence_boundary_checkpoint_items",
            "run_table": "ml_issue_event_context_sentence_boundary_checkpoint_runs",
            "run_join": "item.run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "'micro_event_context_composer'",
            "subpolicy_expr": "item.queue_bucket",
            "item_filter": "item.checkpoint_allowed = 1",
        },
        {
            "source": "dynamic_token_literal_repair_checkpoint",
            "table": "ml_issue_dynamic_token_literal_repair_checkpoint_items",
            "run_table": "ml_issue_dynamic_token_literal_repair_checkpoint_runs",
            "run_join": "item.run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
        },
        {
            "source": "decision_pattern_checkpoint",
            "table": "ml_issue_decision_pattern_checkpoint_items",
            "run_table": "ml_issue_decision_pattern_checkpoint_runs",
            "run_join": "item.run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
        },
        {
            "source": "nickname_select_residual_checkpoint",
            "table": "ml_issue_nickname_select_residual_checkpoint_items",
            "run_table": "ml_issue_nickname_select_residual_checkpoint_runs",
            "run_join": "item.run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
        },
        {
            "source": "nickname_context_composition_checkpoint",
            "table": "ml_issue_nickname_context_composition_checkpoint_items",
            "run_table": "ml_issue_nickname_context_composition_checkpoint_runs",
            "run_join": "item.run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
        },
        {
            "source": "nickname_demonym_gender_checkpoint",
            "table": "ml_issue_nickname_demonym_gender_checkpoint_items",
            "run_table": "ml_issue_nickname_demonym_gender_checkpoint_runs",
            "run_join": "item.run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
        },
        {
            "source": "semantic_composition_checkpoint",
            "table": "ml_issue_semantic_composition_checkpoint_items",
            "run_table": "ml_issue_semantic_composition_checkpoint_runs",
            "run_join": "item.run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
        },
        {
            "source": "autofix_unknown_surface_checkpoint",
            "table": "ml_issue_autofix_unknown_surface_checkpoint_items",
            "run_table": "ml_issue_autofix_unknown_surface_checkpoint_runs",
            "run_join": "item.run_id = run.id",
            "allowed_column": "CASE WHEN item.checkpoint_status = 'allowed' THEN 1 ELSE 0 END",
            "action_column": "'stage_autofix_unknown_surface_checkpoint'",
            "agent_expr": "'micro_autofix_unknown_router'",
            "subpolicy_expr": "item.cluster",
            "item_filter": "item.checkpoint_status = 'allowed'",
        },
        {
            "source": "semantic_short_label_pair_checkpoint",
            "table": "ml_issue_semantic_short_label_pair_checkpoint_items",
            "run_table": "ml_issue_semantic_short_label_pair_checkpoint_runs",
            "run_join": "item.checkpoint_run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
            "item_filter": "item.checkpoint_allowed = 1",
        },
        {
            "source": "full_coverage_trust_checkpoint",
            "table": "ml_issue_full_coverage_trust_checkpoint_items",
            "run_table": "ml_issue_full_coverage_trust_checkpoint_runs",
            "run_join": "item.checkpoint_run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
            "item_filter": "item.checkpoint_allowed = 1",
        },
        {
            "source": "structural_boundary_checkpoint",
            "table": "ml_issue_structural_boundary_checkpoint_items",
            "run_table": "ml_issue_structural_boundary_checkpoint_runs",
            "run_join": "item.run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
        },
        {
            "source": "high_issue_explained_checkpoint",
            "table": "ml_issue_high_issue_explained_items",
            "run_table": "ml_issue_high_issue_explained_runs",
            "run_join": "item.run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
        },
        {
            "source": "semantic_review_explained_checkpoint",
            "table": "ml_issue_semantic_review_explained_items",
            "run_table": "ml_issue_semantic_review_explained_runs",
            "run_join": "item.run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
        },
        {
            "source": "semantic_dynamic_explained_checkpoint",
            "table": "ml_issue_semantic_dynamic_explained_items",
            "run_table": "ml_issue_semantic_dynamic_explained_runs",
            "run_join": "item.run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
        },
        {
            "source": "title_policy_explained_checkpoint",
            "table": "ml_issue_title_policy_explained_items",
            "run_table": "ml_issue_title_policy_explained_runs",
            "run_join": "item.run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
            "item_filter": (
                "run.id = ("
                "SELECT MAX(latest.id) "
                "FROM ml_issue_title_policy_explained_runs latest "
                "WHERE latest.finished_at IS NOT NULL"
                ")"
            ),
        },
        {
            "source": "nickname_policy_explained_checkpoint",
            "table": "ml_issue_nickname_policy_explained_items",
            "run_table": "ml_issue_nickname_policy_explained_runs",
            "run_join": "item.run_id = run.id",
            "allowed_column": "item.checkpoint_allowed",
            "action_column": "item.checkpoint_action",
            "agent_expr": "item.agent_key",
            "subpolicy_expr": "item.subpolicy_name",
        },
    ]
    covered: dict[int, list[dict[str, Any]]] = defaultdict(list)
    blocked: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for spec in specs:
        if not table_exists(conn, spec["table"]) or not table_exists(conn, spec["run_table"]):
            continue
        item_filter = str(spec.get("item_filter") or "1 = 1")
        queries = [
            (
                "exact",
                f"""
                SELECT
                    ledger.id AS current_ledger_item_id,
                    ledger.segment_id,
                    ledger.run_id AS matched_ledger_run_id,
                    {spec['allowed_column']} AS allowed,
                    COALESCE(item.block_reason, '') AS block_reason,
                    {spec['action_column']} AS action,
                    {spec['agent_expr']} AS agent_key,
                    {spec['subpolicy_expr']} AS subpolicy_name,
                    run.id AS source_run_id
                FROM {spec['table']} item
                JOIN {spec['run_table']} run ON {spec['run_join']}
                JOIN ml_issue_ledger_items ledger ON ledger.id = item.ledger_item_id
                WHERE ledger.run_id = ?
                  AND {item_filter}
                """,
                (ledger_run_id,),
            ),
            (
                "carry_forward",
                f"""
                SELECT
                    current_ledger.id AS current_ledger_item_id,
                    current_ledger.segment_id,
                    old_ledger.run_id AS matched_ledger_run_id,
                    {spec['allowed_column']} AS allowed,
                    COALESCE(item.block_reason, '') AS block_reason,
                    {spec['action_column']} AS action,
                    {spec['agent_expr']} AS agent_key,
                    {spec['subpolicy_expr']} AS subpolicy_name,
                    run.id AS source_run_id
                FROM {spec['table']} item
                JOIN {spec['run_table']} run ON {spec['run_join']}
                JOIN ml_issue_ledger_items old_ledger ON old_ledger.id = item.ledger_item_id
                JOIN ml_issue_ledger_items current_ledger
                  ON current_ledger.run_id = ?
                 AND current_ledger.segment_id = old_ledger.segment_id
                 AND current_ledger.issue_family = old_ledger.issue_family
                 AND current_ledger.issue_kind = old_ledger.issue_kind
                WHERE old_ledger.run_id <> ?
                  AND {item_filter}
                """,
                (ledger_run_id, ledger_run_id),
            ),
        ]
        seen: set[tuple[int, str, int, str, str]] = set()
        for match_type, sql, params in queries:
            rows = conn.execute(sql, params).fetchall()
            for row in rows:
                source_run_id = int(row["source_run_id"])
                ledger_item_id = int(row["current_ledger_item_id"])
                action = row["action"] or ""
                subpolicy = row["subpolicy_name"] or ""
                dedupe_key = (ledger_item_id, spec["source"], source_run_id, action, match_type)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                evidence = {
                    "source": f"{spec['source']}:{match_type}",
                    "source_family": spec["source"],
                    "match_type": match_type,
                    "source_run_id": source_run_id,
                    "matched_ledger_run_id": int(row["matched_ledger_run_id"] or 0),
                    "agent_key": row["agent_key"] or "unknown_agent",
                    "subpolicy_name": subpolicy,
                    "action": action,
                    "block_reason": row["block_reason"] or "",
                }
                if int(row["allowed"] or 0) == 1:
                    covered[ledger_item_id].append(evidence)
                else:
                    blocked[ledger_item_id].append(evidence)
    return covered, blocked


def build_segment_rows(
    *,
    ledger: dict[str, Any],
    ledger_items: list[dict[str, Any]],
    covered_evidence: dict[int, list[dict[str, Any]]],
    blocked_evidence: dict[int, list[dict[str, Any]]],
    reviewed_decisions: dict[int, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_segment: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in ledger_items:
        by_segment[int(item["segment_id"])].append(item)

    family_totals: Counter[str] = Counter()
    family_covered: Counter[str] = Counter()
    family_reviewed: Counter[str] = Counter()
    family_blocked: Counter[str] = Counter()
    family_open: Counter[str] = Counter()
    agent_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    decision_counts: Counter[str] = Counter()
    bucket_counts: Counter[str] = Counter()
    coverage_state_counts: Counter[str] = Counter()
    review_state_counts: Counter[str] = Counter()

    total_issues = 0
    covered_issues = 0
    reviewed_issues = 0
    blocked_issues = 0
    open_issues = 0
    closed_segments = 0
    multi_issue_segments = 0
    partial_multi_issue_segments = 0
    weighted_ratio_sum = 0.0
    rows: list[dict[str, Any]] = []

    for segment_id, items in by_segment.items():
        first = items[0]
        total_count = len(items)
        total_issues += total_count
        if total_count > 1:
            multi_issue_segments += 1
        segment_closed = int(first.get("state_is_closed") or 0)
        if segment_closed:
            closed_segments += 1

        issue_families = Counter(str(item["issue_family"]) for item in items)
        covered_families: Counter[str] = Counter()
        open_families: Counter[str] = Counter()
        covered_agents: Counter[str] = Counter()
        coverage_sources: Counter[str] = Counter()
        reviewed_decisions_by_label: Counter[str] = Counter()

        segment_covered = 0
        segment_reviewed = 0
        segment_blocked = 0
        segment_open = 0

        for item in items:
            item_id = int(item["id"])
            family = str(item["issue_family"])
            family_totals[family] += 1

            item_covered = bool(covered_evidence.get(item_id))
            item_reviewed = bool(reviewed_decisions.get(item_id))
            item_blocked = (
                str(item.get("status") or "") == "blocked"
                or str(item.get("route_status") or "") == "guard_block"
                or (bool(blocked_evidence.get(item_id)) and not item_covered)
            )
            if item_covered:
                segment_covered += 1
                family_covered[family] += 1
                covered_families[family] += 1
                item_agents_seen: set[str] = set()
                item_sources_seen: set[str] = set()
                for evidence in covered_evidence[item_id]:
                    agent = evidence.get("agent_key") or "unknown_agent"
                    source = evidence.get("source") or "unknown_source"
                    if agent not in item_agents_seen:
                        agent_counts[agent] += 1
                        covered_agents[agent] += 1
                        item_agents_seen.add(agent)
                    if source not in item_sources_seen:
                        source_counts[source] += 1
                        coverage_sources[source] += 1
                        item_sources_seen.add(source)
            if item_reviewed:
                segment_reviewed += 1
                family_reviewed[family] += 1
                for decision in reviewed_decisions[item_id]:
                    label = str(decision.get("normalized_decision") or "unknown_decision")
                    decision_counts[label] += 1
                    reviewed_decisions_by_label[label] += 1
            if item_blocked:
                segment_blocked += 1
                family_blocked[family] += 1
            if not item_covered and not item_blocked:
                segment_open += 1
                family_open[family] += 1
                open_families[family] += 1

        covered_issues += segment_covered
        reviewed_issues += segment_reviewed
        blocked_issues += segment_blocked
        open_issues += segment_open
        coverage_ratio = ratio(segment_covered, total_count)
        weighted_ratio_sum += coverage_ratio
        bucket = coverage_bucket(coverage_ratio)
        bucket_counts[bucket] += 1
        if segment_covered == total_count:
            coverage_state = "full"
        elif segment_covered > 0:
            coverage_state = "partial"
            if total_count > 1:
                partial_multi_issue_segments += 1
        else:
            coverage_state = "none"
        if segment_reviewed == total_count:
            review_state = "review_full"
        elif segment_reviewed > 0:
            review_state = "review_partial"
        else:
            review_state = "review_none"
        coverage_state_counts[coverage_state] += 1
        review_state_counts[review_state] += 1

        rows.append(
            {
                "ledger_run_id": int(ledger["id"]),
                "segment_state_run_id": int(ledger["segment_state_run_id"]),
                "segment_id": segment_id,
                "relative_path": first["relative_path"],
                "source_key": first["source_key"],
                "source_line_number": first["source_line_number"],
                "final_state": first.get("final_state") or "",
                "state_group": first.get("state_group") or "",
                "is_closed": segment_closed,
                "total_issue_count": total_count,
                "covered_issue_count": segment_covered,
                "reviewed_issue_count": segment_reviewed,
                "blocked_issue_count": segment_blocked,
                "open_issue_count": segment_open,
                "coverage_ratio": coverage_ratio,
                "coverage_state": coverage_state,
                "review_state": review_state,
                "issue_families_json": json.dumps(dict(issue_families), ensure_ascii=False, sort_keys=True),
                "covered_families_json": json.dumps(dict(covered_families), ensure_ascii=False, sort_keys=True),
                "open_families_json": json.dumps(dict(open_families), ensure_ascii=False, sort_keys=True),
                "covered_agents_json": json.dumps(dict(covered_agents), ensure_ascii=False, sort_keys=True),
                "coverage_sources_json": json.dumps(dict(coverage_sources), ensure_ascii=False, sort_keys=True),
                "reviewed_decisions_json": json.dumps(
                    dict(reviewed_decisions_by_label),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "evidence_json": json.dumps(
                    {
                        "coverage_bucket": bucket,
                        "issue_family_count": len(issue_families),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            }
        )

    segment_count = len(rows)
    full_segments = coverage_state_counts["full"]
    partial_segments = coverage_state_counts["partial"]
    uncovered_segments = coverage_state_counts["none"]
    summary = {
        "total_segments_with_issues": segment_count,
        "total_issue_items": total_issues,
        "covered_issue_items": covered_issues,
        "reviewed_issue_items": reviewed_issues,
        "blocked_issue_items": blocked_issues,
        "open_issue_items": open_issues,
        "fully_covered_segments": full_segments,
        "partially_covered_segments": partial_segments,
        "uncovered_segments": uncovered_segments,
        "reviewed_segments": review_state_counts["review_full"] + review_state_counts["review_partial"],
        "closed_segments": closed_segments,
        "multi_issue_segments": multi_issue_segments,
        "partially_covered_multi_issue_segments": partial_multi_issue_segments,
        "avg_coverage_ratio": ratio(weighted_ratio_sum, segment_count),
        "weighted_coverage_ratio": ratio(covered_issues, total_issues),
        "bucket_counts": dict(bucket_counts),
        "coverage_state_counts": dict(coverage_state_counts),
        "review_state_counts": dict(review_state_counts),
        "family_counts": {
            family: {
                "total": family_totals[family],
                "covered": family_covered[family],
                "reviewed": family_reviewed[family],
                "blocked": family_blocked[family],
                "open": family_open[family],
                "coverage_ratio": ratio(family_covered[family], family_totals[family]),
            }
            for family in sorted(family_totals, key=lambda key: (-family_totals[key], key))
        },
        "agent_counts": dict(agent_counts),
        "source_counts": dict(source_counts),
        "decision_counts": dict(decision_counts),
    }
    return rows, summary


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    json_path: Path,
    run_id: int,
    ledger: dict[str, Any],
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
    started_at: str,
) -> None:
    fieldnames = [
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "coverage_state",
        "review_state",
        "total_issue_count",
        "covered_issue_count",
        "reviewed_issue_count",
        "blocked_issue_count",
        "open_issue_count",
        "coverage_ratio",
        "issue_families_json",
        "covered_families_json",
        "open_families_json",
        "covered_agents_json",
        "coverage_sources_json",
        "reviewed_decisions_json",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in sorted(
            rows,
            key=lambda item: (
                item["coverage_state"] != "partial",
                -int(item["open_issue_count"]),
                -int(item["total_issue_count"]),
                item["relative_path"],
                item["source_line_number"] or 0,
                item["source_key"],
            ),
        ):
            writer.writerow({field: row.get(field) for field in fieldnames})

    partial_samples = [
        row
        for row in sorted(
            rows,
            key=lambda item: (
                item["coverage_state"] != "partial",
                -int(item["open_issue_count"]),
                -int(item["total_issue_count"]),
                item["relative_path"],
                item["source_line_number"] or 0,
                item["source_key"],
            ),
        )
        if row["coverage_state"] == "partial"
    ][:30]
    uncovered_samples = [
        row
        for row in sorted(
            rows,
            key=lambda item: (
                item["coverage_state"] != "none",
                -int(item["total_issue_count"]),
                item["relative_path"],
                item["source_line_number"] or 0,
                item["source_key"],
            ),
        )
        if row["coverage_state"] == "none"
    ][:30]
    full_samples = [
        row
        for row in sorted(
            rows,
            key=lambda item: (
                item["coverage_state"] != "full",
                -int(item["total_issue_count"]),
                item["relative_path"],
                item["source_line_number"] or 0,
                item["source_key"],
            ),
        )
        if row["coverage_state"] == "full"
    ][:20]
    family_counts = summary["family_counts"]
    top_open_families = sorted(
        family_counts.items(),
        key=lambda item: (-int(item[1]["open"]), item[0]),
    )[:12]
    top_covered_families = sorted(
        family_counts.items(),
        key=lambda item: (-int(item[1]["covered"]), item[0]),
    )[:12]

    lines = [
        "Issue partial coverage report",
        f"Rule version: {RULE_VERSION}",
        f"Coverage run id: {run_id}",
        f"Ledger run id: {ledger['id']}",
        f"Segment-state run id: {ledger['segment_state_run_id']}",
        f"Started at: {started_at}",
        "",
        "Core metrics:",
        f"- Segments with issues: {summary['total_segments_with_issues']:,}",
        f"- Issue items: {summary['total_issue_items']:,}",
        f"- Covered issue items: {summary['covered_issue_items']:,} ({summary['weighted_coverage_ratio']:.2%})",
        f"- Reviewed issue items: {summary['reviewed_issue_items']:,}",
        f"- Blocked issue items: {summary['blocked_issue_items']:,}",
        f"- Open issue items: {summary['open_issue_items']:,}",
        f"- Fully covered segments: {summary['fully_covered_segments']:,}",
        f"- Partially covered segments: {summary['partially_covered_segments']:,}",
        f"- Uncovered segments: {summary['uncovered_segments']:,}",
        f"- Multi-issue segments: {summary['multi_issue_segments']:,}",
        f"- Partially covered multi-issue segments: {summary['partially_covered_multi_issue_segments']:,}",
        f"- Average segment coverage ratio: {summary['avg_coverage_ratio']:.2%}",
        "",
        "Coverage buckets:",
        *[
            f"- {bucket}: {int(count):,} segments"
            for bucket, count in sorted(summary["bucket_counts"].items())
        ],
        "",
        "Coverage sources:",
        *[
            f"- {source}: {int(count):,} covered issue items"
            for source, count in Counter(summary["source_counts"]).most_common(12)
        ],
        "",
        "Covered agents:",
        *[
            f"- {agent}: {int(count):,} covered issue items"
            for agent, count in Counter(summary["agent_counts"]).most_common(12)
        ],
        "",
        "Top open families:",
        *[
            (
                f"- {family}: open={int(values['open']):,}, covered={int(values['covered']):,}, "
                f"reviewed={int(values['reviewed']):,}, total={int(values['total']):,}, "
                f"coverage={float(values['coverage_ratio']):.2%}"
            )
            for family, values in top_open_families
        ],
        "",
        "Top covered families:",
        *[
            (
                f"- {family}: covered={int(values['covered']):,}, open={int(values['open']):,}, "
                f"total={int(values['total']):,}, coverage={float(values['coverage_ratio']):.2%}"
            )
            for family, values in top_covered_families
        ],
        "",
        "Partial segment samples:",
    ]
    if partial_samples:
        for row in partial_samples:
            lines.append(
                (
                    f"- {row['relative_path']}:{row['source_line_number']}:{row['source_key']} "
                    f"coverage={int(row['covered_issue_count'])}/{int(row['total_issue_count'])}, "
                    f"open={int(row['open_issue_count'])}, families={row['issue_families_json']}"
                )
            )
    else:
        lines.append("- none")
    lines.extend(["", "Fully covered samples:"])
    if full_samples:
        for row in full_samples:
            lines.append(
                (
                    f"- {row['relative_path']}:{row['source_line_number']}:{row['source_key']} "
                    f"coverage={int(row['covered_issue_count'])}/{int(row['total_issue_count'])}, "
                    f"sources={row['coverage_sources_json']}"
                )
            )
    else:
        lines.append("- none")
    lines.extend(["", "Uncovered high-issue samples:"])
    if uncovered_samples:
        for row in uncovered_samples:
            lines.append(
                (
                    f"- {row['relative_path']}:{row['source_line_number']}:{row['source_key']} "
                    f"issues={int(row['total_issue_count'])}, families={row['issue_families_json']}"
                )
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Interpretation:",
            "- Covered means a current ledger item is matched to policy/checkpoint/lifecycle evidence by exact id or stable issue signature.",
            "- Carry-forward coverage reuses evidence across ledger regenerations only when segment_id, issue_family and issue_kind still match.",
            "- Reviewed means a human decision exists, but the issue is not counted as covered until a microagent/policy captures it.",
            "- Full segment coverage is not the same as production closure; it means every known issue on the segment has network coverage evidence.",
            "",
            "Safety note:",
            "- Diagnostic only: no source/output reads, no confirmation updates, no model promotion and no output writes.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    json_payload = {
        "rule_version": RULE_VERSION,
        "coverage_run_id": run_id,
        "ledger": ledger,
        "summary": summary,
        "partial_samples": partial_samples,
        "full_samples": full_samples,
        "uncovered_samples": uncovered_samples,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
    }
    json_path.write_text(json.dumps(json_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(*, ledger_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = db.utc_now()
    txt_path, csv_path, json_path = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ledger = fetch_latest_ledger(conn, ledger_run_id=ledger_run_id)
        ledger_items = fetch_ledger_items(conn, ledger_run_id=int(ledger["id"]))
        covered_evidence, blocked_evidence = fetch_exact_policy_evidence(conn, ledger_run_id=int(ledger["id"]))
        reviewed_decisions = fetch_reviewed_decisions(conn, ledger_run_id=int(ledger["id"]))
        rows, summary = build_segment_rows(
            ledger=ledger,
            ledger_items=ledger_items,
            covered_evidence=covered_evidence,
            blocked_evidence=blocked_evidence,
            reviewed_decisions=reviewed_decisions,
        )
        notes = {
            "coverage_definition": "exact ledger_item_id evidence plus carry-forward by segment_id, issue_family and issue_kind",
            "covered_sources": sorted(summary["source_counts"]),
            "review_definition": "accepted human issue-review decisions",
        }
        now = db.utc_now()
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_partial_coverage_runs (
                rule_version,
                ledger_run_id,
                segment_state_run_id,
                total_segments_with_issues,
                total_issue_items,
                covered_issue_items,
                reviewed_issue_items,
                blocked_issue_items,
                open_issue_items,
                fully_covered_segments,
                partially_covered_segments,
                uncovered_segments,
                reviewed_segments,
                closed_segments,
                multi_issue_segments,
                partially_covered_multi_issue_segments,
                avg_coverage_ratio,
                weighted_coverage_ratio,
                bucket_counts_json,
                family_counts_json,
                agent_counts_json,
                source_counts_json,
                notes_json,
                report_path,
                csv_path,
                json_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                int(ledger["id"]),
                int(ledger["segment_state_run_id"]),
                int(summary["total_segments_with_issues"]),
                int(summary["total_issue_items"]),
                int(summary["covered_issue_items"]),
                int(summary["reviewed_issue_items"]),
                int(summary["blocked_issue_items"]),
                int(summary["open_issue_items"]),
                int(summary["fully_covered_segments"]),
                int(summary["partially_covered_segments"]),
                int(summary["uncovered_segments"]),
                int(summary["reviewed_segments"]),
                int(summary["closed_segments"]),
                int(summary["multi_issue_segments"]),
                int(summary["partially_covered_multi_issue_segments"]),
                float(summary["avg_coverage_ratio"]),
                float(summary["weighted_coverage_ratio"]),
                json.dumps(summary["bucket_counts"], ensure_ascii=False, sort_keys=True),
                json.dumps(summary["family_counts"], ensure_ascii=False, sort_keys=True),
                json.dumps(summary["agent_counts"], ensure_ascii=False, sort_keys=True),
                json.dumps(summary["source_counts"], ensure_ascii=False, sort_keys=True),
                json.dumps(notes, ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(json_path),
                started_at,
                now,
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO ml_issue_partial_coverage_items (
                run_id,
                ledger_run_id,
                segment_state_run_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                final_state,
                state_group,
                is_closed,
                total_issue_count,
                covered_issue_count,
                reviewed_issue_count,
                blocked_issue_count,
                open_issue_count,
                coverage_ratio,
                coverage_state,
                review_state,
                issue_families_json,
                covered_families_json,
                open_families_json,
                covered_agents_json,
                coverage_sources_json,
                reviewed_decisions_json,
                evidence_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    int(row["ledger_run_id"]),
                    int(row["segment_state_run_id"]),
                    int(row["segment_id"]),
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["final_state"],
                    row["state_group"],
                    int(row["is_closed"]),
                    int(row["total_issue_count"]),
                    int(row["covered_issue_count"]),
                    int(row["reviewed_issue_count"]),
                    int(row["blocked_issue_count"]),
                    int(row["open_issue_count"]),
                    float(row["coverage_ratio"]),
                    row["coverage_state"],
                    row["review_state"],
                    row["issue_families_json"],
                    row["covered_families_json"],
                    row["open_families_json"],
                    row["covered_agents_json"],
                    row["coverage_sources_json"],
                    row["reviewed_decisions_json"],
                    row["evidence_json"],
                    now,
                )
                for row in rows
            ],
        )
        conn.commit()

    summary["run_id"] = run_id
    write_outputs(
        txt_path=txt_path,
        csv_path=csv_path,
        json_path=json_path,
        run_id=run_id,
        ledger=ledger,
        rows=rows,
        summary=summary,
        started_at=started_at,
    )
    print("[issue_partial_coverage_report] Coverage report generated")
    print(f"[issue_partial_coverage_report] Run id: {run_id}")
    print(f"[issue_partial_coverage_report] Ledger run id: {ledger['id']}")
    print(f"[issue_partial_coverage_report] Covered issues: {summary['covered_issue_items']:,}/{summary['total_issue_items']:,}")
    print(f"[issue_partial_coverage_report] Partial segments: {summary['partially_covered_segments']:,}")
    print(f"[issue_partial_coverage_report] Report: {txt_path}")
    print(f"[issue_partial_coverage_report] CSV: {csv_path}")
    print(f"[issue_partial_coverage_report] JSON: {json_path}")
    return {
        "run_id": run_id,
        "ledger": ledger,
        "summary": summary,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "json_path": str(json_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Measure partial issue coverage by microagent policies.")
    parser.add_argument("--ledger-run-id", type=int, default=None, help="Issue ledger run id. Default: latest finished.")
    args = parser.parse_args()
    main(ledger_run_id=args.ledger_run_id)
