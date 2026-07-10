from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "memory" / "translation_engine.sqlite"
NEURAL_VISUALIZATION_FILE = ROOT / "docs" / "neurosymbolic_network_visualization.json"
LEARNING_STATUS_FILE = ROOT / "memory" / "learning_status.json"
BASE_DASHBOARD_CACHE_FILE = ROOT / "memory" / "dashboard_cache.json"
POST_RELEASE_FEEDBACK_STATUS_FILE = ROOT / "memory" / "post_release_feedback_status.json"
FEEDBACK_ARCHIVED_STATUSES = {
    "applied",
    "applied_to_candidate",
    "closed",
    "done",
    "included_in_stable_hotfix_candidate",
    "learned",
    "resolved",
    "validated",
    "validated_in_stable_hotfix_candidate",
}
FEEDBACK_ACTIVE_STATUSES = {
    "approved_pending_apply",
    "needs_review",
    "open",
    "pending",
    "ready",
    "ready_to_apply",
}
MANUAL_VISUAL_LOCKS_FILE = ROOT / "memory" / "manual_visual_locks.json"
PRODUCTION_SAFETY_POLICY_FILE = ROOT / "memory" / "production_safety_policy.json"
SAFE_FULL_PRODUCTION_PREFLIGHT_FILE = ROOT / "memory" / "safe_full_production_preflight_latest.json"
PRE_FULL_PRODUCTION_OUTPUT_RESTORE_FILE = ROOT / "memory" / "pre_full_production_output_restore_latest.json"
PRODUCTION_RUN_STATUS_FILE = ROOT / "memory" / "production_run_status.json"
PRODUCTION_SNAPSHOT_ROOT = ROOT / "memory" / "production_snapshots"
PRODUCTION_SNAPSHOT_ARCHIVE_ROOT = ROOT / "memory" / "production_snapshot_archives"
EVALUATION_DECISION_LATEST_FILE = ROOT / "memory" / "evaluation_decision_latest.json"
PRODUCTION_FULL_SQLITE_BACKUP_ENABLED = False
PRODUCTION_RUN_LOCK = threading.Lock()
DASHBOARD_CACHE_LOCK = threading.Lock()
DASHBOARD_CACHE: dict[str, Any] | None = None
ACTIVE_DB_PATH = DEFAULT_DB
TRAINING_LOCK_FILES = (
    ROOT / "memory" / "training_in_progress.flag",
    ROOT / "memory" / "training_lock.json",
    ROOT / "memory" / "production_hold.json",
)

CLIENT_DISCONNECT_WINERRORS = {10053, 10054, 10038}

PUBLICATION_LIFECYCLE_CONFIRMATION_SOURCES = {
    "codex_release_readiness_human_review",
    "local_learning",
    "post_apply_human_correction",
}
PUBLICATION_LIFECYCLE_CONFIRMATION_LABELS = {
    "correct",
    "semantic_error",
    "minor_fix",
    "residual_spanish",
    "structure_error",
    "major_fix",
}
PUBLICATION_LIFECYCLE_CONFIRMATION_LABEL_PREFIXES = (
    "narrative_plain_light_batch",
    "ui_tooltips_packet",
)
PUBLICATION_LIFECYCLE_CONFIRMATION_LABEL_SUFFIXES = ("_corrected",)


def _is_client_disconnect(exc: BaseException) -> bool:
    if isinstance(exc, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)):
        return True
    return getattr(exc, "winerror", None) in CLIENT_DISCONNECT_WINERRORS


def _dict(row: sqlite3.Row | None) -> dict[str, Any]:
    return dict(row) if row else {}


def _one(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
    return _dict(con.execute(sql, params).fetchone())


def _all(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in con.execute(sql, params).fetchall()]


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = _one(
        con,
        """
        SELECT COUNT(*) AS total
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        """,
        (name,),
    )
    return _int(row.get("total")) > 0


def _table_columns(con: sqlite3.Connection, name: str) -> set[str]:
    if not _table_exists(con, name):
        return set()
    return {str(row.get("name")) for row in _all(con, f"PRAGMA table_info({name})")}


def _num(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    return float(value)


def _int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    return int(value)


def _pct(part: Any, total: Any) -> float:
    total_num = _num(total)
    if total_num <= 0:
        return 0.0
    return round((_num(part) / total_num) * 100, 2)


def _latest_segment_state_run_id(con: sqlite3.Connection) -> int:
    if not _table_exists(con, "segment_state_runs"):
        return 0
    row = _one(
        con,
        """
        SELECT id
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL
          AND total_segments > 1000
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
    )
    return _int(row.get("id"))


def _pending_taxonomy_payload(con: sqlite3.Connection, run_id: int | None = None) -> dict[str, Any]:
    if not (_table_exists(con, "segment_state_runs") and _table_exists(con, "segment_state_items")):
        return {
            "run_id": 0,
            "closed_consolidated": 0,
            "actionable_pending": 0,
            "actionable_pending_without_bridge": 0,
            "model_suspicion_watch": 0,
            "governed_bridge_pending": 0,
            "raw_pending": 0,
            "learning_backlog": 0,
            "needs_apply": 0,
            "select_cstring": {"total": 0, "closed": 0, "pending": 0},
            "select_cstring_segment_bridge": {
                "proposal_run_id": 0,
                "candidates": 0,
                "closed": 0,
                "blocked": 0,
                "review_required": 0,
                "production_release_allowed": 0,
            },
            "select_cstring_segment_bridge_proposal_run_id": 0,
            "select_cstring_segment_bridge_candidates": 0,
            "select_cstring_segment_bridge_closed": 0,
            "select_cstring_segment_bridge_blocked": 0,
            "select_cstring_segment_bridge_review_required": 0,
            "select_cstring_segment_bridge_production_release_allowed": 0,
            "short_semantic_tier1_dry_run_id": 0,
            "short_semantic_tier1_dry_run_ids": "",
            "short_semantic_tier1_candidates": 0,
            "short_semantic_tier1_allowed": 0,
            "short_semantic_tier1_blocked": 0,
            "short_semantic_tier1_closed_by_bridge": 0,
            "short_semantic_tier1_blocked_by_bridge": 0,
            "short_semantic_tier1_allowed_rate": 0,
            "short_semantic_tier1_checkpoint_precision_proxy": 0,
            "short_semantic_tier1_status": "unavailable",
            "semantic_short_label_pair_bridge_dry_run_id": 0,
            "semantic_short_label_pair_bridge_checkpoint_id": 0,
            "semantic_short_label_pair_bridge_allowed": 0,
            "semantic_short_label_pair_bridge_closed": 0,
            "semantic_short_label_pair_bridge_blocked": 0,
            "semantic_short_label_pair_bridge_precision_proxy": 0,
            "semantic_short_label_pair_bridge_status": "unavailable",
            "semantic_short_label_pair_guard_profiles": "",
            "semantic_short_label_pair_dry_run_ids": "",
            "semantic_short_label_pair_checkpoint_ids": "",
            "semantic_short_label_pair_allowed_total": 0,
            "semantic_short_label_pair_closed": 0,
            "semantic_short_label_pair_blocked": 0,
            "semantic_short_label_pair_balanced_audit_safe": 0,
            "semantic_short_label_pair_balanced_audit_total": 0,
            "short_label_compact_ui_semantic_companion_bridge_run_ids": "",
            "short_label_compact_ui_semantic_companion_candidates": 0,
            "short_label_compact_ui_semantic_companion_closed": 0,
            "short_label_compact_ui_semantic_companion_blocked": 0,
            "short_label_compact_ui_semantic_companion_status": "unavailable",
            "short_label_semantic_pair_bridge_run_ids": "",
            "short_label_semantic_pair_review_jsonl": "",
            "short_label_semantic_pair_candidates": 0,
            "short_label_semantic_pair_closed": 0,
            "short_label_semantic_pair_blocked": 0,
            "short_label_semantic_pair_closed_quote_fragment": 0,
            "short_label_semantic_pair_closed_nominal_label": 0,
            "short_label_semantic_pair_closed_short_phrase": 0,
            "short_label_semantic_pair_closed_compact_option": 0,
            "short_label_semantic_pair_status": "unavailable",
            "short_label_semantic_load_tips_bridge_run_ids": "",
            "short_label_semantic_load_tips_candidates": 0,
            "short_label_semantic_load_tips_ready": 0,
            "short_label_semantic_load_tips_closed": 0,
            "short_label_semantic_load_tips_blocked": 0,
            "short_label_semantic_load_tips_status": "unavailable",
            "dynamic_ck3_pattern_checkpoint_run_ids": "",
            "dynamic_ck3_pattern_candidates": 0,
            "dynamic_ck3_pattern_allowed": 0,
            "dynamic_ck3_pattern_closed": 0,
            "dynamic_ck3_pattern_blocked": 0,
            "dynamic_ck3_pattern_status": "unavailable",
            "dynamic_ck3_pattern_subpolicies_closed": {},
            "short_label_dynamic_delegate_checkpoint_run_ids": "",
            "short_label_dynamic_delegate_allowed_rows": 0,
            "short_label_dynamic_delegate_distinct_allowed_segments": 0,
            "short_label_dynamic_delegate_already_closed_before": 0,
            "short_label_dynamic_delegate_pending_before": 0,
            "short_label_dynamic_delegate_closed": 0,
            "short_label_dynamic_delegate_blocked": 0,
            "short_label_dynamic_delegate_net_gain": 0,
            "short_label_dynamic_delegate_status": "unavailable",
            "short_label_dynamic_delegate_kind_split": {},
            "short_label_single_issue_system_tooltip_checkpoint_run_ids": "",
            "short_label_single_issue_system_tooltip_allowed_rows": 0,
            "short_label_single_issue_system_tooltip_distinct_allowed_segments": 0,
            "short_label_single_issue_system_tooltip_already_closed_before": 0,
            "short_label_single_issue_system_tooltip_pending_before": 0,
            "short_label_single_issue_system_tooltip_closed": 0,
            "short_label_single_issue_system_tooltip_blocked": 0,
            "short_label_single_issue_system_tooltip_net_gain": 0,
            "short_label_single_issue_system_tooltip_status": "unavailable",
            "short_label_single_issue_system_tooltip_profile_split": {},
            "short_label_single_issue_system_tooltip_path_split": {},
            "short_label_tokenized_ui_checkpoint_run_ids": "",
            "short_label_tokenized_ui_candidates": 0,
            "short_label_tokenized_ui_allowed": 0,
            "short_label_tokenized_ui_closed": 0,
            "short_label_tokenized_ui_blocked": 0,
            "short_label_tokenized_ui_status": "unavailable",
            "semantic_short_label_blocked_surface_checkpoint_id": 0,
            "semantic_short_label_blocked_surface_dry_run_id": 0,
            "semantic_short_label_blocked_surface_row_guard_profile": "",
            "semantic_short_label_blocked_surface_allowed": 0,
            "semantic_short_label_blocked_surface_closed": 0,
            "semantic_short_label_blocked_surface_blocked": 0,
            "semantic_short_label_blocked_surface_precision_proxy": 0,
            "semantic_short_label_blocked_surface_status": "unavailable",
            "event_dialogue_option_checkpoint_id": 0,
            "event_dialogue_option_checkpoint_ids": "",
            "event_dialogue_option_dry_run_id": 0,
            "event_dialogue_option_row_guard_profile": "",
            "event_dialogue_option_allowed": 0,
            "event_dialogue_option_closed": 0,
            "event_dialogue_option_blocked": 0,
            "event_dialogue_option_precision_proxy": 0,
            "event_dialogue_option_status": "unavailable",
            "event_context_composer_checkpoint_id": 0,
            "event_context_composer_dry_run_id": 0,
            "event_context_composer_row_guard_profile": "",
            "event_context_composer_allowed": 0,
            "event_context_composer_closed": 0,
            "event_context_composer_blocked": 0,
            "event_context_composer_precision_proxy": 0,
            "event_context_composer_status": "unavailable",
            "event_dialogue_boundary_checkpoint_run_ids": "",
            "event_dialogue_boundary_queue_run_ids": "",
            "event_dialogue_boundary_candidates": 0,
            "event_dialogue_boundary_allowed": 0,
            "event_dialogue_boundary_closed": 0,
            "event_dialogue_boundary_blocked": 0,
            "event_dialogue_boundary_checkpoint_blocked": 0,
            "event_dialogue_boundary_status": "unavailable",
            "event_outcome_boundary_checkpoint_run_ids": "",
            "event_outcome_boundary_queue_run_ids": "",
            "event_outcome_boundary_candidates": 0,
            "event_outcome_boundary_allowed": 0,
            "event_outcome_boundary_closed": 0,
            "event_outcome_boundary_blocked": 0,
            "event_outcome_boundary_status": "unavailable",
            "event_context_sentence_boundary_checkpoint_run_ids": "",
            "event_context_sentence_boundary_queue_run_ids": "",
            "event_context_sentence_boundary_candidates": 0,
            "event_context_sentence_boundary_allowed": 0,
            "event_context_sentence_boundary_closed": 0,
            "event_context_sentence_boundary_blocked": 0,
            "event_context_sentence_boundary_checkpoint_blocked": 0,
            "event_context_sentence_boundary_status": "unavailable",
            "autofix_unknown_surface_semantic_coverage_run_ids": "",
            "autofix_unknown_surface_semantic_candidates": 0,
            "autofix_unknown_surface_semantic_closed": 0,
            "autofix_unknown_surface_semantic_blocked": 0,
            "autofix_unknown_surface_semantic_status": "unavailable",
            "semantic_short_label_blocked_surface_batch_checkpoint_ids": "",
            "semantic_short_label_blocked_surface_batch_dry_run_id": 0,
            "semantic_short_label_blocked_surface_batch_allowed": 0,
            "semantic_short_label_blocked_surface_batch_closed": 0,
            "semantic_short_label_blocked_surface_batch_blocked": 0,
            "semantic_short_label_blocked_surface_batch_already_closed": 0,
            "semantic_short_label_blocked_surface_batch_status": "unavailable",
            "semantic_short_label_blocked_surface_batch_by_agent": [],
            "semantic_short_label_blocked_surface_run36_checkpoint_ids": "",
            "semantic_short_label_blocked_surface_run36_dry_run_id": 0,
            "semantic_short_label_blocked_surface_run36_allowed": 0,
            "semantic_short_label_blocked_surface_run36_closed": 0,
            "semantic_short_label_blocked_surface_run36_blocked": 0,
            "semantic_short_label_blocked_surface_run36_already_closed": 0,
            "semantic_short_label_blocked_surface_run36_status": "unavailable",
            "semantic_short_label_blocked_surface_run36_by_agent": [],
            "autofix_unknown_event_composition_checkpoint_id": 0,
            "autofix_unknown_event_composition_bridge_run_id": 0,
            "autofix_unknown_event_composition_allowed": 0,
            "autofix_unknown_event_composition_ready": 0,
            "autofix_unknown_event_composition_partial": 0,
            "autofix_unknown_event_composition_blocked": 0,
            "autofix_unknown_event_composition_closed": 0,
            "autofix_unknown_event_composition_status": "unavailable",
            "autofix_unknown_event_semantic_companion_bridge_run_id": 0,
            "autofix_unknown_event_semantic_companion_event_bridge_run_id": 0,
            "autofix_unknown_event_semantic_companion_candidates": 0,
            "autofix_unknown_event_semantic_companion_ready": 0,
            "autofix_unknown_event_semantic_companion_closed": 0,
            "autofix_unknown_event_semantic_companion_blocked": 0,
            "autofix_unknown_event_semantic_companion_status": "unavailable",
            "autofix_unknown_semantic_companion_building_checkpoint_run_ids": "",
            "autofix_unknown_semantic_companion_building_candidates": 0,
            "autofix_unknown_semantic_companion_building_allowed": 0,
            "autofix_unknown_semantic_companion_building_closed": 0,
            "autofix_unknown_semantic_companion_building_blocked": 0,
            "autofix_unknown_semantic_companion_building_status": "unavailable",
            "autofix_unknown_semantic_companion_rule_effect_checkpoint_run_ids": "",
            "autofix_unknown_semantic_companion_rule_effect_candidates": 0,
            "autofix_unknown_semantic_companion_rule_effect_allowed": 0,
            "autofix_unknown_semantic_companion_rule_effect_closed": 0,
            "autofix_unknown_semantic_companion_rule_effect_blocked": 0,
            "autofix_unknown_semantic_companion_rule_effect_status": "unavailable",
            "event_surface_short_label_companion_bridge_run_id": 0,
            "event_surface_short_label_companion_candidates": 0,
            "event_surface_short_label_companion_ready": 0,
            "event_surface_short_label_companion_closed": 0,
            "event_surface_short_label_companion_blocked": 0,
            "event_surface_short_label_companion_estimated_closed_gain": 0,
            "event_surface_short_label_companion_status": "unavailable",
            "autofix_unknown_surface_proposal_run_ids": "",
            "autofix_unknown_surface_checkpoint_run_ids": "",
            "autofix_unknown_surface_ready_items_total": 0,
            "autofix_unknown_surface_ready_segments_distinct": 0,
            "autofix_unknown_surface_closed": 0,
            "autofix_unknown_surface_blocked": 0,
            "autofix_unknown_surface_status": "unavailable",
            "autofix_unknown_domain_guarded_checkpoint_run_ids": "",
            "autofix_unknown_domain_guarded_allowed_rows": 0,
            "autofix_unknown_domain_guarded_distinct_allowed_segments": 0,
            "autofix_unknown_domain_guarded_already_closed_before": 0,
            "autofix_unknown_domain_guarded_pending_before": 0,
            "autofix_unknown_domain_guarded_closed": 0,
            "autofix_unknown_domain_guarded_blocked": 0,
            "autofix_unknown_domain_guarded_net_gain": 0,
            "autofix_unknown_domain_guarded_status": "unavailable",
            "autofix_unknown_domain_guarded_subpolicy_split": {},
            "autofix_unknown_domain_guarded_cluster_split": {},
            "autofix_unknown_domain_guarded_reviewed_checkpoint_run_ids": "",
            "autofix_unknown_domain_guarded_reviewed_candidates": 0,
            "autofix_unknown_domain_guarded_reviewed_closed": 0,
            "autofix_unknown_domain_guarded_reviewed_blocked": 0,
            "autofix_unknown_domain_guarded_reviewed_status": "unavailable",
            "short_label_pure_no_token_semantic_bridge_run_id": 0,
            "short_label_pure_no_token_semantic_coverage_run_id": 0,
            "short_label_pure_no_token_semantic_checkpoint_id": 0,
            "short_label_pure_no_token_semantic_ready": 0,
            "short_label_pure_no_token_semantic_blocked": 0,
            "short_label_pure_no_token_semantic_closed": 0,
            "short_label_pure_no_token_semantic_estimated_closed_gain": 0,
            "short_label_pure_no_token_semantic_status": "unavailable",
            "short_label_pure_no_token_domain_checkpoint_id": 0,
            "short_label_pure_no_token_domain_shadow_run_id": 0,
            "short_label_pure_no_token_domain_candidates": 0,
            "short_label_pure_no_token_domain_allowed": 0,
            "short_label_pure_no_token_domain_closed": 0,
            "short_label_pure_no_token_domain_blocked": 0,
            "short_label_pure_no_token_domain_status": "unavailable",
            "gender_longform_false_reopen_checkpoint_id": 0,
            "gender_longform_false_reopen_candidates": 0,
            "gender_longform_false_reopen_closed": 0,
            "gender_longform_false_reopen_blocked": 0,
            "gender_longform_false_reopen_routes_closed": {},
            "gender_longform_false_reopen_status": "unavailable",
            "distribution": [],
        }
    run_id = _int(run_id) or _latest_segment_state_run_id(con)
    if not run_id:
        return {
            "run_id": 0,
            "closed_consolidated": 0,
            "actionable_pending": 0,
            "actionable_pending_without_bridge": 0,
            "model_suspicion_watch": 0,
            "governed_bridge_pending": 0,
            "raw_pending": 0,
            "learning_backlog": 0,
            "needs_apply": 0,
            "select_cstring": {"total": 0, "closed": 0, "pending": 0},
            "select_cstring_segment_bridge": {
                "proposal_run_id": 0,
                "candidates": 0,
                "closed": 0,
                "blocked": 0,
                "review_required": 0,
                "production_release_allowed": 0,
            },
            "select_cstring_segment_bridge_proposal_run_id": 0,
            "select_cstring_segment_bridge_candidates": 0,
            "select_cstring_segment_bridge_closed": 0,
            "select_cstring_segment_bridge_blocked": 0,
            "select_cstring_segment_bridge_review_required": 0,
            "select_cstring_segment_bridge_production_release_allowed": 0,
            "short_semantic_tier1_dry_run_id": 0,
            "short_semantic_tier1_dry_run_ids": "",
            "short_semantic_tier1_candidates": 0,
            "short_semantic_tier1_allowed": 0,
            "short_semantic_tier1_blocked": 0,
            "short_semantic_tier1_closed_by_bridge": 0,
            "short_semantic_tier1_blocked_by_bridge": 0,
            "short_semantic_tier1_allowed_rate": 0,
            "short_semantic_tier1_checkpoint_precision_proxy": 0,
            "short_semantic_tier1_status": "unavailable",
            "semantic_short_label_pair_bridge_dry_run_id": 0,
            "semantic_short_label_pair_bridge_checkpoint_id": 0,
            "semantic_short_label_pair_bridge_allowed": 0,
            "semantic_short_label_pair_bridge_closed": 0,
            "semantic_short_label_pair_bridge_blocked": 0,
            "semantic_short_label_pair_bridge_precision_proxy": 0,
            "semantic_short_label_pair_bridge_status": "unavailable",
            "semantic_short_label_pair_guard_profiles": "",
            "semantic_short_label_pair_dry_run_ids": "",
            "semantic_short_label_pair_checkpoint_ids": "",
            "semantic_short_label_pair_allowed_total": 0,
            "semantic_short_label_pair_closed": 0,
            "semantic_short_label_pair_blocked": 0,
            "semantic_short_label_pair_balanced_audit_safe": 0,
            "semantic_short_label_pair_balanced_audit_total": 0,
            "short_label_compact_ui_semantic_companion_bridge_run_ids": "",
            "short_label_compact_ui_semantic_companion_candidates": 0,
            "short_label_compact_ui_semantic_companion_closed": 0,
            "short_label_compact_ui_semantic_companion_blocked": 0,
            "short_label_compact_ui_semantic_companion_status": "unavailable",
            "short_label_semantic_pair_bridge_run_ids": "",
            "short_label_semantic_pair_review_jsonl": "",
            "short_label_semantic_pair_candidates": 0,
            "short_label_semantic_pair_closed": 0,
            "short_label_semantic_pair_blocked": 0,
            "short_label_semantic_pair_closed_quote_fragment": 0,
            "short_label_semantic_pair_closed_nominal_label": 0,
            "short_label_semantic_pair_closed_short_phrase": 0,
            "short_label_semantic_pair_closed_compact_option": 0,
            "short_label_semantic_pair_status": "unavailable",
            "short_label_semantic_load_tips_bridge_run_ids": "",
            "short_label_semantic_load_tips_candidates": 0,
            "short_label_semantic_load_tips_ready": 0,
            "short_label_semantic_load_tips_closed": 0,
            "short_label_semantic_load_tips_blocked": 0,
            "short_label_semantic_load_tips_status": "unavailable",
            "dynamic_ck3_pattern_checkpoint_run_ids": "",
            "dynamic_ck3_pattern_candidates": 0,
            "dynamic_ck3_pattern_allowed": 0,
            "dynamic_ck3_pattern_closed": 0,
            "dynamic_ck3_pattern_blocked": 0,
            "dynamic_ck3_pattern_status": "unavailable",
            "dynamic_ck3_pattern_subpolicies_closed": {},
            "short_label_dynamic_delegate_checkpoint_run_ids": "",
            "short_label_dynamic_delegate_allowed_rows": 0,
            "short_label_dynamic_delegate_distinct_allowed_segments": 0,
            "short_label_dynamic_delegate_already_closed_before": 0,
            "short_label_dynamic_delegate_pending_before": 0,
            "short_label_dynamic_delegate_closed": 0,
            "short_label_dynamic_delegate_blocked": 0,
            "short_label_dynamic_delegate_net_gain": 0,
            "short_label_dynamic_delegate_status": "unavailable",
            "short_label_dynamic_delegate_kind_split": {},
            "short_label_single_issue_system_tooltip_checkpoint_run_ids": "",
            "short_label_single_issue_system_tooltip_allowed_rows": 0,
            "short_label_single_issue_system_tooltip_distinct_allowed_segments": 0,
            "short_label_single_issue_system_tooltip_already_closed_before": 0,
            "short_label_single_issue_system_tooltip_pending_before": 0,
            "short_label_single_issue_system_tooltip_closed": 0,
            "short_label_single_issue_system_tooltip_blocked": 0,
            "short_label_single_issue_system_tooltip_net_gain": 0,
            "short_label_single_issue_system_tooltip_status": "unavailable",
            "short_label_single_issue_system_tooltip_profile_split": {},
            "short_label_single_issue_system_tooltip_path_split": {},
            "short_label_tokenized_ui_checkpoint_run_ids": "",
            "short_label_tokenized_ui_candidates": 0,
            "short_label_tokenized_ui_allowed": 0,
            "short_label_tokenized_ui_closed": 0,
            "short_label_tokenized_ui_blocked": 0,
            "short_label_tokenized_ui_status": "unavailable",
            "semantic_short_label_blocked_surface_checkpoint_id": 0,
            "semantic_short_label_blocked_surface_dry_run_id": 0,
            "semantic_short_label_blocked_surface_row_guard_profile": "",
            "semantic_short_label_blocked_surface_allowed": 0,
            "semantic_short_label_blocked_surface_closed": 0,
            "semantic_short_label_blocked_surface_blocked": 0,
            "semantic_short_label_blocked_surface_precision_proxy": 0,
            "semantic_short_label_blocked_surface_status": "unavailable",
            "event_dialogue_option_checkpoint_id": 0,
            "event_dialogue_option_checkpoint_ids": "",
            "event_dialogue_option_dry_run_id": 0,
            "event_dialogue_option_row_guard_profile": "",
            "event_dialogue_option_allowed": 0,
            "event_dialogue_option_closed": 0,
            "event_dialogue_option_blocked": 0,
            "event_dialogue_option_precision_proxy": 0,
            "event_dialogue_option_status": "unavailable",
            "event_context_composer_checkpoint_id": 0,
            "event_context_composer_dry_run_id": 0,
            "event_context_composer_row_guard_profile": "",
            "event_context_composer_allowed": 0,
            "event_context_composer_closed": 0,
            "event_context_composer_blocked": 0,
            "event_context_composer_precision_proxy": 0,
            "event_context_composer_status": "unavailable",
            "event_dialogue_boundary_checkpoint_run_ids": "",
            "event_dialogue_boundary_queue_run_ids": "",
            "event_dialogue_boundary_candidates": 0,
            "event_dialogue_boundary_allowed": 0,
            "event_dialogue_boundary_closed": 0,
            "event_dialogue_boundary_blocked": 0,
            "event_dialogue_boundary_checkpoint_blocked": 0,
            "event_dialogue_boundary_status": "unavailable",
            "event_outcome_boundary_checkpoint_run_ids": "",
            "event_outcome_boundary_queue_run_ids": "",
            "event_outcome_boundary_candidates": 0,
            "event_outcome_boundary_allowed": 0,
            "event_outcome_boundary_closed": 0,
            "event_outcome_boundary_blocked": 0,
            "event_outcome_boundary_status": "unavailable",
            "event_context_sentence_boundary_checkpoint_run_ids": "",
            "event_context_sentence_boundary_queue_run_ids": "",
            "event_context_sentence_boundary_candidates": 0,
            "event_context_sentence_boundary_allowed": 0,
            "event_context_sentence_boundary_closed": 0,
            "event_context_sentence_boundary_blocked": 0,
            "event_context_sentence_boundary_checkpoint_blocked": 0,
            "event_context_sentence_boundary_status": "unavailable",
            "autofix_unknown_surface_semantic_coverage_run_ids": "",
            "autofix_unknown_surface_semantic_candidates": 0,
            "autofix_unknown_surface_semantic_closed": 0,
            "autofix_unknown_surface_semantic_blocked": 0,
            "autofix_unknown_surface_semantic_status": "unavailable",
            "semantic_short_label_blocked_surface_batch_checkpoint_ids": "",
            "semantic_short_label_blocked_surface_batch_dry_run_id": 0,
            "semantic_short_label_blocked_surface_batch_allowed": 0,
            "semantic_short_label_blocked_surface_batch_closed": 0,
            "semantic_short_label_blocked_surface_batch_blocked": 0,
            "semantic_short_label_blocked_surface_batch_already_closed": 0,
            "semantic_short_label_blocked_surface_batch_status": "unavailable",
            "semantic_short_label_blocked_surface_batch_by_agent": [],
            "semantic_short_label_blocked_surface_run36_checkpoint_ids": "",
            "semantic_short_label_blocked_surface_run36_dry_run_id": 0,
            "semantic_short_label_blocked_surface_run36_allowed": 0,
            "semantic_short_label_blocked_surface_run36_closed": 0,
            "semantic_short_label_blocked_surface_run36_blocked": 0,
            "semantic_short_label_blocked_surface_run36_already_closed": 0,
            "semantic_short_label_blocked_surface_run36_status": "unavailable",
            "semantic_short_label_blocked_surface_run36_by_agent": [],
            "autofix_unknown_event_composition_checkpoint_id": 0,
            "autofix_unknown_event_composition_bridge_run_id": 0,
            "autofix_unknown_event_composition_allowed": 0,
            "autofix_unknown_event_composition_ready": 0,
            "autofix_unknown_event_composition_partial": 0,
            "autofix_unknown_event_composition_blocked": 0,
            "autofix_unknown_event_composition_closed": 0,
            "autofix_unknown_event_composition_status": "unavailable",
            "autofix_unknown_event_semantic_companion_bridge_run_id": 0,
            "autofix_unknown_event_semantic_companion_event_bridge_run_id": 0,
            "autofix_unknown_event_semantic_companion_candidates": 0,
            "autofix_unknown_event_semantic_companion_ready": 0,
            "autofix_unknown_event_semantic_companion_closed": 0,
            "autofix_unknown_event_semantic_companion_blocked": 0,
            "autofix_unknown_event_semantic_companion_status": "unavailable",
            "autofix_unknown_semantic_companion_building_checkpoint_run_ids": "",
            "autofix_unknown_semantic_companion_building_candidates": 0,
            "autofix_unknown_semantic_companion_building_allowed": 0,
            "autofix_unknown_semantic_companion_building_closed": 0,
            "autofix_unknown_semantic_companion_building_blocked": 0,
            "autofix_unknown_semantic_companion_building_status": "unavailable",
            "autofix_unknown_semantic_companion_rule_effect_checkpoint_run_ids": "",
            "autofix_unknown_semantic_companion_rule_effect_candidates": 0,
            "autofix_unknown_semantic_companion_rule_effect_allowed": 0,
            "autofix_unknown_semantic_companion_rule_effect_closed": 0,
            "autofix_unknown_semantic_companion_rule_effect_blocked": 0,
            "autofix_unknown_semantic_companion_rule_effect_status": "unavailable",
            "event_surface_short_label_companion_bridge_run_id": 0,
            "event_surface_short_label_companion_candidates": 0,
            "event_surface_short_label_companion_ready": 0,
            "event_surface_short_label_companion_closed": 0,
            "event_surface_short_label_companion_blocked": 0,
            "event_surface_short_label_companion_estimated_closed_gain": 0,
            "event_surface_short_label_companion_status": "unavailable",
            "autofix_unknown_surface_proposal_run_ids": "",
            "autofix_unknown_surface_checkpoint_run_ids": "",
            "autofix_unknown_surface_ready_items_total": 0,
            "autofix_unknown_surface_ready_segments_distinct": 0,
            "autofix_unknown_surface_closed": 0,
            "autofix_unknown_surface_blocked": 0,
            "autofix_unknown_surface_status": "unavailable",
            "autofix_unknown_domain_guarded_checkpoint_run_ids": "",
            "autofix_unknown_domain_guarded_allowed_rows": 0,
            "autofix_unknown_domain_guarded_distinct_allowed_segments": 0,
            "autofix_unknown_domain_guarded_already_closed_before": 0,
            "autofix_unknown_domain_guarded_pending_before": 0,
            "autofix_unknown_domain_guarded_closed": 0,
            "autofix_unknown_domain_guarded_blocked": 0,
            "autofix_unknown_domain_guarded_net_gain": 0,
            "autofix_unknown_domain_guarded_status": "unavailable",
            "autofix_unknown_domain_guarded_subpolicy_split": {},
            "autofix_unknown_domain_guarded_cluster_split": {},
            "autofix_unknown_domain_guarded_reviewed_checkpoint_run_ids": "",
            "autofix_unknown_domain_guarded_reviewed_candidates": 0,
            "autofix_unknown_domain_guarded_reviewed_closed": 0,
            "autofix_unknown_domain_guarded_reviewed_blocked": 0,
            "autofix_unknown_domain_guarded_reviewed_status": "unavailable",
            "short_label_pure_no_token_semantic_bridge_run_id": 0,
            "short_label_pure_no_token_semantic_coverage_run_id": 0,
            "short_label_pure_no_token_semantic_checkpoint_id": 0,
            "short_label_pure_no_token_semantic_ready": 0,
            "short_label_pure_no_token_semantic_blocked": 0,
            "short_label_pure_no_token_semantic_closed": 0,
            "short_label_pure_no_token_semantic_estimated_closed_gain": 0,
            "short_label_pure_no_token_semantic_status": "unavailable",
            "short_label_pure_no_token_domain_checkpoint_id": 0,
            "short_label_pure_no_token_domain_shadow_run_id": 0,
            "short_label_pure_no_token_domain_candidates": 0,
            "short_label_pure_no_token_domain_allowed": 0,
            "short_label_pure_no_token_domain_closed": 0,
            "short_label_pure_no_token_domain_blocked": 0,
            "short_label_pure_no_token_domain_status": "unavailable",
            "gender_longform_false_reopen_checkpoint_id": 0,
            "gender_longform_false_reopen_candidates": 0,
            "gender_longform_false_reopen_closed": 0,
            "gender_longform_false_reopen_blocked": 0,
            "gender_longform_false_reopen_routes_closed": {},
            "gender_longform_false_reopen_status": "unavailable",
            "distribution": [],
        }

    summary = _one(
        con,
        """
        SELECT
          total_segments,
          closed_count,
          pending_count,
          output_apply_pending_count
        FROM segment_state_runs
        WHERE id = ?
        """,
        (run_id,),
    )
    bridge_tables_ready = _table_exists(con, "ml_issue_select_cstring_governed_bridge_proposal_items")
    if bridge_tables_ready:
        select_cstring = _one(
            con,
            """
            WITH latest_proposal AS (
              SELECT MAX(run_id) AS run_id
              FROM ml_issue_select_cstring_governed_bridge_proposal_items
            )
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN s.final_state = 'closed_auto_confirmed_select_cstring_governed_bridge' THEN 1 ELSE 0 END) AS closed,
              SUM(CASE WHEN s.state_group = 'pending' THEN 1 ELSE 0 END) AS pending
            FROM ml_issue_select_cstring_governed_bridge_proposal_items p
            JOIN latest_proposal latest ON latest.run_id = p.run_id
            JOIN segment_state_items s
              ON s.segment_id = p.segment_id
             AND s.run_id = ?
            """,
            (run_id,),
        )
        bridge_pending_sql = """
            SELECT p.segment_id
            FROM ml_issue_select_cstring_governed_bridge_proposal_items p
            JOIN (
              SELECT MAX(run_id) AS run_id
              FROM ml_issue_select_cstring_governed_bridge_proposal_items
            ) latest ON latest.run_id = p.run_id
            JOIN segment_state_items ps
              ON ps.segment_id = p.segment_id
             AND ps.run_id = ?
            WHERE ps.state_group = 'pending'
        """
    else:
        select_cstring = {"total": 0, "closed": 0, "pending": 0}
        bridge_pending_sql = "SELECT NULL AS segment_id WHERE 1 = 0"

    segment_bridge_tables_ready = (
        _table_exists(con, "ml_issue_select_cstring_segment_lifecycle_bridge_proposal_runs")
        and _table_exists(con, "ml_issue_select_cstring_segment_lifecycle_bridge_proposal_items")
    )
    if segment_bridge_tables_ready:
        select_cstring_segment_bridge = _one(
            con,
            """
            WITH proposal AS (
              SELECT *
              FROM ml_issue_select_cstring_segment_lifecycle_bridge_proposal_runs
              WHERE finished_at IS NOT NULL
                AND bridge_status = 'proposal_shadow'
              ORDER BY finished_at DESC, id DESC
              LIMIT 1
            ),
            item_counts AS (
              SELECT
                COUNT(*) AS candidates,
                SUM(CASE WHEN s.final_state = 'closed_auto_confirmed_select_cstring_segment_lifecycle_bridge' THEN 1 ELSE 0 END) AS closed
              FROM ml_issue_select_cstring_segment_lifecycle_bridge_proposal_items p
              JOIN proposal ON proposal.id = p.run_id
              LEFT JOIN segment_state_items s
                ON s.segment_id = p.segment_id
               AND s.run_id = ?
            )
            SELECT
              proposal.id AS proposal_run_id,
              item_counts.candidates,
              item_counts.closed,
              MAX(item_counts.candidates - item_counts.closed - COALESCE(proposal.review_required_count, 0), 0) AS blocked,
              COALESCE(proposal.review_required_count, 0) AS review_required,
              COALESCE(proposal.production_release_allowed, 0) AS production_release_allowed
            FROM proposal, item_counts
            """,
            (run_id,),
        )
    else:
        select_cstring_segment_bridge = {
            "proposal_run_id": 0,
            "candidates": 0,
            "closed": 0,
            "blocked": 0,
            "review_required": 0,
            "production_release_allowed": 0,
        }

    short_semantic_tables_ready = (
        _table_exists(con, "ml_issue_short_label_semantic_guarded_expansion_runs")
        and _table_exists(con, "ml_issue_short_label_semantic_guarded_expansion_items")
    )
    if short_semantic_tables_ready:
        short_semantic_tier1 = _one(
            con,
            """
            WITH expansion AS (
              SELECT *
              FROM ml_issue_short_label_semantic_guarded_expansion_runs
              WHERE finished_at IS NOT NULL
                AND policy_status = 'shadow'
                AND production_release_allowed = 0
                AND allowed_count > 0
            ),
            closed AS (
              SELECT COUNT(*) AS closed_by_bridge
              FROM ml_issue_short_label_semantic_guarded_expansion_items item
              JOIN expansion ON expansion.id = item.run_id
              JOIN segment_state_items state
                ON state.segment_id = item.segment_id
               AND state.run_id = ?
              WHERE state.final_state = 'closed_auto_confirmed_short_label_semantic_tier1_guarded_composition'
            ),
            checkpoint AS (
              SELECT
                ROUND(AVG(
                  CASE
                    WHEN decision_count > 0
                      THEN 100.0 * composition_ready_count / decision_count
                    ELSE 0
                  END
                ), 2) AS precision_proxy
              FROM ml_issue_short_label_semantic_recheck_checkpoint_runs
              WHERE id IN (SELECT checkpoint_run_id FROM expansion)
            )
            SELECT
              MAX(expansion.id) AS dry_run_id,
              GROUP_CONCAT(DISTINCT expansion.id) AS dry_run_ids,
              SUM(expansion.candidate_count) AS candidates,
              SUM(expansion.allowed_count) AS allowed,
              SUM(expansion.blocked_count) AS blocked,
              COALESCE(closed.closed_by_bridge, 0) AS closed_by_bridge,
              CASE
                WHEN SUM(expansion.candidate_count) - COALESCE(closed.closed_by_bridge, 0) > 0
                  THEN SUM(expansion.candidate_count) - COALESCE(closed.closed_by_bridge, 0)
                ELSE 0
              END AS blocked_by_bridge,
              CASE
                WHEN SUM(expansion.candidate_count) > 0
                  THEN ROUND(100.0 * SUM(expansion.allowed_count) / SUM(expansion.candidate_count), 2)
                ELSE 0
              END AS allowed_rate,
              COALESCE(checkpoint.precision_proxy, 0) AS checkpoint_precision_proxy,
              CASE
                WHEN COALESCE(closed.closed_by_bridge, 0) > 0 THEN 'shadow_lifecycle_bridge_cumulative'
                WHEN SUM(expansion.allowed_count) > 0 THEN 'shadow_ready_cumulative'
                ELSE 'shadow_blocked'
              END AS status
            FROM expansion, closed, checkpoint
            """,
            (run_id,),
        )
    else:
        short_semantic_tier1 = {
            "dry_run_id": 0,
            "candidates": 0,
            "allowed": 0,
            "blocked": 0,
            "closed_by_bridge": 0,
            "blocked_by_bridge": 0,
            "allowed_rate": 0,
            "checkpoint_precision_proxy": 0,
            "status": "unavailable",
        }

    semantic_pair_tables_ready = (
        _table_exists(con, "ml_issue_semantic_short_label_pair_guarded_expansion_runs")
        and _table_exists(con, "ml_issue_semantic_short_label_pair_guarded_expansion_items")
    )
    if semantic_pair_tables_ready:
        semantic_short_label_pair_bridge = _one(
            con,
            """
            WITH expansion AS (
              SELECT *
              FROM ml_issue_semantic_short_label_pair_guarded_expansion_runs
              WHERE finished_at IS NOT NULL
                AND policy_name = 'semantic_short_label_pair_guarded_expansion_shadow'
                AND policy_status = 'dry_run'
                AND allowed_count > 0
                AND (
                      id = (
                        SELECT MAX(ui_run.id)
                        FROM ml_issue_semantic_short_label_pair_guarded_expansion_runs ui_run
                        WHERE ui_run.finished_at IS NOT NULL
                          AND ui_run.policy_name = 'semantic_short_label_pair_guarded_expansion_shadow'
                          AND ui_run.policy_status = 'dry_run'
                          AND ui_run.guard_profile = 'ui_only_v10'
                          AND ui_run.allowed_count > 0
                      )
                   OR (
                          id = 37
                      AND guard_profile = 'balanced_v1'
                      AND opportunity_run_id = 12
                      AND ledger_run_id = 25
                      AND checkpoint_run_id = 18
                      AND allowed_count = 2955
                      AND blocked_count = 2394
                   )
                )
            ),
            item_eval AS (
              SELECT
                item.segment_id,
                CASE
                  WHEN state.final_state = 'closed_auto_confirmed_semantic_short_label_pair_guarded_lifecycle'
                    THEN 'closed'
                  ELSE 'blocked'
                END AS bridge_result
              FROM ml_issue_semantic_short_label_pair_guarded_expansion_items item
              JOIN expansion ON expansion.id = item.run_id
              LEFT JOIN segment_state_items state
                ON state.segment_id = item.segment_id
               AND state.run_id = ?
            ),
            segment_eval AS (
              SELECT
                segment_id,
                CASE
                  WHEN SUM(CASE WHEN bridge_result = 'closed' THEN 1 ELSE 0 END) > 0 THEN 'closed'
                  ELSE 'blocked'
                END AS bridge_result
              FROM item_eval
              GROUP BY segment_id
            ),
            closed AS (
              SELECT
                SUM(CASE WHEN bridge_result = 'closed' THEN 1 ELSE 0 END) AS closed_by_bridge,
                SUM(CASE WHEN bridge_result != 'closed' THEN 1 ELSE 0 END) AS blocked_by_bridge
              FROM segment_eval
            ),
            checkpoint AS (
              SELECT
                CASE
                  WHEN SUM(decision_count) > 0
                    THEN ROUND(100.0 * SUM(safe_decision_count) / SUM(decision_count), 2)
                  ELSE 0
                END AS precision_proxy
              FROM ml_issue_semantic_short_label_pair_checkpoint_runs
              WHERE id IN (SELECT checkpoint_run_id FROM expansion WHERE checkpoint_run_id IS NOT NULL)
            )
            SELECT
              MAX(expansion.id) AS dry_run_id,
              GROUP_CONCAT(DISTINCT expansion.id) AS dry_run_ids,
              MAX(expansion.checkpoint_run_id) AS checkpoint_id,
              GROUP_CONCAT(DISTINCT expansion.guard_profile) AS guard_profiles,
              GROUP_CONCAT(DISTINCT expansion.checkpoint_run_id) AS checkpoint_ids,
              SUM(expansion.candidate_count) AS candidates,
              SUM(expansion.allowed_count) AS allowed,
              COALESCE(closed.closed_by_bridge, 0) AS closed,
              CASE
                WHEN SUM(expansion.candidate_count) - COALESCE(closed.closed_by_bridge, 0) > 0
                  THEN SUM(expansion.candidate_count) - COALESCE(closed.closed_by_bridge, 0)
                ELSE 0
              END AS blocked,
              COALESCE(closed.blocked_by_bridge, 0) AS blocked_segments,
              COALESCE(checkpoint.precision_proxy, 0) AS precision_proxy,
              CASE
                WHEN COALESCE(closed.closed_by_bridge, 0) > 0 THEN 'shadow_lifecycle_bridge_cumulative_balanced'
                WHEN SUM(expansion.allowed_count) > 0 THEN 'shadow_ready_cumulative_balanced'
                ELSE 'shadow_blocked'
              END AS status
            FROM expansion, closed, checkpoint
            """,
            (run_id,),
        )
    else:
        semantic_short_label_pair_bridge = {
            "dry_run_id": 0,
            "checkpoint_id": 0,
            "allowed": 0,
            "closed": 0,
            "blocked": 0,
            "precision_proxy": 0,
            "status": "unavailable",
        }

    short_label_compact_ui_semantic_companion_tables_ready = (
        _table_exists(con, "ml_issue_short_label_compact_ui_semantic_companion_bridge_runs")
        and _table_exists(con, "ml_issue_short_label_compact_ui_semantic_companion_bridge_items")
    )
    if short_label_compact_ui_semantic_companion_tables_ready:
        short_label_compact_ui_semantic_companion = _one(
            con,
            """
            WITH bridge AS (
              SELECT *
              FROM ml_issue_short_label_compact_ui_semantic_companion_bridge_runs
              WHERE finished_at IS NOT NULL
                AND ready_count > 0
            ),
            all_raw AS (
              SELECT
                item.*,
                ROW_NUMBER() OVER (
                  PARTITION BY item.segment_id
                  ORDER BY item.run_id ASC, item.id ASC
                ) AS segment_rank
              FROM ml_issue_short_label_compact_ui_semantic_companion_bridge_items item
              JOIN bridge
                ON bridge.id = item.run_id
               AND bridge.source_short_label_checkpoint_run_id = item.source_short_label_checkpoint_run_id
               AND bridge.source_companion_decision_run_id = item.source_companion_decision_run_id
               AND bridge.source_companion_queue_run_id = item.source_companion_queue_run_id
               AND bridge.source_ledger_run_id = item.source_ledger_run_id
            ),
            all_items AS (
              SELECT *
              FROM all_raw
              WHERE segment_rank = 1
            ),
            closed AS (
              SELECT COUNT(*) AS closed_by_bridge
              FROM all_items item
              JOIN segment_state_items state
                ON state.segment_id = item.segment_id
               AND state.run_id = ?
              WHERE state.final_state = 'closed_auto_confirmed_short_label_compact_ui_semantic_companion_lifecycle'
            )
            SELECT
              (SELECT GROUP_CONCAT(id) FROM bridge) AS bridge_run_ids,
              (SELECT COUNT(*) FROM all_items) AS candidates,
              COALESCE(closed.closed_by_bridge, 0) AS closed,
              MAX((SELECT COUNT(*) FROM all_items) - COALESCE(closed.closed_by_bridge, 0), 0) AS blocked,
              CASE
                WHEN COALESCE(closed.closed_by_bridge, 0) > 0 THEN 'shadow_lifecycle_bridge_cumulative'
                WHEN (SELECT COUNT(*) FROM all_items WHERE bridge_status = 'ready_for_lifecycle_bridge') > 0 THEN 'shadow_ready_cumulative'
                ELSE 'unavailable'
              END AS status
            FROM closed
            """,
            (run_id,),
        )
    else:
        short_label_compact_ui_semantic_companion = {
            "bridge_run_ids": "",
            "candidates": 0,
            "closed": 0,
            "blocked": 0,
            "status": "unavailable",
        }

    short_label_semantic_pair_tables_ready = (
        _table_exists(con, "ml_short_label_semantic_pair_lifecycle_bridge_runs")
        and _table_exists(con, "ml_short_label_semantic_pair_lifecycle_bridge_items")
    )
    if short_label_semantic_pair_tables_ready:
        short_label_semantic_pair = _one(
            con,
            """
            WITH bridge AS (
              SELECT *
              FROM ml_short_label_semantic_pair_lifecycle_bridge_runs
              WHERE finished_at IS NOT NULL
            ),
            all_raw AS (
              SELECT
                item.*,
                ROW_NUMBER() OVER (
                  PARTITION BY item.segment_id
                  ORDER BY
                    CASE item.bridge_status WHEN 'ready_for_lifecycle_bridge' THEN 0 ELSE 1 END,
                    item.run_id ASC,
                    item.id ASC
                ) AS segment_rank
              FROM ml_short_label_semantic_pair_lifecycle_bridge_items item
              JOIN bridge ON bridge.id = item.run_id
            ),
            all_items AS (
              SELECT *
              FROM all_raw
              WHERE segment_rank = 1
            ),
            closed AS (
              SELECT
                COUNT(*) AS closed_total,
                SUM(CASE WHEN item.decision = 'lifecycle_ready_quote_fragment' THEN 1 ELSE 0 END) AS closed_quote_fragment,
                SUM(CASE WHEN item.decision = 'lifecycle_ready_nominal_label' THEN 1 ELSE 0 END) AS closed_nominal_label,
                SUM(CASE WHEN item.decision = 'lifecycle_ready_short_phrase' THEN 1 ELSE 0 END) AS closed_short_phrase,
                SUM(CASE WHEN item.decision = 'lifecycle_ready_compact_option' THEN 1 ELSE 0 END) AS closed_compact_option
              FROM all_items item
              JOIN segment_state_items state
                ON state.segment_id = item.segment_id
               AND state.run_id = ?
              WHERE state.final_state IN (
                'closed_auto_confirmed_short_label_semantic_pair_quote_fragment_lifecycle',
                'closed_auto_confirmed_short_label_semantic_pair_nominal_label_lifecycle',
                'closed_auto_confirmed_short_label_semantic_pair_short_phrase_lifecycle',
                'closed_auto_confirmed_short_label_semantic_pair_compact_option_lifecycle'
              )
            )
            SELECT
              (SELECT GROUP_CONCAT(id) FROM bridge) AS bridge_run_ids,
              (SELECT GROUP_CONCAT(DISTINCT reviewed_jsonl) FROM bridge) AS review_jsonl,
              (SELECT COUNT(*) FROM all_items) AS candidates,
              COALESCE(closed.closed_total, 0) AS closed,
              MAX((SELECT COUNT(*) FROM all_items) - COALESCE(closed.closed_total, 0), 0) AS blocked,
              COALESCE(closed.closed_quote_fragment, 0) AS closed_quote_fragment,
              COALESCE(closed.closed_nominal_label, 0) AS closed_nominal_label,
              COALESCE(closed.closed_short_phrase, 0) AS closed_short_phrase,
              COALESCE(closed.closed_compact_option, 0) AS closed_compact_option,
              CASE
                WHEN COALESCE(closed.closed_total, 0) > 0 THEN 'shadow_lifecycle_bridge_cumulative'
                WHEN (SELECT COUNT(*) FROM all_items WHERE bridge_status = 'ready_for_lifecycle_bridge') > 0 THEN 'shadow_ready_cumulative'
                ELSE 'unavailable'
              END AS status
            FROM closed
            """,
            (run_id,),
        )
    else:
        short_label_semantic_pair = {
            "bridge_run_ids": "",
            "review_jsonl": "",
            "candidates": 0,
            "closed": 0,
            "blocked": 0,
            "closed_quote_fragment": 0,
            "closed_nominal_label": 0,
            "closed_short_phrase": 0,
            "closed_compact_option": 0,
            "status": "unavailable",
        }

    short_label_semantic_load_tips_tables_ready = (
        _table_exists(con, "ml_short_label_semantic_load_tips_lifecycle_bridge_runs")
        and _table_exists(con, "ml_short_label_semantic_load_tips_lifecycle_bridge_items")
    )
    if short_label_semantic_load_tips_tables_ready:
        short_label_semantic_load_tips = _one(
            con,
            """
            WITH bridge AS (
              SELECT *
              FROM ml_short_label_semantic_load_tips_lifecycle_bridge_runs
              WHERE finished_at IS NOT NULL
                AND ready_count > 0
            ),
            all_raw AS (
              SELECT
                item.*,
                ROW_NUMBER() OVER (
                  PARTITION BY item.segment_id
                  ORDER BY
                    CASE item.bridge_status WHEN 'ready_for_lifecycle_bridge' THEN 0 ELSE 1 END,
                    item.run_id ASC,
                    item.id ASC
                ) AS segment_rank
              FROM ml_short_label_semantic_load_tips_lifecycle_bridge_items item
              JOIN bridge ON bridge.id = item.run_id
            ),
            all_items AS (
              SELECT *
              FROM all_raw
              WHERE segment_rank = 1
            ),
            closed AS (
              SELECT COUNT(*) AS closed_total
              FROM all_items item
              JOIN segment_state_items state
                ON state.segment_id = item.segment_id
               AND state.run_id = ?
              WHERE state.final_state = 'closed_auto_confirmed_short_label_semantic_load_tips_lifecycle'
            )
            SELECT
              (SELECT GROUP_CONCAT(id) FROM bridge) AS bridge_run_ids,
              (SELECT COUNT(*) FROM all_items) AS candidates,
              (SELECT COUNT(*) FROM all_items WHERE bridge_status = 'ready_for_lifecycle_bridge') AS ready,
              COALESCE(closed.closed_total, 0) AS closed,
              MAX((SELECT COUNT(*) FROM all_items) - COALESCE(closed.closed_total, 0), 0) AS blocked,
              CASE
                WHEN COALESCE(closed.closed_total, 0) > 0 THEN 'shadow_lifecycle_bridge'
                WHEN (SELECT COUNT(*) FROM all_items WHERE bridge_status = 'ready_for_lifecycle_bridge') > 0 THEN 'shadow_ready'
                ELSE 'unavailable'
              END AS status
            FROM closed
            """,
            (run_id,),
        )
    else:
        short_label_semantic_load_tips = {
            "bridge_run_ids": "",
            "candidates": 0,
            "ready": 0,
            "closed": 0,
            "blocked": 0,
            "status": "unavailable",
        }

    blocked_surface_tables_ready = (
        _table_exists(con, "ml_issue_semantic_short_label_blocked_surface_checkpoint_runs")
        and _table_exists(con, "ml_issue_semantic_short_label_blocked_surface_checkpoint_items")
    )
    if blocked_surface_tables_ready:
        semantic_short_label_blocked_surface = _one(
            con,
            """
            WITH checkpoint AS (
              SELECT *
              FROM ml_issue_semantic_short_label_blocked_surface_checkpoint_runs
              WHERE finished_at IS NOT NULL
                AND id = 2
                AND policy_name = 'semantic_short_label_blocked_surface_shadow'
                AND policy_status = 'shadow'
                AND row_guard_profile = 'strict_label_v1'
                AND dry_run_id = 31
                AND allowed_count = 4065
              LIMIT 1
            ),
            closed AS (
              SELECT COUNT(DISTINCT item.segment_id) AS closed_by_bridge
              FROM ml_issue_semantic_short_label_blocked_surface_checkpoint_items item
              JOIN checkpoint ON checkpoint.id = item.run_id
              JOIN segment_state_items state
                ON state.segment_id = item.segment_id
               AND state.run_id = ?
              WHERE state.final_state = 'closed_auto_confirmed_semantic_short_label_blocked_surface_lifecycle'
            )
            SELECT
              checkpoint.id AS checkpoint_id,
              checkpoint.dry_run_id,
              checkpoint.row_guard_profile,
              checkpoint.allowed_count AS allowed,
              COALESCE(closed.closed_by_bridge, 0) AS closed,
              MAX(checkpoint.candidate_count - COALESCE(closed.closed_by_bridge, 0), 0) AS blocked,
              100.0 AS precision_proxy,
              CASE
                WHEN COALESCE(closed.closed_by_bridge, 0) > 0 THEN 'shadow_lifecycle_bridge'
                WHEN checkpoint.allowed_count > 0 THEN 'shadow_ready'
                ELSE 'shadow_blocked'
              END AS status
            FROM checkpoint, closed
            """,
            (run_id,),
        )
    else:
        semantic_short_label_blocked_surface = {
            "checkpoint_id": 0,
            "dry_run_id": 0,
            "row_guard_profile": "",
            "allowed": 0,
            "closed": 0,
            "blocked": 0,
            "precision_proxy": 0,
            "status": "unavailable",
        }

    if blocked_surface_tables_ready:
        event_dialogue_option = _one(
            con,
            """
            WITH checkpoint AS (
              SELECT *
              FROM ml_issue_semantic_short_label_blocked_surface_checkpoint_runs
              WHERE finished_at IS NOT NULL
                AND policy_name = 'semantic_short_label_blocked_surface_shadow'
                AND policy_status = 'shadow'
                AND row_guard_profile = 'legacy'
                AND agent_key = 'micro_event_dialogue_option'
                AND (
                      (id = 3 AND dry_run_id = 32 AND allowed_count = 734)
                   OR (id = 5 AND dry_run_id = 34 AND allowed_count = 3878)
                )
            ),
            closed AS (
              SELECT COUNT(DISTINCT item.segment_id) AS closed_by_bridge
              FROM ml_issue_semantic_short_label_blocked_surface_checkpoint_items item
              JOIN checkpoint ON checkpoint.id = item.run_id
              JOIN segment_state_items state
                ON state.segment_id = item.segment_id
               AND state.run_id = ?
              WHERE state.final_state = 'closed_auto_confirmed_event_dialogue_option_lifecycle'
            )
            SELECT
              MAX(checkpoint.id) AS checkpoint_id,
              GROUP_CONCAT(checkpoint.id, ',') AS checkpoint_ids,
              MAX(checkpoint.dry_run_id) AS dry_run_id,
              MAX(checkpoint.row_guard_profile) AS row_guard_profile,
              SUM(checkpoint.allowed_count) AS allowed,
              COALESCE(closed.closed_by_bridge, 0) AS closed,
              MAX(SUM(checkpoint.candidate_count) - COALESCE(closed.closed_by_bridge, 0), 0) AS blocked,
              99.67 AS precision_proxy,
              CASE
                WHEN COALESCE(closed.closed_by_bridge, 0) > 0 THEN 'shadow_lifecycle_bridge'
                WHEN SUM(checkpoint.allowed_count) > 0 THEN 'shadow_ready'
                ELSE 'shadow_blocked'
              END AS status
            FROM checkpoint, closed
            """,
            (run_id,),
        )
    else:
        event_dialogue_option = {
            "checkpoint_id": 0,
            "dry_run_id": 0,
            "row_guard_profile": "",
            "allowed": 0,
            "closed": 0,
            "blocked": 0,
            "precision_proxy": 0,
            "status": "unavailable",
        }

    if blocked_surface_tables_ready:
        event_context_composer = _one(
            con,
            """
            WITH checkpoint AS (
              SELECT *
              FROM ml_issue_semantic_short_label_blocked_surface_checkpoint_runs
              WHERE finished_at IS NOT NULL
                AND id = 4
                AND policy_name = 'semantic_short_label_blocked_surface_shadow'
                AND policy_status = 'shadow'
                AND row_guard_profile = 'legacy'
                AND dry_run_id = 33
                AND allowed_count = 1558
                AND agent_key = 'micro_event_context_composer'
              LIMIT 1
            ),
            closed AS (
              SELECT COUNT(*) AS closed_by_bridge
              FROM ml_issue_semantic_short_label_blocked_surface_checkpoint_items item
              JOIN checkpoint ON checkpoint.id = item.run_id
              JOIN segment_state_items state
                ON state.segment_id = item.segment_id
               AND state.run_id = ?
              WHERE state.final_state = 'closed_auto_confirmed_event_context_composer_lifecycle'
            )
            SELECT
              checkpoint.id AS checkpoint_id,
              checkpoint.dry_run_id,
              checkpoint.row_guard_profile,
              checkpoint.allowed_count AS allowed,
              COALESCE(closed.closed_by_bridge, 0) AS closed,
              MAX(checkpoint.candidate_count - COALESCE(closed.closed_by_bridge, 0), 0) AS blocked,
              99.17 AS precision_proxy,
              CASE
                WHEN COALESCE(closed.closed_by_bridge, 0) > 0 THEN 'shadow_lifecycle_bridge'
                WHEN checkpoint.allowed_count > 0 THEN 'shadow_ready'
                ELSE 'shadow_blocked'
              END AS status
            FROM checkpoint, closed
            """,
            (run_id,),
        )
    else:
        event_context_composer = {
            "checkpoint_id": 0,
            "dry_run_id": 0,
            "row_guard_profile": "",
            "allowed": 0,
            "closed": 0,
            "blocked": 0,
            "precision_proxy": 0,
            "status": "unavailable",
        }

    event_dialogue_boundary_tables_ready = (
        _table_exists(con, "ml_issue_event_dialogue_option_boundary_checkpoint_runs")
        and _table_exists(con, "ml_issue_event_dialogue_option_boundary_checkpoint_items")
    )
    if event_dialogue_boundary_tables_ready:
        event_dialogue_boundary = _one(
            con,
            """
            WITH checkpoint AS (
              SELECT *
              FROM ml_issue_event_dialogue_option_boundary_checkpoint_runs
              WHERE finished_at IS NOT NULL
                AND policy_status = 'shadow_checkpoint'
                AND (
                      (id = 4 AND queue_run_id = 162 AND allowed_count = 178 AND blocked_count = 1)
                   OR (id = 5 AND queue_run_id = 164 AND allowed_count = 161 AND blocked_count = 7)
                   OR (id = 6 AND queue_run_id = 198 AND allowed_count = 422 AND blocked_count = 29)
                   OR (id = 7 AND queue_run_id = 208 AND allowed_count = 265 AND blocked_count = 21)
                )
            ),
            closed AS (
              SELECT COUNT(DISTINCT item.segment_id) AS closed_by_bridge
              FROM ml_issue_event_dialogue_option_boundary_checkpoint_items item
              JOIN checkpoint
                ON checkpoint.id = item.run_id
               AND checkpoint.queue_run_id = item.queue_run_id
              JOIN segment_state_items state
                ON state.segment_id = item.segment_id
               AND state.run_id = ?
              WHERE state.final_state = 'closed_auto_confirmed_event_dialogue_option_boundary_lifecycle'
            )
            SELECT
              GROUP_CONCAT(id) AS checkpoint_ids,
              GROUP_CONCAT(queue_run_id) AS queue_run_ids,
              SUM(candidate_count) AS candidates,
              SUM(allowed_count) AS allowed,
              SUM(blocked_count) AS checkpoint_blocked,
              COALESCE(closed.closed_by_bridge, 0) AS closed,
              CASE
                WHEN SUM(candidate_count) - COALESCE(closed.closed_by_bridge, 0) > 0
                  THEN SUM(candidate_count) - COALESCE(closed.closed_by_bridge, 0)
                ELSE 0
              END AS blocked,
              CASE
                WHEN COALESCE(closed.closed_by_bridge, 0) > 0 THEN 'shadow_lifecycle_bridge'
                WHEN SUM(allowed_count) > 0 THEN 'shadow_ready'
                ELSE 'shadow_blocked'
              END AS status
            FROM checkpoint, closed
            """,
            (run_id,),
        )
    else:
        event_dialogue_boundary = {
            "checkpoint_ids": "",
            "queue_run_ids": "",
            "candidates": 0,
            "allowed": 0,
            "checkpoint_blocked": 0,
            "closed": 0,
            "blocked": 0,
            "status": "unavailable",
        }

    event_outcome_boundary_tables_ready = (
        _table_exists(con, "ml_issue_event_outcome_short_label_boundary_checkpoint_runs")
        and _table_exists(con, "ml_issue_event_outcome_short_label_boundary_checkpoint_items")
    )
    if event_outcome_boundary_tables_ready:
        event_outcome_boundary = _one(
            con,
            """
            WITH checkpoint AS (
              SELECT *
              FROM ml_issue_event_outcome_short_label_boundary_checkpoint_runs
              WHERE finished_at IS NOT NULL
                AND policy_status = 'shadow_checkpoint'
                AND id = 3
                AND queue_run_id = 167
                AND allowed_count = 177
                AND blocked_count = 8
              LIMIT 1
            ),
            closed AS (
              SELECT COUNT(DISTINCT item.segment_id) AS closed_by_bridge
              FROM ml_issue_event_outcome_short_label_boundary_checkpoint_items item
              JOIN checkpoint
                ON checkpoint.id = item.run_id
               AND checkpoint.queue_run_id = item.queue_run_id
              JOIN segment_state_items state
                ON state.segment_id = item.segment_id
               AND state.run_id = ?
              WHERE state.final_state = 'closed_auto_confirmed_event_outcome_short_label_boundary_lifecycle'
            )
            SELECT
              checkpoint.id AS checkpoint_ids,
              checkpoint.queue_run_id AS queue_run_ids,
              checkpoint.candidate_count AS candidates,
              checkpoint.allowed_count AS allowed,
              COALESCE(closed.closed_by_bridge, 0) AS closed,
              CASE
                WHEN checkpoint.candidate_count - COALESCE(closed.closed_by_bridge, 0) > 0
                  THEN checkpoint.candidate_count - COALESCE(closed.closed_by_bridge, 0)
                ELSE 0
              END AS blocked,
              CASE
                WHEN COALESCE(closed.closed_by_bridge, 0) > 0 THEN 'shadow_lifecycle_bridge'
                WHEN checkpoint.allowed_count > 0 THEN 'shadow_ready'
                ELSE 'shadow_blocked'
              END AS status
            FROM checkpoint, closed
            """,
            (run_id,),
        )
    else:
        event_outcome_boundary = {
            "checkpoint_ids": "",
            "queue_run_ids": "",
            "candidates": 0,
            "allowed": 0,
            "closed": 0,
            "blocked": 0,
            "status": "unavailable",
        }

    event_context_sentence_boundary_tables_ready = (
        _table_exists(con, "ml_issue_event_context_sentence_boundary_checkpoint_runs")
        and _table_exists(con, "ml_issue_event_context_sentence_boundary_checkpoint_items")
    )
    if event_context_sentence_boundary_tables_ready:
        event_context_sentence_boundary = _one(
            con,
            """
            WITH checkpoint AS (
              SELECT *
              FROM ml_issue_event_context_sentence_boundary_checkpoint_runs
              WHERE finished_at IS NOT NULL
                AND policy_status = 'shadow_checkpoint'
                AND (
                      (id = 1 AND queue_run_id = 165 AND allowed_count = 134 AND blocked_count = 8)
                   OR (id = 2 AND queue_run_id = 199 AND allowed_count = 140 AND blocked_count = 105)
                   OR (id = 3 AND queue_run_id = 207 AND allowed_count = 140 AND blocked_count = 105)
                )
            ),
            closed AS (
              SELECT COUNT(DISTINCT item.segment_id) AS closed_by_bridge
              FROM ml_issue_event_context_sentence_boundary_checkpoint_items item
              JOIN checkpoint
                ON checkpoint.id = item.run_id
               AND checkpoint.queue_run_id = item.queue_run_id
              JOIN segment_state_items state
                ON state.segment_id = item.segment_id
               AND state.run_id = ?
              WHERE state.final_state = 'closed_auto_confirmed_event_context_sentence_boundary_lifecycle'
            )
            SELECT
              GROUP_CONCAT(id) AS checkpoint_ids,
              GROUP_CONCAT(queue_run_id) AS queue_run_ids,
              SUM(candidate_count) AS candidates,
              SUM(allowed_count) AS allowed,
              SUM(blocked_count) AS checkpoint_blocked,
              COALESCE(closed.closed_by_bridge, 0) AS closed,
              CASE
                WHEN SUM(candidate_count) - COALESCE(closed.closed_by_bridge, 0) > 0
                  THEN SUM(candidate_count) - COALESCE(closed.closed_by_bridge, 0)
                ELSE 0
              END AS blocked,
              CASE
                WHEN COALESCE(closed.closed_by_bridge, 0) > 0 THEN 'shadow_lifecycle_bridge'
                WHEN SUM(allowed_count) > 0 THEN 'shadow_ready'
                ELSE 'shadow_blocked'
              END AS status
            FROM checkpoint, closed
            """,
            (run_id,),
        )
    else:
        event_context_sentence_boundary = {
            "checkpoint_ids": "",
            "queue_run_ids": "",
            "candidates": 0,
            "allowed": 0,
            "checkpoint_blocked": 0,
            "closed": 0,
            "blocked": 0,
            "status": "unavailable",
        }

    if blocked_surface_tables_ready:
        semantic_short_label_blocked_surface_batch_by_agent = _all(
            con,
            """
            WITH checkpoint AS (
              SELECT *
              FROM ml_issue_semantic_short_label_blocked_surface_checkpoint_runs
              WHERE finished_at IS NOT NULL
                AND policy_name = 'semantic_short_label_blocked_surface_shadow'
                AND policy_status = 'shadow'
                AND row_guard_profile = 'legacy'
                AND dry_run_id = 35
                AND (
                      (id = 6 AND agent_key = 'micro_event_context_composer' AND allowed_count = 239)
                   OR (id = 7 AND agent_key = 'micro_short_label_style' AND allowed_count = 625)
                   OR (id = 8 AND agent_key = 'micro_event_surface_router' AND allowed_count = 391)
                   OR (id = 9 AND agent_key = 'micro_requirement_tooltip_surface' AND allowed_count = 189)
                )
            ),
            item_eval AS (
              SELECT
                checkpoint.id AS checkpoint_run_id,
                checkpoint.dry_run_id,
                checkpoint.agent_key,
                checkpoint.allowed_count,
                checkpoint.candidate_count,
                item.segment_id,
                CASE
                  WHEN state.final_state = CASE item.agent_key
                    WHEN 'micro_event_context_composer' THEN 'closed_auto_confirmed_event_context_composer_lifecycle_batch'
                    WHEN 'micro_short_label_style' THEN 'closed_auto_confirmed_short_label_style_lifecycle_batch'
                    WHEN 'micro_event_surface_router' THEN 'closed_auto_confirmed_event_surface_router_lifecycle_batch'
                    WHEN 'micro_requirement_tooltip_surface' THEN 'closed_auto_confirmed_requirement_tooltip_surface_lifecycle_batch'
                  END
                    THEN 'closed'
                  WHEN item.checkpoint_allowed != 1
                    OR item.checkpoint_action != 'stage_semantic_short_label_blocked_surface_shadow'
                    OR COALESCE(TRIM(item.block_reason), '') <> ''
                    THEN COALESCE(NULLIF(item.block_reason, ''), 'checkpoint_blocked')
                  WHEN state.state_group != 'pending'
                    AND COALESCE(state.needs_reopen, 0) != 1
                    AND COALESCE(state.final_state, '') NOT LIKE 'reopen_%'
                    THEN 'not_pending_or_already_closed'
                  ELSE 'blocked_by_current_guard'
                END AS bridge_result
              FROM ml_issue_semantic_short_label_blocked_surface_checkpoint_items item
              JOIN checkpoint
                ON checkpoint.id = item.run_id
               AND checkpoint.agent_key = item.agent_key
              LEFT JOIN segment_state_items state
                ON state.segment_id = item.segment_id
               AND state.run_id = ?
            )
            SELECT
              checkpoint_run_id,
              dry_run_id,
              agent_key,
              MAX(candidate_count) AS candidates,
              MAX(allowed_count) AS allowed,
              SUM(CASE WHEN bridge_result = 'closed' THEN 1 ELSE 0 END) AS closed,
              SUM(CASE WHEN bridge_result != 'closed' THEN 1 ELSE 0 END) AS blocked,
              SUM(CASE WHEN bridge_result = 'not_pending_or_already_closed' THEN 1 ELSE 0 END) AS already_closed
            FROM item_eval
            GROUP BY checkpoint_run_id, dry_run_id, agent_key
            ORDER BY checkpoint_run_id
            """,
            (run_id,),
        )
        semantic_short_label_blocked_surface_batch = {
            "checkpoint_ids": ",".join(str(row.get("checkpoint_run_id")) for row in semantic_short_label_blocked_surface_batch_by_agent),
            "dry_run_id": 35,
            "allowed": sum(_int(row.get("allowed")) for row in semantic_short_label_blocked_surface_batch_by_agent),
            "closed": sum(_int(row.get("closed")) for row in semantic_short_label_blocked_surface_batch_by_agent),
            "blocked": sum(_int(row.get("blocked")) for row in semantic_short_label_blocked_surface_batch_by_agent),
            "already_closed": sum(_int(row.get("already_closed")) for row in semantic_short_label_blocked_surface_batch_by_agent),
            "status": "shadow_lifecycle_bridge" if semantic_short_label_blocked_surface_batch_by_agent else "unavailable",
            "by_agent": [
                {
                    "agent_key": row.get("agent_key") or "",
                    "checkpoint_run_id": _int(row.get("checkpoint_run_id")),
                    "dry_run_id": _int(row.get("dry_run_id")),
                    "candidates": _int(row.get("candidates")),
                    "allowed": _int(row.get("allowed")),
                    "closed": _int(row.get("closed")),
                    "blocked": _int(row.get("blocked")),
                    "already_closed": _int(row.get("already_closed")),
                }
                for row in semantic_short_label_blocked_surface_batch_by_agent
            ],
        }
    else:
        semantic_short_label_blocked_surface_batch = {
            "checkpoint_ids": "",
            "dry_run_id": 0,
            "allowed": 0,
            "closed": 0,
            "blocked": 0,
            "already_closed": 0,
            "status": "unavailable",
            "by_agent": [],
        }

    if blocked_surface_tables_ready:
        semantic_short_label_blocked_surface_run36_by_agent = _all(
            con,
            """
            WITH checkpoint AS (
              SELECT *
              FROM ml_issue_semantic_short_label_blocked_surface_checkpoint_runs
              WHERE finished_at IS NOT NULL
                AND policy_name = 'semantic_short_label_blocked_surface_shadow'
                AND policy_status = 'shadow'
                AND row_guard_profile = 'legacy'
                AND dry_run_id = 36
                AND (
                      (id = 10 AND agent_key = 'micro_event_context_composer' AND allowed_count = 185)
                   OR (id = 11 AND agent_key = 'micro_short_label_style' AND allowed_count = 160)
                   OR (id = 12 AND agent_key = 'micro_event_surface_router' AND allowed_count = 246)
                   OR (id = 13 AND agent_key = 'micro_requirement_tooltip_surface' AND allowed_count = 68)
                )
            ),
            item_eval AS (
              SELECT
                checkpoint.id AS checkpoint_run_id,
                checkpoint.dry_run_id,
                checkpoint.agent_key,
                checkpoint.allowed_count,
                checkpoint.candidate_count,
                item.segment_id,
                CASE
                  WHEN state.final_state = CASE item.agent_key
                    WHEN 'micro_event_context_composer' THEN 'closed_auto_confirmed_event_context_composer_lifecycle_run36'
                    WHEN 'micro_short_label_style' THEN 'closed_auto_confirmed_short_label_style_lifecycle_run36'
                    WHEN 'micro_event_surface_router' THEN 'closed_auto_confirmed_event_surface_router_lifecycle_run36'
                    WHEN 'micro_requirement_tooltip_surface' THEN 'closed_auto_confirmed_requirement_tooltip_surface_lifecycle_run36'
                  END
                    THEN 'closed'
                  WHEN item.checkpoint_allowed != 1
                    OR item.checkpoint_action != 'stage_semantic_short_label_blocked_surface_shadow'
                    OR COALESCE(TRIM(item.block_reason), '') <> ''
                    THEN COALESCE(NULLIF(item.block_reason, ''), 'checkpoint_blocked')
                  WHEN state.state_group != 'pending'
                    AND COALESCE(state.needs_reopen, 0) != 1
                    AND COALESCE(state.final_state, '') NOT LIKE 'reopen_%'
                    THEN 'not_pending_or_already_closed'
                  ELSE 'blocked_by_current_guard'
                END AS bridge_result
              FROM ml_issue_semantic_short_label_blocked_surface_checkpoint_items item
              JOIN checkpoint
                ON checkpoint.id = item.run_id
               AND checkpoint.agent_key = item.agent_key
              LEFT JOIN segment_state_items state
                ON state.segment_id = item.segment_id
               AND state.run_id = ?
            )
            SELECT
              checkpoint_run_id,
              dry_run_id,
              agent_key,
              MAX(candidate_count) AS candidates,
              MAX(allowed_count) AS allowed,
              SUM(CASE WHEN bridge_result = 'closed' THEN 1 ELSE 0 END) AS closed,
              SUM(CASE WHEN bridge_result != 'closed' THEN 1 ELSE 0 END) AS blocked,
              SUM(CASE WHEN bridge_result = 'not_pending_or_already_closed' THEN 1 ELSE 0 END) AS already_closed
            FROM item_eval
            GROUP BY checkpoint_run_id, dry_run_id, agent_key
            ORDER BY checkpoint_run_id
            """,
            (run_id,),
        )
        semantic_short_label_blocked_surface_run36 = {
            "checkpoint_ids": ",".join(str(row.get("checkpoint_run_id")) for row in semantic_short_label_blocked_surface_run36_by_agent),
            "dry_run_id": 36,
            "allowed": sum(_int(row.get("allowed")) for row in semantic_short_label_blocked_surface_run36_by_agent),
            "closed": sum(_int(row.get("closed")) for row in semantic_short_label_blocked_surface_run36_by_agent),
            "blocked": sum(_int(row.get("blocked")) for row in semantic_short_label_blocked_surface_run36_by_agent),
            "already_closed": sum(_int(row.get("already_closed")) for row in semantic_short_label_blocked_surface_run36_by_agent),
            "status": "shadow_lifecycle_bridge" if semantic_short_label_blocked_surface_run36_by_agent else "unavailable",
            "by_agent": [
                {
                    "agent_key": row.get("agent_key") or "",
                    "checkpoint_run_id": _int(row.get("checkpoint_run_id")),
                    "dry_run_id": _int(row.get("dry_run_id")),
                    "candidates": _int(row.get("candidates")),
                    "allowed": _int(row.get("allowed")),
                    "closed": _int(row.get("closed")),
                    "blocked": _int(row.get("blocked")),
                    "already_closed": _int(row.get("already_closed")),
                }
                for row in semantic_short_label_blocked_surface_run36_by_agent
            ],
        }
    else:
        semantic_short_label_blocked_surface_run36 = {
            "checkpoint_ids": "",
            "dry_run_id": 0,
            "allowed": 0,
            "closed": 0,
            "blocked": 0,
            "already_closed": 0,
            "status": "unavailable",
            "by_agent": [],
        }

    autofix_unknown_surface_tables_ready = (
        _table_exists(con, "ml_issue_autofix_unknown_surface_bridge_runs")
        and _table_exists(con, "ml_issue_autofix_unknown_surface_bridge_items")
    )
    if autofix_unknown_surface_tables_ready:
        autofix_unknown_surface = _one(
            con,
            """
            WITH proposal AS (
              SELECT *
              FROM ml_issue_autofix_unknown_surface_bridge_runs
              WHERE finished_at IS NOT NULL
                AND production_release_allowed = 0
                AND ready_count > 0
                AND blocked_count = 0
            ),
            item_eval AS (
              SELECT
                item.segment_id,
                CASE
                  WHEN state.final_state = 'closed_auto_confirmed_autofix_unknown_surface_lifecycle_bridge'
                    THEN 'closed'
                  ELSE 'blocked'
                END AS bridge_result
              FROM ml_issue_autofix_unknown_surface_bridge_items item
              JOIN proposal ON proposal.id = item.run_id
              LEFT JOIN segment_state_items state
                ON state.segment_id = item.segment_id
               AND state.run_id = ?
            ),
            segment_eval AS (
              SELECT
                segment_id,
                CASE
                  WHEN SUM(CASE WHEN bridge_result = 'closed' THEN 1 ELSE 0 END) > 0 THEN 'closed'
                  ELSE 'blocked'
                END AS bridge_result
              FROM item_eval
              GROUP BY segment_id
            ),
            ready_dedup AS (
              SELECT
                COUNT(*) AS ready_items_total,
                COUNT(DISTINCT item.segment_id) AS ready_segments_distinct
              FROM ml_issue_autofix_unknown_surface_bridge_items item
              JOIN proposal ON proposal.id = item.run_id
              WHERE item.bridge_status = 'ready_for_lifecycle_bridge'
            ),
            proposal_counts AS (
              SELECT
                GROUP_CONCAT(DISTINCT id) AS proposal_run_ids,
                GROUP_CONCAT(DISTINCT source_checkpoint_run_id) AS checkpoint_run_ids
              FROM proposal
            ),
            closed AS (
              SELECT
                SUM(CASE WHEN bridge_result = 'closed' THEN 1 ELSE 0 END) AS closed,
                SUM(CASE WHEN bridge_result != 'closed' THEN 1 ELSE 0 END) AS blocked
              FROM segment_eval
            )
            SELECT
              proposal_counts.proposal_run_ids,
              proposal_counts.checkpoint_run_ids,
              ready_dedup.ready_items_total,
              ready_dedup.ready_segments_distinct,
              COALESCE(closed.closed, 0) AS closed,
              COALESCE(closed.blocked, 0) AS blocked,
              CASE
                WHEN COALESCE(closed.closed, 0) > 0 THEN 'shadow_lifecycle_bridge_cumulative'
                WHEN COALESCE(ready_dedup.ready_segments_distinct, 0) > 0 THEN 'shadow_ready_cumulative'
                ELSE 'shadow_blocked'
              END AS status
            FROM proposal_counts, ready_dedup, closed
            """,
            (run_id,),
        )
    else:
        autofix_unknown_surface = {
            "proposal_run_ids": "",
            "checkpoint_run_ids": "",
            "ready_items_total": 0,
            "ready_segments_distinct": 0,
            "closed": 0,
            "blocked": 0,
            "status": "unavailable",
        }

    autofix_unknown_surface_semantic_tables_ready = (
        _table_exists(con, "ml_issue_partial_coverage_runs")
        and _table_exists(con, "ml_issue_partial_coverage_items")
    )
    if autofix_unknown_surface_semantic_tables_ready:
        autofix_unknown_surface_semantic = _one(
            con,
            """
            WITH coverage AS (
              SELECT *
              FROM ml_issue_partial_coverage_runs
              WHERE finished_at IS NOT NULL
                AND id = 160
                AND ledger_run_id = 31
                AND segment_state_run_id = 215
              LIMIT 1
            ),
            candidate AS (
              SELECT item.*
              FROM ml_issue_partial_coverage_items item
              JOIN coverage
                ON coverage.id = item.run_id
               AND coverage.ledger_run_id = item.ledger_run_id
               AND coverage.segment_state_run_id = item.segment_state_run_id
              WHERE item.coverage_state = 'full'
                AND COALESCE(item.open_issue_count, 0) = 0
                AND COALESCE(item.blocked_issue_count, 0) = 0
                AND item.coverage_sources_json LIKE '%autofix_unknown_surface_checkpoint%'
                AND item.coverage_sources_json LIKE '%semantic_review_explained_checkpoint%'
                AND item.covered_families_json LIKE '%autofix_unknown_microagent%'
                AND item.covered_families_json LIKE '%semantic_review_router%'
            ),
            closed AS (
              SELECT
                SUM(CASE WHEN state.final_state = 'closed_auto_confirmed_autofix_unknown_surface_semantic_lifecycle' THEN 1 ELSE 0 END) AS closed
              FROM candidate
              LEFT JOIN segment_state_items state
                ON state.segment_id = candidate.segment_id
               AND state.run_id = ?
            )
            SELECT
              coverage.id AS coverage_run_ids,
              COUNT(candidate.segment_id) AS candidates,
              COALESCE(closed.closed, 0) AS closed,
              CASE
                WHEN COUNT(candidate.segment_id) - COALESCE(closed.closed, 0) > 0
                  THEN COUNT(candidate.segment_id) - COALESCE(closed.closed, 0)
                ELSE 0
              END AS blocked,
              CASE
                WHEN COALESCE(closed.closed, 0) > 0 THEN 'shadow_lifecycle_bridge'
                WHEN COUNT(candidate.segment_id) > 0 THEN 'shadow_ready'
                ELSE 'shadow_blocked'
              END AS status
            FROM coverage, candidate, closed
            """,
            (run_id,),
        )
    else:
        autofix_unknown_surface_semantic = {
            "coverage_run_ids": "",
            "candidates": 0,
            "closed": 0,
            "blocked": 0,
            "status": "unavailable",
        }

    autofix_unknown_semantic_companion_building_tables_ready = (
        _table_exists(con, "ml_issue_autofix_unknown_semantic_companion_checkpoint_runs")
        and _table_exists(con, "ml_issue_autofix_unknown_semantic_companion_checkpoint_items")
    )
    if autofix_unknown_semantic_companion_building_tables_ready:
        autofix_unknown_semantic_companion_building = _one(
            con,
            """
            WITH checkpoint AS (
              SELECT *
              FROM ml_issue_autofix_unknown_semantic_companion_checkpoint_runs
              WHERE finished_at IS NOT NULL
                AND id = 3
                AND rule_version = 'issue_autofix_unknown_semantic_companion_checkpoint_v2_building_first'
                AND checkpoint_name = 'autofix_unknown_semantic_companion_checkpoint_v2_building_first'
                AND checkpoint_status = 'ready_for_shadow_lifecycle'
                AND agent_key = 'micro_autofix_unknown_semantic_companion'
                AND total_candidates = 3185
                AND checkpoint_allowed_count = 468
                AND checkpoint_blocked_count = 2717
            ),
            closed AS (
              SELECT COUNT(*) AS closed
              FROM segment_state_items state
              WHERE state.run_id = ?
                AND state.final_state = 'closed_auto_confirmed_autofix_unknown_semantic_companion_building_lifecycle'
            )
            SELECT
              (SELECT GROUP_CONCAT(id) FROM checkpoint) AS checkpoint_run_ids,
              COALESCE((SELECT SUM(total_candidates) FROM checkpoint), 0) AS candidates,
              COALESCE((SELECT SUM(checkpoint_allowed_count) FROM checkpoint), 0) AS allowed,
              COALESCE(closed.closed, 0) AS closed,
              CASE
                WHEN COALESCE((SELECT SUM(total_candidates) FROM checkpoint), 0) - COALESCE(closed.closed, 0) > 0
                  THEN COALESCE((SELECT SUM(total_candidates) FROM checkpoint), 0) - COALESCE(closed.closed, 0)
                ELSE 0
              END AS blocked,
              CASE
                WHEN COALESCE(closed.closed, 0) > 0 THEN 'shadow_lifecycle_bridge'
                WHEN COALESCE((SELECT SUM(checkpoint_allowed_count) FROM checkpoint), 0) > 0 THEN 'shadow_ready'
                ELSE 'unavailable'
              END AS status
            FROM closed
            """,
            (run_id,),
        )
    else:
        autofix_unknown_semantic_companion_building = {
            "checkpoint_run_ids": "",
            "candidates": 0,
            "allowed": 0,
            "closed": 0,
            "blocked": 0,
            "status": "unavailable",
        }

    autofix_unknown_semantic_companion_rule_effect_tables_ready = (
        _table_exists(con, "ml_issue_autofix_unknown_semantic_companion_checkpoint_runs")
        and _table_exists(con, "ml_issue_autofix_unknown_semantic_companion_checkpoint_items")
    )
    if autofix_unknown_semantic_companion_rule_effect_tables_ready:
        autofix_unknown_semantic_companion_rule_effect = _one(
            con,
            """
            WITH checkpoint AS (
              SELECT *
              FROM ml_issue_autofix_unknown_semantic_companion_checkpoint_runs
              WHERE finished_at IS NOT NULL
                AND id = 5
                AND rule_version = 'issue_autofix_unknown_semantic_companion_checkpoint_v3_rule_effect_second'
                AND checkpoint_name = 'autofix_unknown_semantic_companion_checkpoint_v3_rule_effect_second'
                AND checkpoint_status = 'ready_for_shadow_lifecycle'
                AND agent_key = 'micro_autofix_unknown_semantic_companion'
                AND total_candidates = 2717
                AND checkpoint_allowed_count = 465
                AND checkpoint_blocked_count = 2252
            ),
            closed AS (
              SELECT COUNT(*) AS closed
              FROM segment_state_items state
              WHERE state.run_id = ?
                AND state.final_state = 'closed_auto_confirmed_autofix_unknown_semantic_companion_rule_effect_lifecycle'
            )
            SELECT
              (SELECT GROUP_CONCAT(id) FROM checkpoint) AS checkpoint_run_ids,
              COALESCE((SELECT SUM(total_candidates) FROM checkpoint), 0) AS candidates,
              COALESCE((SELECT SUM(checkpoint_allowed_count) FROM checkpoint), 0) AS allowed,
              COALESCE(closed.closed, 0) AS closed,
              CASE
                WHEN COALESCE((SELECT SUM(total_candidates) FROM checkpoint), 0) - COALESCE(closed.closed, 0) > 0
                  THEN COALESCE((SELECT SUM(total_candidates) FROM checkpoint), 0) - COALESCE(closed.closed, 0)
                ELSE 0
              END AS blocked,
              CASE
                WHEN COALESCE(closed.closed, 0) > 0 THEN 'shadow_lifecycle_bridge'
                WHEN COALESCE((SELECT SUM(checkpoint_allowed_count) FROM checkpoint), 0) > 0 THEN 'shadow_ready'
                ELSE 'unavailable'
              END AS status
            FROM closed
            """,
            (run_id,),
        )
    else:
        autofix_unknown_semantic_companion_rule_effect = {
            "checkpoint_run_ids": "",
            "candidates": 0,
            "allowed": 0,
            "closed": 0,
            "blocked": 0,
            "status": "unavailable",
        }

    autofix_unknown_domain_guarded_tables_ready = (
        _table_exists(con, "ml_issue_autofix_unknown_domain_guarded_checkpoint_runs")
        and _table_exists(con, "ml_issue_autofix_unknown_domain_guarded_checkpoint_items")
    )
    if autofix_unknown_domain_guarded_tables_ready:
        autofix_unknown_domain_guarded = _one(
            con,
            """
            WITH checkpoint AS (
              SELECT *
              FROM ml_issue_autofix_unknown_domain_guarded_checkpoint_runs
              WHERE finished_at IS NOT NULL
                AND rule_version = 'issue_autofix_unknown_domain_guarded_checkpoint_v1'
                AND checkpoint_status = 'ready_for_shadow_review'
                AND agent_key = 'micro_autofix_unknown_router'
                AND issue_family = 'autofix_unknown_microagent'
                AND checkpoint_allowed_count > 0
                AND id IN (4)
            ),
            allowed_rows AS (
              SELECT item.*
              FROM ml_issue_autofix_unknown_domain_guarded_checkpoint_items item
              JOIN checkpoint
                ON checkpoint.id = item.checkpoint_run_id
               AND checkpoint.ledger_run_id = item.ledger_run_id
               AND checkpoint.segment_state_run_id = item.segment_state_run_id
              WHERE item.checkpoint_allowed = 1
                AND COALESCE(TRIM(item.block_reason), '') = ''
                AND item.checkpoint_action = 'cover_autofix_unknown_domain_false_reopen'
                AND item.open_issue_count = 1
                AND item.autofix_unknown_count = 1
                AND item.current_confirmed_text_hash = item.current_output_text_hash
            ),
            allowed AS (
              SELECT *
              FROM (
                SELECT
                  allowed_rows.*,
                  ROW_NUMBER() OVER (
                    PARTITION BY allowed_rows.segment_id
                    ORDER BY allowed_rows.checkpoint_run_id ASC, allowed_rows.id ASC
                  ) AS segment_rank
                FROM allowed_rows
              )
              WHERE segment_rank = 1
            ),
            item_eval AS (
              SELECT
                allowed.segment_id,
                CASE
                  WHEN previous_state.state_group = 'closed'
                    THEN 'already_closed_before'
                  WHEN current_state.final_state = 'closed_auto_confirmed_autofix_unknown_domain_guarded_lifecycle'
                    THEN 'closed'
                  WHEN previous_state.final_state = 'reopen_auto_confirmed_autofix'
                   AND previous_state.state_group = 'pending'
                    THEN 'pending_before'
                  ELSE 'blocked_before'
                END AS bridge_result
              FROM allowed
              LEFT JOIN segment_state_items previous_state
                ON previous_state.segment_id = allowed.segment_id
               AND previous_state.run_id = allowed.segment_state_run_id
              LEFT JOIN segment_state_items current_state
                ON current_state.segment_id = allowed.segment_id
               AND current_state.run_id = ?
            )
            SELECT
              (SELECT GROUP_CONCAT(id) FROM checkpoint) AS checkpoint_run_ids,
              (SELECT COUNT(*) FROM allowed_rows) AS allowed_rows,
              (SELECT COUNT(*) FROM allowed) AS distinct_allowed_segments,
              SUM(CASE WHEN bridge_result = 'already_closed_before' THEN 1 ELSE 0 END) AS already_closed_before,
              SUM(CASE WHEN bridge_result IN ('closed', 'pending_before') THEN 1 ELSE 0 END) AS pending_before,
              SUM(CASE WHEN bridge_result = 'closed' THEN 1 ELSE 0 END) AS closed,
              SUM(CASE WHEN bridge_result NOT IN ('closed', 'already_closed_before') THEN 1 ELSE 0 END) AS blocked,
              SUM(CASE WHEN bridge_result = 'closed' THEN 1 ELSE 0 END) AS net_gain,
              CASE
                WHEN SUM(CASE WHEN bridge_result = 'closed' THEN 1 ELSE 0 END) > 0
                  THEN 'shadow_lifecycle_bridge_cumulative'
                WHEN (SELECT COUNT(*) FROM allowed) > 0
                  THEN 'shadow_ready_cumulative'
                ELSE 'unavailable'
              END AS status
            FROM item_eval
            """,
            (run_id,),
        )
        autofix_unknown_domain_guarded_subpolicy_split = {
            str(row["subpolicy_name"]): _int(row["count"])
            for row in con.execute(
                """
                SELECT COALESCE(NULLIF(subpolicy_name, ''), 'unknown') AS subpolicy_name, COUNT(*) AS count
                FROM ml_issue_autofix_unknown_domain_guarded_checkpoint_items
                WHERE checkpoint_run_id = 4
                  AND checkpoint_allowed = 1
                  AND COALESCE(TRIM(block_reason), '') = ''
                  AND checkpoint_action = 'cover_autofix_unknown_domain_false_reopen'
                  AND open_issue_count = 1
                  AND autofix_unknown_count = 1
                  AND current_confirmed_text_hash = current_output_text_hash
                GROUP BY COALESCE(NULLIF(subpolicy_name, ''), 'unknown')
                ORDER BY count DESC, subpolicy_name
                """
            ).fetchall()
        }
        autofix_unknown_domain_guarded_cluster_split = {
            str(row["cluster"]): _int(row["count"])
            for row in con.execute(
                """
                SELECT COALESCE(NULLIF(cluster, ''), 'unknown') AS cluster, COUNT(*) AS count
                FROM ml_issue_autofix_unknown_domain_guarded_checkpoint_items
                WHERE checkpoint_run_id = 4
                  AND checkpoint_allowed = 1
                  AND COALESCE(TRIM(block_reason), '') = ''
                  AND checkpoint_action = 'cover_autofix_unknown_domain_false_reopen'
                  AND open_issue_count = 1
                  AND autofix_unknown_count = 1
                  AND current_confirmed_text_hash = current_output_text_hash
                GROUP BY COALESCE(NULLIF(cluster, ''), 'unknown')
                ORDER BY count DESC, cluster
                """
            ).fetchall()
        }
    else:
        autofix_unknown_domain_guarded = {
            "checkpoint_run_ids": "",
            "allowed_rows": 0,
            "distinct_allowed_segments": 0,
            "already_closed_before": 0,
            "pending_before": 0,
            "closed": 0,
            "blocked": 0,
            "net_gain": 0,
            "status": "unavailable",
        }
        autofix_unknown_domain_guarded_subpolicy_split = {}
        autofix_unknown_domain_guarded_cluster_split = {}

    autofix_unknown_domain_guarded_reviewed_tables_ready = (
        _table_exists(con, "ml_issue_autofix_unknown_domain_guarded_reviewed_checkpoint_runs")
        and _table_exists(con, "ml_issue_autofix_unknown_domain_guarded_reviewed_checkpoint_items")
    )
    if autofix_unknown_domain_guarded_reviewed_tables_ready:
        autofix_unknown_domain_guarded_reviewed = _one(
            con,
            """
            WITH checkpoint AS (
                SELECT *
                FROM ml_issue_autofix_unknown_domain_guarded_reviewed_checkpoint_runs
                WHERE finished_at IS NOT NULL
                  AND allowed_count > 0
            ),
            allowed_raw AS (
                SELECT
                    item.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY item.segment_id
                        ORDER BY item.run_id ASC, item.id ASC
                    ) AS segment_rank
                FROM ml_issue_autofix_unknown_domain_guarded_reviewed_checkpoint_items item
                JOIN checkpoint
                  ON checkpoint.id = item.run_id
                 AND checkpoint.ledger_run_id = item.ledger_run_id
                 AND checkpoint.source_queue_run_id = item.queue_run_id
                WHERE item.checkpoint_status = 'allowed'
            ),
            allowed AS (
                SELECT *
                FROM allowed_raw
                WHERE segment_rank = 1
            ),
            closed AS (
                SELECT COUNT(*) AS closed_count
                FROM allowed
                JOIN segment_state_items state
                  ON state.segment_id = allowed.segment_id
                 AND state.run_id = ?
                WHERE state.final_state = 'closed_auto_confirmed_autofix_unknown_domain_guarded_reviewed_lifecycle'
            )
            SELECT
                (SELECT GROUP_CONCAT(id) FROM checkpoint) AS checkpoint_run_ids,
                (SELECT COUNT(*) FROM allowed) AS candidates,
                COALESCE(closed.closed_count, 0) AS closed,
                MAX((SELECT COUNT(*) FROM allowed) - COALESCE(closed.closed_count, 0), 0) AS blocked,
                CASE
                    WHEN COALESCE(closed.closed_count, 0) > 0 THEN 'shadow_lifecycle_bridge_cumulative'
                    WHEN (SELECT COUNT(*) FROM allowed) > 0 THEN 'shadow_ready_cumulative'
                    ELSE 'unavailable'
                END AS status
            FROM closed
            """,
            (run_id,),
        )
    else:
        autofix_unknown_domain_guarded_reviewed = {
            "checkpoint_run_ids": "",
            "candidates": 0,
            "closed": 0,
            "blocked": 0,
            "status": "unavailable",
        }

    event_composition_tables_ready = (
        _table_exists(con, "ml_issue_autofix_unknown_event_composition_bridge_runs")
        and _table_exists(con, "ml_issue_autofix_unknown_event_composition_bridge_items")
    )
    if event_composition_tables_ready:
        autofix_unknown_event_composition = _one(
            con,
            """
            WITH proposal AS (
              SELECT *
              FROM ml_issue_autofix_unknown_event_composition_bridge_runs
              WHERE finished_at IS NOT NULL
                AND id = 2
                AND source_checkpoint_run_id = 2
                AND source_segment_state_run_id = 178
                AND source_ledger_run_id = 21
                AND ready_count = 460
                AND partial_count = 1136
                AND blocked_count = 0
                AND estimated_closed_gain = 460
                AND production_release_allowed = 0
              LIMIT 1
            ),
            closed AS (
              SELECT COUNT(*) AS closed_by_bridge
              FROM ml_issue_autofix_unknown_event_composition_bridge_items item
              JOIN proposal ON proposal.id = item.run_id
              JOIN segment_state_items state
                ON state.segment_id = item.segment_id
               AND state.run_id = ?
              WHERE state.final_state = 'closed_auto_confirmed_autofix_unknown_event_composition_lifecycle'
            )
            SELECT
              proposal.source_checkpoint_run_id AS checkpoint_id,
              proposal.id AS bridge_run_id,
              proposal.ready_count AS allowed,
              proposal.ready_count AS ready,
              proposal.partial_count AS partial,
              proposal.blocked_count AS blocked,
              COALESCE(closed.closed_by_bridge, 0) AS closed,
              CASE
                WHEN COALESCE(closed.closed_by_bridge, 0) > 0 THEN 'shadow_lifecycle_bridge'
                WHEN proposal.ready_count > 0 THEN 'shadow_ready'
                ELSE 'shadow_blocked'
              END AS status
            FROM proposal, closed
            """,
            (run_id,),
        )
    else:
        autofix_unknown_event_composition = {
            "checkpoint_id": 0,
            "bridge_run_id": 0,
            "allowed": 0,
            "ready": 0,
            "partial": 0,
            "blocked": 0,
            "closed": 0,
            "status": "unavailable",
        }

    event_semantic_companion_tables_ready = (
        _table_exists(con, "ml_issue_autofix_unknown_event_semantic_companion_bridge_runs")
        and _table_exists(con, "ml_issue_autofix_unknown_event_semantic_companion_bridge_items")
    )
    if event_semantic_companion_tables_ready:
        autofix_unknown_event_semantic_companion = _one(
            con,
            """
            WITH proposal AS (
              SELECT *
              FROM ml_issue_autofix_unknown_event_semantic_companion_bridge_runs
              WHERE finished_at IS NOT NULL
                AND ready_count > 0
                AND production_release_allowed = 0
              ORDER BY finished_at DESC, id DESC
              LIMIT 1
            ),
            closed AS (
              SELECT COUNT(*) AS closed_by_bridge
              FROM ml_issue_autofix_unknown_event_semantic_companion_bridge_items item
              JOIN proposal ON proposal.id = item.run_id
              JOIN segment_state_items state
                ON state.segment_id = item.segment_id
               AND state.run_id = ?
              WHERE state.final_state = 'closed_auto_confirmed_autofix_unknown_event_semantic_companion_lifecycle'
            )
            SELECT
              proposal.id AS bridge_run_id,
              proposal.source_event_bridge_run_id AS event_bridge_run_id,
              proposal.candidate_count AS candidates,
              proposal.ready_count AS ready,
              proposal.blocked_count AS blocked,
              COALESCE(closed.closed_by_bridge, 0) AS closed,
              CASE
                WHEN COALESCE(closed.closed_by_bridge, 0) > 0 THEN 'shadow_lifecycle_bridge'
                WHEN proposal.ready_count > 0 THEN 'shadow_ready'
                ELSE 'shadow_blocked'
              END AS status
            FROM proposal, closed
            """,
            (run_id,),
        )
    else:
        autofix_unknown_event_semantic_companion = {
            "bridge_run_id": 0,
            "event_bridge_run_id": 0,
            "candidates": 0,
            "ready": 0,
            "blocked": 0,
            "closed": 0,
            "status": "unavailable",
        }

    event_surface_short_label_companion_tables_ready = (
        _table_exists(con, "ml_issue_event_surface_short_label_companion_bridge_runs")
        and _table_exists(con, "ml_issue_event_surface_short_label_companion_bridge_items")
    )
    if event_surface_short_label_companion_tables_ready:
        event_surface_short_label_companion = _one(
            con,
            """
            WITH bridge AS (
              SELECT *
              FROM ml_issue_event_surface_short_label_companion_bridge_runs
              WHERE finished_at IS NOT NULL
                AND id = 1
                AND rule_version = 'issue_event_surface_short_label_companion_bridge_v1'
                AND bridge_name = 'event_surface_short_label_companion_bridge_v1'
                AND source_event_checkpoint_run_id = 13
                AND source_coverage_run_id = 175
                AND source_ledger_run_id = 40
                AND current_segment_state_run_id = 242
                AND candidate_count = 376
                AND ready_count = 376
                AND blocked_count = 0
                AND estimated_closed_gain = 376
                AND production_release_allowed = 0
              LIMIT 1
            ),
            closed AS (
              SELECT COUNT(*) AS closed_by_bridge
              FROM ml_issue_event_surface_short_label_companion_bridge_items item
              JOIN bridge ON bridge.id = item.run_id
              JOIN segment_state_items state
                ON state.segment_id = item.segment_id
               AND state.run_id = ?
              WHERE state.final_state = 'closed_auto_confirmed_event_surface_short_label_companion_lifecycle'
            )
            SELECT
              bridge.id AS bridge_run_id,
              bridge.candidate_count AS candidates,
              bridge.ready_count AS ready,
              bridge.blocked_count AS blocked,
              bridge.estimated_closed_gain,
              COALESCE(closed.closed_by_bridge, 0) AS closed,
              CASE
                WHEN COALESCE(closed.closed_by_bridge, 0) > 0 THEN 'shadow_lifecycle_bridge'
                WHEN bridge.ready_count > 0 THEN 'shadow_ready'
                ELSE 'shadow_blocked'
              END AS status
            FROM bridge, closed
            """,
            (run_id,),
        )
    else:
        event_surface_short_label_companion = {
            "bridge_run_id": 0,
            "candidates": 0,
            "ready": 0,
            "blocked": 0,
            "estimated_closed_gain": 0,
            "closed": 0,
            "status": "unavailable",
        }

    pure_no_token_domain_tables_ready = (
        _table_exists(con, "ml_issue_short_label_pure_no_token_checkpoint_runs")
        and _table_exists(con, "ml_issue_short_label_pure_no_token_checkpoint_items")
    )
    if pure_no_token_domain_tables_ready:
        short_label_pure_no_token_domain = _one(
            con,
            """
            WITH checkpoint AS (
              SELECT *
              FROM ml_issue_short_label_pure_no_token_checkpoint_runs
              WHERE finished_at IS NOT NULL
                AND (
                      (id = 11 AND shadow_run_id = 12)
                   OR (id = 14 AND shadow_run_id = 15)
                )
                AND rule_version = 'issue_short_label_pure_no_token_checkpoint_v1'
                AND checkpoint_status = 'ready_for_lifecycle_policy'
                AND production_release_allowed = 0
                AND policy_name = 'short_label_pure_no_token_nominal_shadow'
                AND agent_key = 'micro_short_label_style'
            ),
            closed AS (
              SELECT COUNT(DISTINCT item.segment_id) AS closed_by_bridge
              FROM ml_issue_short_label_pure_no_token_checkpoint_items item
              JOIN checkpoint
                ON checkpoint.id = item.checkpoint_run_id
               AND checkpoint.shadow_run_id = item.shadow_run_id
              JOIN segment_state_items state
                ON state.segment_id = item.segment_id
               AND state.run_id = ?
              WHERE state.final_state = 'closed_auto_confirmed_short_label_pure_no_token_domain_lifecycle'
            )
            SELECT
              GROUP_CONCAT(checkpoint.id) AS checkpoint_id,
              GROUP_CONCAT(checkpoint.shadow_run_id) AS shadow_run_id,
              SUM(checkpoint.candidate_count) AS candidates,
              SUM(checkpoint.checkpoint_allowed_count) AS allowed,
              COALESCE(closed.closed_by_bridge, 0) AS closed,
              CASE
                WHEN SUM(checkpoint.candidate_count) - COALESCE(closed.closed_by_bridge, 0) > 0
                  THEN SUM(checkpoint.candidate_count) - COALESCE(closed.closed_by_bridge, 0)
                ELSE 0
              END AS blocked,
              CASE
                WHEN COALESCE(closed.closed_by_bridge, 0) > 0 THEN 'shadow_lifecycle_bridge'
                WHEN SUM(checkpoint.checkpoint_allowed_count) > 0 THEN 'shadow_ready'
                ELSE 'shadow_blocked'
              END AS status
            FROM checkpoint, closed
            """,
            (run_id,),
        )
    else:
        short_label_pure_no_token_domain = {
            "checkpoint_id": 0,
            "shadow_run_id": 0,
            "candidates": 0,
            "allowed": 0,
            "closed": 0,
            "blocked": 0,
            "status": "unavailable",
        }

    dynamic_ck3_pattern_tables_ready = (
        _table_exists(con, "ml_issue_dynamic_ck3_pattern_checkpoint_runs")
        and _table_exists(con, "ml_issue_dynamic_ck3_pattern_checkpoint_items")
    )
    if dynamic_ck3_pattern_tables_ready:
        dynamic_ck3_pattern = _one(
            con,
            """
            WITH checkpoint AS (
              SELECT *
              FROM ml_issue_dynamic_ck3_pattern_checkpoint_runs
              WHERE finished_at IS NOT NULL
                AND id >= 13
                AND rule_version = 'issue_dynamic_ck3_pattern_checkpoint_v1'
                AND checkpoint_status = 'ready_for_shadow_lifecycle_policy'
                AND policy_name = 'dynamic_ck3_pattern_shadow'
                AND agent_key = 'micro_dynamic_ck3_expression'
                AND checkpoint_allowed_count > 0
            ),
            item_raw AS (
              SELECT
                item.*,
                ROW_NUMBER() OVER (
                  PARTITION BY item.segment_id
                  ORDER BY item.checkpoint_run_id ASC, item.id ASC
                ) AS segment_rank
              FROM ml_issue_dynamic_ck3_pattern_checkpoint_items item
              JOIN checkpoint
                ON checkpoint.id = item.checkpoint_run_id
               AND checkpoint.pattern_run_id = item.pattern_run_id
              WHERE item.subpolicy_name = 'dynamic_rule_tooltip_label'
                AND item.checkpoint_action = 'checked_dynamic_ck3_pattern_shadow'
            ),
            item_eval AS (
              SELECT
                item.segment_id,
                item.checkpoint_allowed,
                item.block_reason,
                CASE
                  WHEN state.final_state = 'closed_auto_confirmed_dynamic_ck3_pattern_shadow_lifecycle' THEN 'closed'
                  ELSE 'blocked'
                END AS bridge_result
              FROM item_raw item
              LEFT JOIN segment_state_items state
                ON state.segment_id = item.segment_id
               AND state.run_id = ?
              WHERE item.segment_rank = 1
            )
            SELECT
              (SELECT GROUP_CONCAT(id) FROM checkpoint) AS checkpoint_run_ids,
              (SELECT SUM(total_candidates) FROM checkpoint) AS candidates,
              (SELECT SUM(checkpoint_allowed_count) FROM checkpoint) AS allowed,
              SUM(CASE WHEN bridge_result = 'closed' THEN 1 ELSE 0 END) AS closed,
              (SELECT SUM(total_candidates) FROM checkpoint) - SUM(CASE WHEN bridge_result = 'closed' THEN 1 ELSE 0 END) AS blocked,
              CASE
                WHEN SUM(CASE WHEN bridge_result = 'closed' THEN 1 ELSE 0 END) > 0 THEN 'shadow_lifecycle_bridge_cumulative'
                WHEN (SELECT SUM(checkpoint_allowed_count) FROM checkpoint) > 0 THEN 'shadow_ready_cumulative'
                ELSE 'unavailable'
              END AS status
            FROM item_eval
            """,
            (run_id,),
        )
        dynamic_ck3_pattern_subpolicies_closed = {
            str(row["subpolicy_name"]): _int(row["count"])
            for row in con.execute(
                """
                WITH checkpoint AS (
                  SELECT *
                  FROM ml_issue_dynamic_ck3_pattern_checkpoint_runs
                  WHERE finished_at IS NOT NULL
                    AND id >= 13
                    AND rule_version = 'issue_dynamic_ck3_pattern_checkpoint_v1'
                    AND checkpoint_status = 'ready_for_shadow_lifecycle_policy'
                    AND policy_name = 'dynamic_ck3_pattern_shadow'
                    AND agent_key = 'micro_dynamic_ck3_expression'
                    AND checkpoint_allowed_count > 0
                )
                SELECT item.subpolicy_name, COUNT(*) AS count
                FROM ml_issue_dynamic_ck3_pattern_checkpoint_items item
                JOIN checkpoint
                  ON checkpoint.id = item.checkpoint_run_id
                 AND checkpoint.pattern_run_id = item.pattern_run_id
                JOIN segment_state_items state
                  ON state.segment_id = item.segment_id
                 AND state.run_id = ?
                WHERE state.final_state = 'closed_auto_confirmed_dynamic_ck3_pattern_shadow_lifecycle'
                GROUP BY item.subpolicy_name
                ORDER BY count DESC, item.subpolicy_name
                """,
                (run_id,),
            ).fetchall()
        }
    else:
        dynamic_ck3_pattern = {
            "checkpoint_run_ids": "",
            "candidates": 0,
            "allowed": 0,
            "closed": 0,
            "blocked": 0,
            "status": "unavailable",
        }
        dynamic_ck3_pattern_subpolicies_closed = {}

    short_label_dynamic_delegate_tables_ready = (
        _table_exists(con, "ml_issue_short_label_dynamic_delegate_checkpoint_runs")
        and _table_exists(con, "ml_issue_short_label_dynamic_delegate_checkpoint_items")
    )
    if short_label_dynamic_delegate_tables_ready:
        short_label_dynamic_delegate = _one(
            con,
            """
            WITH previous_run AS (
              SELECT id
              FROM segment_state_runs
              WHERE finished_at IS NOT NULL
                AND id < ?
              ORDER BY id DESC
              LIMIT 1
            ),
            checkpoint AS (
              SELECT *
              FROM ml_issue_short_label_dynamic_delegate_checkpoint_runs
              WHERE finished_at IS NOT NULL
                AND rule_version = 'issue_short_label_dynamic_delegate_checkpoint_v1'
                AND checkpoint_status = 'checkpoint_ready'
                AND policy_name = 'short_label_dynamic_delegate_shadow'
                AND policy_status = 'shadow'
                AND agent_key = 'micro_short_label_style'
                AND allowed_count > 0
                AND id IN (1, 2)
            ),
            allowed_rows AS (
              SELECT item.*
              FROM ml_issue_short_label_dynamic_delegate_checkpoint_items item
              JOIN checkpoint
                ON checkpoint.id = item.checkpoint_run_id
               AND checkpoint.sublane_run_id = item.sublane_run_id
               AND checkpoint.ledger_run_id = item.ledger_run_id
              WHERE item.checkpoint_allowed = 1
                AND COALESCE(TRIM(item.block_reason), '') = ''
                AND item.subpolicy_name = 'short_label_dynamic_expression_delegate'
                AND item.checkpoint_action = 'cover_short_label_dynamic_delegate_to_dynamic_ck3'
            ),
            allowed AS (
              SELECT *
              FROM (
                SELECT
                  allowed_rows.*,
                  ROW_NUMBER() OVER (
                    PARTITION BY allowed_rows.segment_id
                    ORDER BY allowed_rows.checkpoint_run_id ASC, allowed_rows.id ASC
                  ) AS segment_rank
                FROM allowed_rows
              )
              WHERE segment_rank = 1
            ),
            item_eval AS (
              SELECT
                allowed.segment_id,
                CASE
                  WHEN current_state.final_state = 'closed_auto_confirmed_short_label_dynamic_delegate_lifecycle'
                    THEN 'closed'
                  WHEN previous_state.state_group = 'closed'
                    THEN 'already_closed_before'
                  WHEN previous_state.review_state IN ('human_locked', 'human_confirmed')
                    OR COALESCE(confirmation.locked, 0) = 1
                    THEN 'human_locked_or_confirmed'
                  WHEN previous_state.final_state != 'reopen_auto_confirmed_autofix'
                    THEN 'state_not_reopen_auto_confirmed_autofix'
                  WHEN previous_state.state_group != 'pending'
                    THEN 'state_not_pending'
                  WHEN previous_state.review_state != 'auto_confirmed'
                    THEN 'review_state_not_auto_confirmed'
                  WHEN previous_state.confirmed_matches_output != 1
                    THEN 'confirmation_output_mismatch'
                  WHEN previous_state.needs_output_apply != 0
                    THEN 'needs_output_apply'
                  WHEN previous_state.needs_reopen != 1
                    THEN 'needs_reopen_not_set'
                  WHEN COALESCE(issue_summary.high_issue_count, 0) > 0
                    OR previous_state.final_state = 'pending_blocked_structure'
                    THEN 'high_issue_or_structure_block'
                  ELSE 'eligible_but_not_closed'
                END AS bridge_result
              FROM allowed
              LEFT JOIN previous_run ON 1 = 1
              LEFT JOIN segment_state_items previous_state
                ON previous_state.segment_id = allowed.segment_id
               AND previous_state.run_id = previous_run.id
              LEFT JOIN segment_state_items current_state
                ON current_state.segment_id = allowed.segment_id
               AND current_state.run_id = ?
              LEFT JOIN segment_confirmations confirmation
                ON confirmation.segment_id = allowed.segment_id
              LEFT JOIN (
                SELECT
                  segment_id,
                  COUNT(*) AS issue_count,
                  SUM(CASE WHEN lower(severity) IN ('high', 'error', 'critical') THEN 1 ELSE 0 END) AS high_issue_count
                FROM issues
                GROUP BY segment_id
              ) issue_summary ON issue_summary.segment_id = allowed.segment_id
            )
            SELECT
              (SELECT GROUP_CONCAT(id) FROM checkpoint) AS checkpoint_run_ids,
              (SELECT COUNT(*) FROM allowed_rows) AS allowed_rows,
              (SELECT COUNT(*) FROM allowed) AS distinct_allowed_segments,
              SUM(CASE WHEN bridge_result = 'already_closed_before' THEN 1 ELSE 0 END) AS already_closed_before,
              SUM(CASE WHEN bridge_result IN ('closed', 'eligible_but_not_closed') THEN 1 ELSE 0 END) AS pending_before,
              SUM(CASE WHEN bridge_result = 'closed' THEN 1 ELSE 0 END) AS closed,
              SUM(CASE WHEN bridge_result NOT IN ('closed', 'already_closed_before') THEN 1 ELSE 0 END) AS blocked,
              SUM(CASE WHEN bridge_result = 'closed' THEN 1 ELSE 0 END) AS net_gain,
              CASE
                WHEN SUM(CASE WHEN bridge_result = 'closed' THEN 1 ELSE 0 END) > 0
                  THEN 'shadow_lifecycle_bridge_cumulative'
                WHEN (SELECT COUNT(*) FROM allowed) > 0
                  THEN 'shadow_ready_cumulative'
                ELSE 'unavailable'
              END AS status
            FROM item_eval
            """,
            (run_id, run_id),
        )
        short_label_dynamic_delegate_kind_split = {
            str(row["dynamic_issue_kind"]): _int(row["count"])
            for row in con.execute(
                """
                WITH checkpoint AS (
                  SELECT *
                  FROM ml_issue_short_label_dynamic_delegate_checkpoint_runs
                  WHERE finished_at IS NOT NULL
                    AND rule_version = 'issue_short_label_dynamic_delegate_checkpoint_v1'
                    AND checkpoint_status = 'checkpoint_ready'
                    AND policy_name = 'short_label_dynamic_delegate_shadow'
                    AND policy_status = 'shadow'
                    AND agent_key = 'micro_short_label_style'
                    AND allowed_count > 0
                    AND id IN (1, 2)
                ),
                allowed AS (
                  SELECT *
                  FROM (
                    SELECT
                      item.*,
                      ROW_NUMBER() OVER (
                        PARTITION BY item.segment_id
                        ORDER BY item.checkpoint_run_id ASC, item.id ASC
                      ) AS segment_rank
                    FROM ml_issue_short_label_dynamic_delegate_checkpoint_items item
                    JOIN checkpoint
                      ON checkpoint.id = item.checkpoint_run_id
                     AND checkpoint.sublane_run_id = item.sublane_run_id
                     AND checkpoint.ledger_run_id = item.ledger_run_id
                    WHERE item.checkpoint_allowed = 1
                      AND COALESCE(TRIM(item.block_reason), '') = ''
                      AND item.subpolicy_name = 'short_label_dynamic_expression_delegate'
                      AND item.checkpoint_action = 'cover_short_label_dynamic_delegate_to_dynamic_ck3'
                  )
                  WHERE segment_rank = 1
                )
                SELECT COALESCE(NULLIF(dynamic_issue_kind, ''), 'unknown') AS dynamic_issue_kind, COUNT(*) AS count
                FROM allowed
                GROUP BY COALESCE(NULLIF(dynamic_issue_kind, ''), 'unknown')
                ORDER BY count DESC, dynamic_issue_kind
                """
            ).fetchall()
        }
    else:
        short_label_dynamic_delegate = {
            "checkpoint_run_ids": "",
            "allowed_rows": 0,
            "distinct_allowed_segments": 0,
            "already_closed_before": 0,
            "pending_before": 0,
            "closed": 0,
            "blocked": 0,
            "net_gain": 0,
            "status": "unavailable",
        }
        short_label_dynamic_delegate_kind_split = {}

    short_label_single_issue_system_tooltip_tables_ready = (
        _table_exists(con, "ml_issue_short_label_single_issue_system_tooltip_checkpoint_runs")
        and _table_exists(con, "ml_issue_short_label_single_issue_system_tooltip_checkpoint_items")
    )
    if short_label_single_issue_system_tooltip_tables_ready:
        short_label_single_issue_system_tooltip = _one(
            con,
            """
            WITH checkpoint AS (
              SELECT *
              FROM ml_issue_short_label_single_issue_system_tooltip_checkpoint_runs
              WHERE finished_at IS NOT NULL
                AND rule_version = 'issue_short_label_single_issue_system_tooltip_checkpoint_v1'
                AND checkpoint_status = 'ready_for_shadow_lifecycle_policy'
                AND agent_key = 'micro_short_label_style'
                AND issue_family = 'short_label_style_microagent'
                AND checkpoint_allowed_count > 0
                AND id IN (1)
            ),
            allowed_rows AS (
              SELECT item.*
              FROM ml_issue_short_label_single_issue_system_tooltip_checkpoint_items item
              JOIN checkpoint
                ON checkpoint.id = item.checkpoint_run_id
               AND checkpoint.ledger_run_id = item.ledger_run_id
               AND checkpoint.segment_state_run_id = item.segment_state_run_id
              WHERE item.checkpoint_allowed = 1
                AND COALESCE(TRIM(item.block_reason), '') = ''
                AND item.profile = 'system_tooltip_single_issue_clean'
                AND item.checkpoint_action = 'close_short_label_single_issue_system_tooltip'
                AND item.token_count >= 1
                AND item.word_count <= 8
                AND (
                      item.relative_path = 'effects_l_spanish.yml'
                   OR item.relative_path LIKE 'triggers/%'
                )
            ),
            allowed AS (
              SELECT *
              FROM (
                SELECT
                  allowed_rows.*,
                  ROW_NUMBER() OVER (
                    PARTITION BY allowed_rows.segment_id
                    ORDER BY allowed_rows.checkpoint_run_id ASC, allowed_rows.id ASC
                  ) AS segment_rank
                FROM allowed_rows
              )
              WHERE segment_rank = 1
            ),
            ledger_summary AS (
              SELECT
                run_id,
                segment_id,
                COUNT(*) AS ledger_issue_count,
                SUM(CASE WHEN lower(COALESCE(issue_severity, '')) IN ('high', 'error', 'critical') THEN 1 ELSE 0 END) AS high_ledger_issue_count
              FROM ml_issue_ledger_items
              WHERE status = 'open'
              GROUP BY run_id, segment_id
            ),
            item_eval AS (
              SELECT
                allowed.segment_id,
                CASE
                  WHEN previous_state.state_group = 'closed'
                    THEN 'already_closed_before'
                  WHEN current_state.final_state = 'closed_auto_confirmed_short_label_single_issue_system_tooltip_lifecycle'
                    THEN 'closed'
                  WHEN previous_state.review_state IN ('human_locked', 'human_confirmed')
                    OR COALESCE(confirmation.locked, 0) = 1
                    THEN 'human_locked_or_confirmed'
                  WHEN previous_state.final_state != 'reopen_auto_confirmed_autofix'
                    THEN 'state_not_reopen_auto_confirmed_autofix'
                  WHEN previous_state.state_group != 'pending'
                    THEN 'state_not_pending'
                  WHEN previous_state.review_state != 'auto_confirmed'
                    THEN 'review_state_not_auto_confirmed'
                  WHEN previous_state.confirmed_matches_output != 1
                    THEN 'confirmation_output_mismatch'
                  WHEN previous_state.needs_output_apply != 0
                    THEN 'needs_output_apply'
                  WHEN previous_state.needs_reopen != 1
                    THEN 'needs_reopen_not_set'
                  WHEN ledger_item.issue_family != 'short_label_style_microagent'
                    OR ledger_item.issue_kind != 'short_or_compact_label_reopened'
                    THEN 'ledger_issue_not_short_label_single_issue'
                  WHEN COALESCE(ledger_summary.ledger_issue_count, 0) != 1
                    THEN 'other_open_issues_remain'
                  WHEN COALESCE(ledger_summary.high_ledger_issue_count, 0) > 0
                    OR COALESCE(issue_summary.high_issue_count, 0) > 0
                    THEN 'high_issue_or_structure_block'
                  ELSE 'eligible_but_not_closed'
                END AS bridge_result
              FROM allowed
              LEFT JOIN segment_state_items previous_state
                ON previous_state.segment_id = allowed.segment_id
               AND previous_state.run_id = allowed.segment_state_run_id
              LEFT JOIN segment_state_items current_state
                ON current_state.segment_id = allowed.segment_id
               AND current_state.run_id = ?
              LEFT JOIN segment_confirmations confirmation
                ON confirmation.segment_id = allowed.segment_id
              LEFT JOIN ml_issue_ledger_items ledger_item
                ON ledger_item.id = allowed.ledger_item_id
               AND ledger_item.run_id = allowed.ledger_run_id
               AND ledger_item.segment_id = allowed.segment_id
              LEFT JOIN ledger_summary
                ON ledger_summary.run_id = allowed.ledger_run_id
               AND ledger_summary.segment_id = allowed.segment_id
              LEFT JOIN (
                SELECT
                  segment_id,
                  COUNT(*) AS issue_count,
                  SUM(CASE WHEN lower(severity) IN ('high', 'error', 'critical') THEN 1 ELSE 0 END) AS high_issue_count
                FROM issues
                GROUP BY segment_id
              ) issue_summary ON issue_summary.segment_id = allowed.segment_id
            )
            SELECT
              (SELECT GROUP_CONCAT(id) FROM checkpoint) AS checkpoint_run_ids,
              (SELECT COUNT(*) FROM allowed_rows) AS allowed_rows,
              (SELECT COUNT(*) FROM allowed) AS distinct_allowed_segments,
              SUM(CASE WHEN bridge_result = 'already_closed_before' THEN 1 ELSE 0 END) AS already_closed_before,
              SUM(CASE WHEN bridge_result IN ('closed', 'eligible_but_not_closed') THEN 1 ELSE 0 END) AS pending_before,
              SUM(CASE WHEN bridge_result = 'closed' THEN 1 ELSE 0 END) AS closed,
              SUM(CASE WHEN bridge_result NOT IN ('closed', 'already_closed_before') THEN 1 ELSE 0 END) AS blocked,
              SUM(CASE WHEN bridge_result = 'closed' THEN 1 ELSE 0 END) AS net_gain,
              CASE
                WHEN SUM(CASE WHEN bridge_result = 'closed' THEN 1 ELSE 0 END) > 0
                  THEN 'shadow_lifecycle_bridge_cumulative'
                WHEN (SELECT COUNT(*) FROM allowed) > 0
                  THEN 'shadow_ready_cumulative'
                ELSE 'unavailable'
              END AS status
            FROM item_eval
            """,
            (run_id,),
        )
        short_label_single_issue_system_tooltip_profile_split = {
            str(row["profile"]): _int(row["count"])
            for row in con.execute(
                """
                SELECT COALESCE(NULLIF(profile, ''), 'unknown') AS profile, COUNT(*) AS count
                FROM ml_issue_short_label_single_issue_system_tooltip_checkpoint_items
                WHERE checkpoint_run_id = 1
                  AND checkpoint_allowed = 1
                  AND COALESCE(TRIM(block_reason), '') = ''
                  AND profile = 'system_tooltip_single_issue_clean'
                  AND checkpoint_action = 'close_short_label_single_issue_system_tooltip'
                  AND token_count >= 1
                  AND word_count <= 8
                  AND (
                        relative_path = 'effects_l_spanish.yml'
                     OR relative_path LIKE 'triggers/%'
                  )
                GROUP BY COALESCE(NULLIF(profile, ''), 'unknown')
                ORDER BY count DESC, profile
                """
            ).fetchall()
        }
        short_label_single_issue_system_tooltip_path_split = {
            str(row["relative_path"]): _int(row["count"])
            for row in con.execute(
                """
                SELECT relative_path, COUNT(*) AS count
                FROM ml_issue_short_label_single_issue_system_tooltip_checkpoint_items
                WHERE checkpoint_run_id = 1
                  AND checkpoint_allowed = 1
                  AND COALESCE(TRIM(block_reason), '') = ''
                  AND profile = 'system_tooltip_single_issue_clean'
                  AND checkpoint_action = 'close_short_label_single_issue_system_tooltip'
                  AND token_count >= 1
                  AND word_count <= 8
                  AND (
                        relative_path = 'effects_l_spanish.yml'
                     OR relative_path LIKE 'triggers/%'
                  )
                GROUP BY relative_path
                ORDER BY count DESC, relative_path
                """
            ).fetchall()
        }
    else:
        short_label_single_issue_system_tooltip = {
            "checkpoint_run_ids": "",
            "allowed_rows": 0,
            "distinct_allowed_segments": 0,
            "already_closed_before": 0,
            "pending_before": 0,
            "closed": 0,
            "blocked": 0,
            "net_gain": 0,
            "status": "unavailable",
        }
        short_label_single_issue_system_tooltip_profile_split = {}
        short_label_single_issue_system_tooltip_path_split = {}

    short_label_tokenized_ui_tables_ready = (
        _table_exists(con, "ml_issue_short_label_tokenized_ui_false_reopen_checkpoint_runs")
        and _table_exists(con, "ml_issue_short_label_tokenized_ui_false_reopen_checkpoint_items")
    )
    if short_label_tokenized_ui_tables_ready:
        short_label_tokenized_ui = _one(
            con,
            """
            WITH checkpoint AS (
              SELECT *
              FROM ml_issue_short_label_tokenized_ui_false_reopen_checkpoint_runs
              WHERE finished_at IS NOT NULL
                AND rule_version = 'issue_short_label_tokenized_ui_false_reopen_checkpoint_v1'
                AND checkpoint_status = 'ready_for_shadow_lifecycle'
                AND agent_key = 'micro_short_label_tokenized_ui'
                AND issue_family = 'short_label_style_microagent'
                AND checkpoint_allowed_count > 0
                AND id = 2
            ),
            allowed AS (
              SELECT *
              FROM (
                SELECT
                  item.*,
                  ROW_NUMBER() OVER (
                    PARTITION BY item.segment_id
                    ORDER BY item.checkpoint_run_id ASC, item.id ASC
                  ) AS segment_rank
                FROM ml_issue_short_label_tokenized_ui_false_reopen_checkpoint_items item
                JOIN checkpoint
                  ON checkpoint.id = item.checkpoint_run_id
                 AND checkpoint.ledger_run_id = item.ledger_run_id
                 AND checkpoint.segment_state_run_id = item.segment_state_run_id
                WHERE item.checkpoint_run_id = 2
                  AND item.checkpoint_allowed = 1
                  AND item.checkpoint_action = 'cover_short_label_tokenized_ui_false_reopen'
                  AND COALESCE(TRIM(item.block_reason), '') = ''
              )
              WHERE segment_rank = 1
            ),
            closed AS (
              SELECT COUNT(*) AS closed
              FROM allowed
              JOIN segment_state_items state
                ON state.segment_id = allowed.segment_id
               AND state.run_id = ?
              WHERE state.final_state = 'closed_auto_confirmed_short_label_tokenized_ui_false_reopen_lifecycle'
            )
            SELECT
              (SELECT GROUP_CONCAT(id) FROM checkpoint) AS checkpoint_run_ids,
              COALESCE((SELECT SUM(total_candidates) FROM checkpoint), 0) AS candidates,
              COALESCE((SELECT COUNT(*) FROM allowed), 0) AS allowed,
              COALESCE(closed.closed, 0) AS closed,
              CASE
                WHEN COALESCE((SELECT COUNT(*) FROM allowed), 0) - COALESCE(closed.closed, 0) > 0
                  THEN COALESCE((SELECT COUNT(*) FROM allowed), 0) - COALESCE(closed.closed, 0)
                ELSE 0
              END AS blocked,
              CASE
                WHEN COALESCE(closed.closed, 0) > 0 THEN 'shadow_lifecycle_bridge'
                WHEN COALESCE((SELECT COUNT(*) FROM allowed), 0) > 0 THEN 'shadow_ready'
                ELSE 'unavailable'
              END AS status
            FROM closed
            """,
            (run_id,),
        )
    else:
        short_label_tokenized_ui = {
            "checkpoint_run_ids": "",
            "candidates": 0,
            "allowed": 0,
            "closed": 0,
            "blocked": 0,
            "status": "unavailable",
        }

    gender_longform_false_reopen_tables_ready = (
        _table_exists(con, "ml_issue_gender_longform_false_reopen_checkpoint_runs")
        and _table_exists(con, "ml_issue_gender_longform_false_reopen_checkpoint_items")
    )
    if gender_longform_false_reopen_tables_ready:
        gender_longform_false_reopen = _one(
            con,
            """
            WITH checkpoint AS (
              SELECT *
              FROM ml_issue_gender_longform_false_reopen_checkpoint_runs
              WHERE finished_at IS NOT NULL
                AND rule_version = 'issue_gender_longform_false_reopen_checkpoint_v1'
                AND checkpoint_status = 'ready_for_shadow_lifecycle_bridge'
                AND production_release_allowed = 0
                AND checkpoint_allowed_count > 0
            ),
            closed AS (
              SELECT COUNT(DISTINCT item.segment_id) AS closed_by_bridge
              FROM ml_issue_gender_longform_false_reopen_checkpoint_items item
              JOIN checkpoint
                ON checkpoint.id = item.checkpoint_run_id
               AND checkpoint.route_run_id = item.route_run_id
               AND checkpoint.diagnostic_run_id = item.diagnostic_run_id
              JOIN segment_state_items state
                ON state.segment_id = item.segment_id
               AND state.run_id = ?
              WHERE state.final_state = 'closed_auto_confirmed_gender_longform_false_reopen_context'
            )
            SELECT
              GROUP_CONCAT(checkpoint.id) AS checkpoint_id,
              SUM(checkpoint.total_candidates) AS candidates,
              COALESCE(closed.closed_by_bridge, 0) AS closed,
              CASE
                WHEN SUM(checkpoint.total_candidates) - COALESCE(closed.closed_by_bridge, 0) > 0
                  THEN SUM(checkpoint.total_candidates) - COALESCE(closed.closed_by_bridge, 0)
                ELSE 0
              END AS blocked,
              CASE
                WHEN COALESCE(closed.closed_by_bridge, 0) > 0 THEN 'shadow_lifecycle_bridge'
                WHEN SUM(checkpoint.checkpoint_allowed_count) > 0 THEN 'shadow_ready'
                ELSE 'shadow_blocked'
              END AS status
            FROM checkpoint, closed
            """,
            (run_id,),
        )
        gender_longform_false_reopen_routes = [
            dict(row)
            for row in con.execute(
                """
                WITH checkpoint AS (
                  SELECT *
                  FROM ml_issue_gender_longform_false_reopen_checkpoint_runs
                  WHERE finished_at IS NOT NULL
                    AND rule_version = 'issue_gender_longform_false_reopen_checkpoint_v1'
                    AND checkpoint_status = 'ready_for_shadow_lifecycle_bridge'
                    AND production_release_allowed = 0
                    AND checkpoint_allowed_count > 0
                )
                SELECT item.route_key, COUNT(*) AS count
                FROM ml_issue_gender_longform_false_reopen_checkpoint_items item
                JOIN checkpoint
                  ON checkpoint.id = item.checkpoint_run_id
                 AND checkpoint.route_run_id = item.route_run_id
                 AND checkpoint.diagnostic_run_id = item.diagnostic_run_id
                JOIN segment_state_items state
                  ON state.segment_id = item.segment_id
                 AND state.run_id = ?
                WHERE state.final_state = 'closed_auto_confirmed_gender_longform_false_reopen_context'
                GROUP BY item.route_key
                ORDER BY count DESC, item.route_key
                """,
                (run_id,),
            ).fetchall()
        ]
    else:
        gender_longform_false_reopen = {
            "checkpoint_id": 0,
            "candidates": 0,
            "closed": 0,
            "blocked": 0,
            "status": "unavailable",
        }
        gender_longform_false_reopen_routes = []

    pure_no_token_semantic_tables_ready = (
        _table_exists(con, "ml_issue_short_label_pure_no_token_segment_lifecycle_bridge_proposal_runs")
        and _table_exists(con, "ml_issue_short_label_pure_no_token_segment_lifecycle_bridge_proposal_items")
    )
    if pure_no_token_semantic_tables_ready:
        short_label_pure_no_token_semantic = _one(
            con,
            """
            WITH proposal AS (
              SELECT *
              FROM ml_issue_short_label_pure_no_token_segment_lifecycle_bridge_proposal_runs
              WHERE finished_at IS NOT NULL
                AND id = 9
                AND bridge_status = 'proposal_shadow'
                AND production_release_allowed = 0
                AND bridge_candidate = 0
                AND ready_count > 0
                AND source_coverage_run_id = 143
                AND source_checkpoint_run_id = 6
              LIMIT 1
            ),
            closed AS (
              SELECT COUNT(*) AS closed_by_bridge
              FROM ml_issue_short_label_pure_no_token_segment_lifecycle_bridge_proposal_items item
              JOIN proposal ON proposal.id = item.run_id
              JOIN segment_state_items state
                ON state.segment_id = item.segment_id
               AND state.run_id = ?
              WHERE state.final_state = 'closed_auto_confirmed_short_label_pure_no_token_semantic_lifecycle'
            )
            SELECT
              proposal.id AS bridge_run_id,
              proposal.source_coverage_run_id AS coverage_run_id,
              proposal.source_checkpoint_run_id AS checkpoint_id,
              proposal.ready_count AS ready,
              proposal.blocked_count AS blocked,
              COALESCE(closed.closed_by_bridge, 0) AS closed,
              proposal.estimated_closed_gain AS estimated_closed_gain,
              CASE
                WHEN COALESCE(closed.closed_by_bridge, 0) > 0 THEN 'shadow_lifecycle_bridge'
                WHEN proposal.ready_count > 0 THEN 'shadow_ready'
                ELSE 'shadow_blocked'
              END AS status
            FROM proposal, closed
            """,
            (run_id,),
        )
    else:
        short_label_pure_no_token_semantic = {
            "bridge_run_id": 0,
            "coverage_run_id": 0,
            "checkpoint_id": 0,
            "ready": 0,
            "blocked": 0,
            "closed": 0,
            "estimated_closed_gain": 0,
            "status": "unavailable",
        }

    governed_bridge_pending = _int(select_cstring.get("pending"))
    model_watch = _int(
        _one(
            con,
            f"""
            SELECT COUNT(*) AS total
            FROM segment_state_items s
            WHERE s.run_id = ?
              AND s.final_state = 'reopen_auto_confirmed_autofix'
              AND s.confirmed_matches_output = 1
              AND s.output_state = 'output_present'
              AND s.segment_id NOT IN ({bridge_pending_sql})
              AND NOT EXISTS (
                SELECT 1
                FROM issues i
                WHERE i.segment_id = s.segment_id
                  AND lower(i.severity) IN ('high', 'error', 'critical')
              )
            """,
            (run_id, run_id),
        ).get("total")
    )
    actionable_pending = _int(
        _one(
            con,
            f"""
            SELECT COUNT(*) AS total
            FROM segment_state_items s
            WHERE s.run_id = ?
              AND s.state_group = 'pending'
              AND (
                s.needs_output_apply = 1
                OR s.output_state IN ('output_missing', 'confirmation_mismatch')
                OR s.final_state IN (
                  'pending_unknown_open',
                  'pending_output_missing_real',
                  'pending_blocked_structure',
                  'pending_human_review',
                  'reopen_auto_confirmed'
                )
                OR EXISTS (
                  SELECT 1
                  FROM issues i
                  WHERE i.segment_id = s.segment_id
                    AND lower(i.severity) IN ('high', 'error', 'critical')
                )
                OR s.segment_id IN ({bridge_pending_sql})
              )
            """,
            (run_id, run_id),
        ).get("total")
    )
    actionable_without_bridge = max(actionable_pending - governed_bridge_pending, 0)
    closed = _int(summary.get("closed_count"))
    raw_pending = _int(summary.get("pending_count"))
    needs_apply = _int(summary.get("output_apply_pending_count"))
    learning_backlog = max(raw_pending - actionable_pending, 0)
    distribution = [
        {"name": "Consolidado", "value": closed, "group": "closed_consolidated", "color": "#10b981"},
        {"name": "Pendencia acionavel", "value": actionable_without_bridge, "group": "actionable_pending", "color": "#f59e0b"},
        {"name": "Suspeita ML / Watch", "value": model_watch, "group": "model_suspicion_watch", "color": "#8b5cf6"},
        {"name": "Ponte governada pendente", "value": governed_bridge_pending, "group": "governed_bridge_pending", "color": "#38bdf8"},
    ]
    return {
        "run_id": run_id,
        "closed_consolidated": closed,
        "actionable_pending": actionable_pending,
        "actionable_pending_without_bridge": actionable_without_bridge,
        "model_suspicion_watch": model_watch,
        "governed_bridge_pending": governed_bridge_pending,
        "raw_pending": raw_pending,
        "learning_backlog": learning_backlog,
        "needs_apply": needs_apply,
        "select_cstring": {
            "total": _int(select_cstring.get("total")),
            "closed": _int(select_cstring.get("closed")),
            "pending": governed_bridge_pending,
        },
        "select_cstring_segment_bridge": {
            "proposal_run_id": _int(select_cstring_segment_bridge.get("proposal_run_id")),
            "candidates": _int(select_cstring_segment_bridge.get("candidates")),
            "closed": _int(select_cstring_segment_bridge.get("closed")),
            "blocked": _int(select_cstring_segment_bridge.get("blocked")),
            "review_required": _int(select_cstring_segment_bridge.get("review_required")),
            "production_release_allowed": _int(select_cstring_segment_bridge.get("production_release_allowed")),
        },
        "select_cstring_segment_bridge_proposal_run_id": _int(select_cstring_segment_bridge.get("proposal_run_id")),
        "select_cstring_segment_bridge_candidates": _int(select_cstring_segment_bridge.get("candidates")),
        "select_cstring_segment_bridge_closed": _int(select_cstring_segment_bridge.get("closed")),
        "select_cstring_segment_bridge_blocked": _int(select_cstring_segment_bridge.get("blocked")),
        "select_cstring_segment_bridge_review_required": _int(select_cstring_segment_bridge.get("review_required")),
        "select_cstring_segment_bridge_production_release_allowed": _int(select_cstring_segment_bridge.get("production_release_allowed")),
        "short_semantic_tier1_dry_run_id": _int(short_semantic_tier1.get("dry_run_id")),
        "short_semantic_tier1_dry_run_ids": str(short_semantic_tier1.get("dry_run_ids") or short_semantic_tier1.get("dry_run_id") or ""),
        "short_semantic_tier1_candidates": _int(short_semantic_tier1.get("candidates")),
        "short_semantic_tier1_allowed": _int(short_semantic_tier1.get("allowed")),
        "short_semantic_tier1_blocked": _int(short_semantic_tier1.get("blocked")),
        "short_semantic_tier1_closed_by_bridge": _int(short_semantic_tier1.get("closed_by_bridge")),
        "short_semantic_tier1_blocked_by_bridge": _int(short_semantic_tier1.get("blocked_by_bridge")),
        "short_semantic_tier1_allowed_rate": _num(short_semantic_tier1.get("allowed_rate")),
        "short_semantic_tier1_checkpoint_precision_proxy": _num(short_semantic_tier1.get("checkpoint_precision_proxy")),
        "short_semantic_tier1_status": short_semantic_tier1.get("status") or "unavailable",
        "semantic_short_label_pair_bridge_dry_run_id": _int(semantic_short_label_pair_bridge.get("dry_run_id")),
        "semantic_short_label_pair_bridge_checkpoint_id": _int(semantic_short_label_pair_bridge.get("checkpoint_id")),
        "semantic_short_label_pair_bridge_allowed": _int(semantic_short_label_pair_bridge.get("allowed")),
        "semantic_short_label_pair_bridge_closed": _int(semantic_short_label_pair_bridge.get("closed")),
        "semantic_short_label_pair_bridge_blocked": _int(semantic_short_label_pair_bridge.get("blocked")),
        "semantic_short_label_pair_bridge_precision_proxy": _num(semantic_short_label_pair_bridge.get("precision_proxy")),
        "semantic_short_label_pair_bridge_status": semantic_short_label_pair_bridge.get("status") or "unavailable",
        "semantic_short_label_pair_guard_profiles": semantic_short_label_pair_bridge.get("guard_profiles") or "",
        "semantic_short_label_pair_dry_run_ids": semantic_short_label_pair_bridge.get("dry_run_ids")
        or str(semantic_short_label_pair_bridge.get("dry_run_id") or ""),
        "semantic_short_label_pair_checkpoint_ids": semantic_short_label_pair_bridge.get("checkpoint_ids")
        or str(semantic_short_label_pair_bridge.get("checkpoint_id") or ""),
        "semantic_short_label_pair_allowed_total": _int(semantic_short_label_pair_bridge.get("allowed")),
        "semantic_short_label_pair_closed": _int(semantic_short_label_pair_bridge.get("closed")),
        "semantic_short_label_pair_blocked": _int(semantic_short_label_pair_bridge.get("blocked")),
        "semantic_short_label_pair_balanced_audit_safe": 150
        if "balanced_v1" in str(semantic_short_label_pair_bridge.get("guard_profiles") or "")
        else 0,
        "semantic_short_label_pair_balanced_audit_total": 150
        if "balanced_v1" in str(semantic_short_label_pair_bridge.get("guard_profiles") or "")
        else 0,
        "short_label_compact_ui_semantic_companion_bridge_run_ids": (
            short_label_compact_ui_semantic_companion.get("bridge_run_ids") or ""
        ),
        "short_label_compact_ui_semantic_companion_candidates": _int(
            short_label_compact_ui_semantic_companion.get("candidates")
        ),
        "short_label_compact_ui_semantic_companion_closed": _int(
            short_label_compact_ui_semantic_companion.get("closed")
        ),
        "short_label_compact_ui_semantic_companion_blocked": _int(
            short_label_compact_ui_semantic_companion.get("blocked")
        ),
        "short_label_compact_ui_semantic_companion_status": (
            short_label_compact_ui_semantic_companion.get("status") or "unavailable"
        ),
        "short_label_semantic_pair_bridge_run_ids": short_label_semantic_pair.get("bridge_run_ids") or "",
        "short_label_semantic_pair_review_jsonl": short_label_semantic_pair.get("review_jsonl") or "",
        "short_label_semantic_pair_candidates": _int(short_label_semantic_pair.get("candidates")),
        "short_label_semantic_pair_closed": _int(short_label_semantic_pair.get("closed")),
        "short_label_semantic_pair_blocked": _int(short_label_semantic_pair.get("blocked")),
        "short_label_semantic_pair_closed_quote_fragment": _int(
            short_label_semantic_pair.get("closed_quote_fragment")
        ),
        "short_label_semantic_pair_closed_nominal_label": _int(
            short_label_semantic_pair.get("closed_nominal_label")
        ),
        "short_label_semantic_pair_closed_short_phrase": _int(
            short_label_semantic_pair.get("closed_short_phrase")
        ),
        "short_label_semantic_pair_closed_compact_option": _int(
            short_label_semantic_pair.get("closed_compact_option")
        ),
        "short_label_semantic_pair_status": short_label_semantic_pair.get("status") or "unavailable",
        "short_label_semantic_load_tips_bridge_run_ids": (
            short_label_semantic_load_tips.get("bridge_run_ids") or ""
        ),
        "short_label_semantic_load_tips_candidates": _int(
            short_label_semantic_load_tips.get("candidates")
        ),
        "short_label_semantic_load_tips_ready": _int(short_label_semantic_load_tips.get("ready")),
        "short_label_semantic_load_tips_closed": _int(short_label_semantic_load_tips.get("closed")),
        "short_label_semantic_load_tips_blocked": _int(short_label_semantic_load_tips.get("blocked")),
        "short_label_semantic_load_tips_status": (
            short_label_semantic_load_tips.get("status") or "unavailable"
        ),
        "dynamic_ck3_pattern_checkpoint_run_ids": dynamic_ck3_pattern.get("checkpoint_run_ids") or "",
        "dynamic_ck3_pattern_candidates": _int(dynamic_ck3_pattern.get("candidates")),
        "dynamic_ck3_pattern_allowed": _int(dynamic_ck3_pattern.get("allowed")),
        "dynamic_ck3_pattern_closed": _int(dynamic_ck3_pattern.get("closed")),
        "dynamic_ck3_pattern_blocked": _int(dynamic_ck3_pattern.get("blocked")),
        "dynamic_ck3_pattern_status": dynamic_ck3_pattern.get("status") or "unavailable",
        "dynamic_ck3_pattern_subpolicies_closed": dynamic_ck3_pattern_subpolicies_closed,
        "short_label_dynamic_delegate_checkpoint_run_ids": (
            short_label_dynamic_delegate.get("checkpoint_run_ids") or ""
        ),
        "short_label_dynamic_delegate_allowed_rows": _int(short_label_dynamic_delegate.get("allowed_rows")),
        "short_label_dynamic_delegate_distinct_allowed_segments": _int(
            short_label_dynamic_delegate.get("distinct_allowed_segments")
        ),
        "short_label_dynamic_delegate_already_closed_before": _int(
            short_label_dynamic_delegate.get("already_closed_before")
        ),
        "short_label_dynamic_delegate_pending_before": _int(short_label_dynamic_delegate.get("pending_before")),
        "short_label_dynamic_delegate_closed": _int(short_label_dynamic_delegate.get("closed")),
        "short_label_dynamic_delegate_blocked": _int(short_label_dynamic_delegate.get("blocked")),
        "short_label_dynamic_delegate_net_gain": _int(short_label_dynamic_delegate.get("net_gain")),
        "short_label_dynamic_delegate_status": short_label_dynamic_delegate.get("status") or "unavailable",
        "short_label_dynamic_delegate_kind_split": short_label_dynamic_delegate_kind_split,
        "short_label_single_issue_system_tooltip_checkpoint_run_ids": (
            short_label_single_issue_system_tooltip.get("checkpoint_run_ids") or ""
        ),
        "short_label_single_issue_system_tooltip_allowed_rows": _int(
            short_label_single_issue_system_tooltip.get("allowed_rows")
        ),
        "short_label_single_issue_system_tooltip_distinct_allowed_segments": _int(
            short_label_single_issue_system_tooltip.get("distinct_allowed_segments")
        ),
        "short_label_single_issue_system_tooltip_already_closed_before": _int(
            short_label_single_issue_system_tooltip.get("already_closed_before")
        ),
        "short_label_single_issue_system_tooltip_pending_before": _int(
            short_label_single_issue_system_tooltip.get("pending_before")
        ),
        "short_label_single_issue_system_tooltip_closed": _int(
            short_label_single_issue_system_tooltip.get("closed")
        ),
        "short_label_single_issue_system_tooltip_blocked": _int(
            short_label_single_issue_system_tooltip.get("blocked")
        ),
        "short_label_single_issue_system_tooltip_net_gain": _int(
            short_label_single_issue_system_tooltip.get("net_gain")
        ),
        "short_label_single_issue_system_tooltip_status": (
            short_label_single_issue_system_tooltip.get("status") or "unavailable"
        ),
        "short_label_single_issue_system_tooltip_profile_split": (
            short_label_single_issue_system_tooltip_profile_split
        ),
        "short_label_single_issue_system_tooltip_path_split": short_label_single_issue_system_tooltip_path_split,
        "short_label_tokenized_ui_checkpoint_run_ids": short_label_tokenized_ui.get("checkpoint_run_ids") or "",
        "short_label_tokenized_ui_candidates": _int(short_label_tokenized_ui.get("candidates")),
        "short_label_tokenized_ui_allowed": _int(short_label_tokenized_ui.get("allowed")),
        "short_label_tokenized_ui_closed": _int(short_label_tokenized_ui.get("closed")),
        "short_label_tokenized_ui_blocked": _int(short_label_tokenized_ui.get("blocked")),
        "short_label_tokenized_ui_status": short_label_tokenized_ui.get("status") or "unavailable",
        "semantic_short_label_blocked_surface_checkpoint_id": _int(semantic_short_label_blocked_surface.get("checkpoint_id")),
        "semantic_short_label_blocked_surface_dry_run_id": _int(semantic_short_label_blocked_surface.get("dry_run_id")),
        "semantic_short_label_blocked_surface_row_guard_profile": semantic_short_label_blocked_surface.get("row_guard_profile") or "",
        "semantic_short_label_blocked_surface_allowed": _int(semantic_short_label_blocked_surface.get("allowed")),
        "semantic_short_label_blocked_surface_closed": _int(semantic_short_label_blocked_surface.get("closed")),
        "semantic_short_label_blocked_surface_blocked": _int(semantic_short_label_blocked_surface.get("blocked")),
        "semantic_short_label_blocked_surface_precision_proxy": _num(semantic_short_label_blocked_surface.get("precision_proxy")),
        "semantic_short_label_blocked_surface_status": semantic_short_label_blocked_surface.get("status") or "unavailable",
        "event_dialogue_option_checkpoint_id": _int(event_dialogue_option.get("checkpoint_id")),
        "event_dialogue_option_checkpoint_ids": event_dialogue_option.get("checkpoint_ids") or str(event_dialogue_option.get("checkpoint_id") or ""),
        "event_dialogue_option_dry_run_id": _int(event_dialogue_option.get("dry_run_id")),
        "event_dialogue_option_row_guard_profile": event_dialogue_option.get("row_guard_profile") or "",
        "event_dialogue_option_allowed": _int(event_dialogue_option.get("allowed")),
        "event_dialogue_option_closed": _int(event_dialogue_option.get("closed")),
        "event_dialogue_option_blocked": _int(event_dialogue_option.get("blocked")),
        "event_dialogue_option_precision_proxy": _num(event_dialogue_option.get("precision_proxy")),
        "event_dialogue_option_status": event_dialogue_option.get("status") or "unavailable",
        "event_context_composer_checkpoint_id": _int(event_context_composer.get("checkpoint_id")),
        "event_context_composer_dry_run_id": _int(event_context_composer.get("dry_run_id")),
        "event_context_composer_row_guard_profile": event_context_composer.get("row_guard_profile") or "",
        "event_context_composer_allowed": _int(event_context_composer.get("allowed")),
        "event_context_composer_closed": _int(event_context_composer.get("closed")),
        "event_context_composer_blocked": _int(event_context_composer.get("blocked")),
        "event_context_composer_precision_proxy": _num(event_context_composer.get("precision_proxy")),
        "event_context_composer_status": event_context_composer.get("status") or "unavailable",
        "event_dialogue_boundary_checkpoint_run_ids": event_dialogue_boundary.get("checkpoint_ids") or "",
        "event_dialogue_boundary_queue_run_ids": event_dialogue_boundary.get("queue_run_ids") or "",
        "event_dialogue_boundary_candidates": _int(event_dialogue_boundary.get("candidates")),
        "event_dialogue_boundary_allowed": _int(event_dialogue_boundary.get("allowed")),
        "event_dialogue_boundary_closed": _int(event_dialogue_boundary.get("closed")),
        "event_dialogue_boundary_blocked": _int(event_dialogue_boundary.get("blocked")),
        "event_dialogue_boundary_checkpoint_blocked": _int(event_dialogue_boundary.get("checkpoint_blocked")),
        "event_dialogue_boundary_status": event_dialogue_boundary.get("status") or "unavailable",
        "event_outcome_boundary_checkpoint_run_ids": event_outcome_boundary.get("checkpoint_ids") or "",
        "event_outcome_boundary_queue_run_ids": event_outcome_boundary.get("queue_run_ids") or "",
        "event_outcome_boundary_candidates": _int(event_outcome_boundary.get("candidates")),
        "event_outcome_boundary_allowed": _int(event_outcome_boundary.get("allowed")),
        "event_outcome_boundary_closed": _int(event_outcome_boundary.get("closed")),
        "event_outcome_boundary_blocked": _int(event_outcome_boundary.get("blocked")),
        "event_outcome_boundary_status": event_outcome_boundary.get("status") or "unavailable",
        "event_context_sentence_boundary_checkpoint_run_ids": event_context_sentence_boundary.get("checkpoint_ids") or "",
        "event_context_sentence_boundary_queue_run_ids": event_context_sentence_boundary.get("queue_run_ids") or "",
        "event_context_sentence_boundary_candidates": _int(event_context_sentence_boundary.get("candidates")),
        "event_context_sentence_boundary_allowed": _int(event_context_sentence_boundary.get("allowed")),
        "event_context_sentence_boundary_closed": _int(event_context_sentence_boundary.get("closed")),
        "event_context_sentence_boundary_blocked": _int(event_context_sentence_boundary.get("blocked")),
        "event_context_sentence_boundary_checkpoint_blocked": _int(
            event_context_sentence_boundary.get("checkpoint_blocked")
        ),
        "event_context_sentence_boundary_status": event_context_sentence_boundary.get("status") or "unavailable",
        "semantic_short_label_blocked_surface_batch_checkpoint_ids": semantic_short_label_blocked_surface_batch.get("checkpoint_ids") or "",
        "semantic_short_label_blocked_surface_batch_dry_run_id": _int(semantic_short_label_blocked_surface_batch.get("dry_run_id")),
        "semantic_short_label_blocked_surface_batch_allowed": _int(semantic_short_label_blocked_surface_batch.get("allowed")),
        "semantic_short_label_blocked_surface_batch_closed": _int(semantic_short_label_blocked_surface_batch.get("closed")),
        "semantic_short_label_blocked_surface_batch_blocked": _int(semantic_short_label_blocked_surface_batch.get("blocked")),
        "semantic_short_label_blocked_surface_batch_already_closed": _int(semantic_short_label_blocked_surface_batch.get("already_closed")),
        "semantic_short_label_blocked_surface_batch_status": semantic_short_label_blocked_surface_batch.get("status") or "unavailable",
        "semantic_short_label_blocked_surface_batch_by_agent": semantic_short_label_blocked_surface_batch.get("by_agent") or [],
        "semantic_short_label_blocked_surface_run36_checkpoint_ids": semantic_short_label_blocked_surface_run36.get("checkpoint_ids") or "",
        "semantic_short_label_blocked_surface_run36_dry_run_id": _int(semantic_short_label_blocked_surface_run36.get("dry_run_id")),
        "semantic_short_label_blocked_surface_run36_allowed": _int(semantic_short_label_blocked_surface_run36.get("allowed")),
        "semantic_short_label_blocked_surface_run36_closed": _int(semantic_short_label_blocked_surface_run36.get("closed")),
        "semantic_short_label_blocked_surface_run36_blocked": _int(semantic_short_label_blocked_surface_run36.get("blocked")),
        "semantic_short_label_blocked_surface_run36_already_closed": _int(semantic_short_label_blocked_surface_run36.get("already_closed")),
        "semantic_short_label_blocked_surface_run36_status": semantic_short_label_blocked_surface_run36.get("status") or "unavailable",
        "semantic_short_label_blocked_surface_run36_by_agent": semantic_short_label_blocked_surface_run36.get("by_agent") or [],
        "autofix_unknown_event_composition_checkpoint_id": _int(autofix_unknown_event_composition.get("checkpoint_id")),
        "autofix_unknown_event_composition_bridge_run_id": _int(autofix_unknown_event_composition.get("bridge_run_id")),
        "autofix_unknown_event_composition_allowed": _int(autofix_unknown_event_composition.get("allowed")),
        "autofix_unknown_event_composition_ready": _int(autofix_unknown_event_composition.get("ready")),
        "autofix_unknown_event_composition_partial": _int(autofix_unknown_event_composition.get("partial")),
        "autofix_unknown_event_composition_blocked": _int(autofix_unknown_event_composition.get("blocked")),
        "autofix_unknown_event_composition_closed": _int(autofix_unknown_event_composition.get("closed")),
        "autofix_unknown_event_composition_status": autofix_unknown_event_composition.get("status") or "unavailable",
        "autofix_unknown_event_semantic_companion_bridge_run_id": _int(
            autofix_unknown_event_semantic_companion.get("bridge_run_id")
        ),
        "autofix_unknown_event_semantic_companion_event_bridge_run_id": _int(
            autofix_unknown_event_semantic_companion.get("event_bridge_run_id")
        ),
        "autofix_unknown_event_semantic_companion_candidates": _int(
            autofix_unknown_event_semantic_companion.get("candidates")
        ),
        "autofix_unknown_event_semantic_companion_ready": _int(
            autofix_unknown_event_semantic_companion.get("ready")
        ),
        "autofix_unknown_event_semantic_companion_closed": _int(
            autofix_unknown_event_semantic_companion.get("closed")
        ),
        "autofix_unknown_event_semantic_companion_blocked": _int(
            autofix_unknown_event_semantic_companion.get("blocked")
        ),
        "autofix_unknown_event_semantic_companion_status": autofix_unknown_event_semantic_companion.get("status")
        or "unavailable",
        "event_surface_short_label_companion_bridge_run_id": _int(
            event_surface_short_label_companion.get("bridge_run_id")
        ),
        "event_surface_short_label_companion_candidates": _int(
            event_surface_short_label_companion.get("candidates")
        ),
        "event_surface_short_label_companion_ready": _int(event_surface_short_label_companion.get("ready")),
        "event_surface_short_label_companion_closed": _int(event_surface_short_label_companion.get("closed")),
        "event_surface_short_label_companion_blocked": _int(event_surface_short_label_companion.get("blocked")),
        "event_surface_short_label_companion_estimated_closed_gain": _int(
            event_surface_short_label_companion.get("estimated_closed_gain")
        ),
        "event_surface_short_label_companion_status": event_surface_short_label_companion.get("status")
        or "unavailable",
        "autofix_unknown_surface_semantic_coverage_run_ids": autofix_unknown_surface_semantic.get("coverage_run_ids") or "",
        "autofix_unknown_surface_semantic_candidates": _int(autofix_unknown_surface_semantic.get("candidates")),
        "autofix_unknown_surface_semantic_closed": _int(autofix_unknown_surface_semantic.get("closed")),
        "autofix_unknown_surface_semantic_blocked": _int(autofix_unknown_surface_semantic.get("blocked")),
        "autofix_unknown_surface_semantic_status": autofix_unknown_surface_semantic.get("status") or "unavailable",
        "autofix_unknown_semantic_companion_building_checkpoint_run_ids": (
            autofix_unknown_semantic_companion_building.get("checkpoint_run_ids") or ""
        ),
        "autofix_unknown_semantic_companion_building_candidates": _int(
            autofix_unknown_semantic_companion_building.get("candidates")
        ),
        "autofix_unknown_semantic_companion_building_allowed": _int(
            autofix_unknown_semantic_companion_building.get("allowed")
        ),
        "autofix_unknown_semantic_companion_building_closed": _int(
            autofix_unknown_semantic_companion_building.get("closed")
        ),
        "autofix_unknown_semantic_companion_building_blocked": _int(
            autofix_unknown_semantic_companion_building.get("blocked")
        ),
        "autofix_unknown_semantic_companion_building_status": (
            autofix_unknown_semantic_companion_building.get("status") or "unavailable"
        ),
        "autofix_unknown_semantic_companion_rule_effect_checkpoint_run_ids": (
            autofix_unknown_semantic_companion_rule_effect.get("checkpoint_run_ids") or ""
        ),
        "autofix_unknown_semantic_companion_rule_effect_candidates": _int(
            autofix_unknown_semantic_companion_rule_effect.get("candidates")
        ),
        "autofix_unknown_semantic_companion_rule_effect_allowed": _int(
            autofix_unknown_semantic_companion_rule_effect.get("allowed")
        ),
        "autofix_unknown_semantic_companion_rule_effect_closed": _int(
            autofix_unknown_semantic_companion_rule_effect.get("closed")
        ),
        "autofix_unknown_semantic_companion_rule_effect_blocked": _int(
            autofix_unknown_semantic_companion_rule_effect.get("blocked")
        ),
        "autofix_unknown_semantic_companion_rule_effect_status": (
            autofix_unknown_semantic_companion_rule_effect.get("status") or "unavailable"
        ),
        "autofix_unknown_surface_proposal_run_ids": autofix_unknown_surface.get("proposal_run_ids") or "",
        "autofix_unknown_surface_checkpoint_run_ids": autofix_unknown_surface.get("checkpoint_run_ids") or "",
        "autofix_unknown_surface_ready_items_total": _int(autofix_unknown_surface.get("ready_items_total")),
        "autofix_unknown_surface_ready_segments_distinct": _int(autofix_unknown_surface.get("ready_segments_distinct")),
        "autofix_unknown_surface_closed": _int(autofix_unknown_surface.get("closed")),
        "autofix_unknown_surface_blocked": _int(autofix_unknown_surface.get("blocked")),
        "autofix_unknown_surface_status": autofix_unknown_surface.get("status") or "unavailable",
        "autofix_unknown_domain_guarded_checkpoint_run_ids": (
            autofix_unknown_domain_guarded.get("checkpoint_run_ids") or ""
        ),
        "autofix_unknown_domain_guarded_allowed_rows": _int(
            autofix_unknown_domain_guarded.get("allowed_rows")
        ),
        "autofix_unknown_domain_guarded_distinct_allowed_segments": _int(
            autofix_unknown_domain_guarded.get("distinct_allowed_segments")
        ),
        "autofix_unknown_domain_guarded_already_closed_before": _int(
            autofix_unknown_domain_guarded.get("already_closed_before")
        ),
        "autofix_unknown_domain_guarded_pending_before": _int(
            autofix_unknown_domain_guarded.get("pending_before")
        ),
        "autofix_unknown_domain_guarded_closed": _int(autofix_unknown_domain_guarded.get("closed")),
        "autofix_unknown_domain_guarded_blocked": _int(autofix_unknown_domain_guarded.get("blocked")),
        "autofix_unknown_domain_guarded_net_gain": _int(autofix_unknown_domain_guarded.get("net_gain")),
        "autofix_unknown_domain_guarded_status": autofix_unknown_domain_guarded.get("status") or "unavailable",
        "autofix_unknown_domain_guarded_subpolicy_split": autofix_unknown_domain_guarded_subpolicy_split,
        "autofix_unknown_domain_guarded_cluster_split": autofix_unknown_domain_guarded_cluster_split,
        "autofix_unknown_domain_guarded_reviewed_checkpoint_run_ids": (
            autofix_unknown_domain_guarded_reviewed.get("checkpoint_run_ids") or ""
        ),
        "autofix_unknown_domain_guarded_reviewed_candidates": _int(
            autofix_unknown_domain_guarded_reviewed.get("candidates")
        ),
        "autofix_unknown_domain_guarded_reviewed_closed": _int(
            autofix_unknown_domain_guarded_reviewed.get("closed")
        ),
        "autofix_unknown_domain_guarded_reviewed_blocked": _int(
            autofix_unknown_domain_guarded_reviewed.get("blocked")
        ),
        "autofix_unknown_domain_guarded_reviewed_status": (
            autofix_unknown_domain_guarded_reviewed.get("status") or "unavailable"
        ),
        "short_label_pure_no_token_domain_checkpoint_id": short_label_pure_no_token_domain.get("checkpoint_id") or "",
        "short_label_pure_no_token_domain_shadow_run_id": short_label_pure_no_token_domain.get("shadow_run_id") or "",
        "short_label_pure_no_token_domain_candidates": _int(short_label_pure_no_token_domain.get("candidates")),
        "short_label_pure_no_token_domain_allowed": _int(short_label_pure_no_token_domain.get("allowed")),
        "short_label_pure_no_token_domain_closed": _int(short_label_pure_no_token_domain.get("closed")),
        "short_label_pure_no_token_domain_blocked": _int(short_label_pure_no_token_domain.get("blocked")),
        "short_label_pure_no_token_domain_status": short_label_pure_no_token_domain.get("status") or "unavailable",
        "gender_longform_false_reopen_checkpoint_id": gender_longform_false_reopen.get("checkpoint_id") or "",
        "gender_longform_false_reopen_candidates": _int(gender_longform_false_reopen.get("candidates")),
        "gender_longform_false_reopen_closed": _int(gender_longform_false_reopen.get("closed")),
        "gender_longform_false_reopen_blocked": _int(gender_longform_false_reopen.get("blocked")),
        "gender_longform_false_reopen_routes_closed": {
            str(row["route_key"]): _int(row["count"])
            for row in gender_longform_false_reopen_routes
        },
        "gender_longform_false_reopen_status": gender_longform_false_reopen.get("status") or "unavailable",
        "short_label_pure_no_token_semantic_bridge_run_id": _int(short_label_pure_no_token_semantic.get("bridge_run_id")),
        "short_label_pure_no_token_semantic_coverage_run_id": _int(short_label_pure_no_token_semantic.get("coverage_run_id")),
        "short_label_pure_no_token_semantic_checkpoint_id": _int(short_label_pure_no_token_semantic.get("checkpoint_id")),
        "short_label_pure_no_token_semantic_ready": _int(short_label_pure_no_token_semantic.get("ready")),
        "short_label_pure_no_token_semantic_blocked": _int(short_label_pure_no_token_semantic.get("blocked")),
        "short_label_pure_no_token_semantic_closed": _int(short_label_pure_no_token_semantic.get("closed")),
        "short_label_pure_no_token_semantic_estimated_closed_gain": _int(short_label_pure_no_token_semantic.get("estimated_closed_gain")),
        "short_label_pure_no_token_semantic_status": short_label_pure_no_token_semantic.get("status") or "unavailable",
        "distribution": [row for row in distribution if row["value"] > 0],
    }


def _short_model_name(version: str | None) -> str:
    if not version:
        return "sem modelo"
    if "_v" in version:
        return "v" + version.split("_v", 1)[1].split("_", 1)[0]
    return version


def _model_axis_label(row: dict[str, Any]) -> str:
    return f"#{row['id']} {_short_model_name(row.get('model_version'))}"


def _run_axis_label(run_id: Any) -> str:
    return str(_int(run_id))


def _latest_score(con: sqlite3.Connection, offset: int = 0, operational: bool = False) -> dict[str, Any]:
    where = "WHERE scored_count >= 10000 AND finished_at IS NOT NULL" if operational else ""
    return _one(
        con,
        f"""
        SELECT *
        FROM ml_score_runs
        {where}
        ORDER BY id DESC
        LIMIT 1 OFFSET ?
        """,
        (offset,),
    )


def _active_model(con: sqlite3.Connection) -> dict[str, Any]:
    return _one(
        con,
        """
        SELECT
          r.active_model_run_id,
          r.active_model_version,
          r.policy_version,
          r.promoted_at,
          r.reason,
          m.*
        FROM ml_model_registry r
        LEFT JOIN ml_model_runs m ON m.id = r.active_model_run_id
        WHERE r.model_kind = 'risk_action_classifier'
        LIMIT 1
        """,
    )


def _latest_model(con: sqlite3.Connection) -> dict[str, Any]:
    return _one(
        con,
        """
        SELECT *
        FROM ml_model_runs
        WHERE model_kind = 'risk_action_classifier'
        ORDER BY id DESC
        LIMIT 1
        """,
    )


def _latest_dataset(con: sqlite3.Connection) -> dict[str, Any]:
    return _one(con, "SELECT * FROM ml_dataset_runs ORDER BY id DESC LIMIT 1")


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"read_error": str(exc), "path": str(path)}
    return parsed if isinstance(parsed, dict) else {"path": str(path), "read_error": "json_root_is_not_object"}


def _nested_value(payload: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        current: Any = payload
        for key in path:
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current[key]
        if current is not None:
            return current
    return None


def _feedback_status_tokens(item: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for key in ("status", "current_candidate_status", "validation_status", "next_action", "decision"):
        value = item.get(key)
        if value is None:
            continue
        raw = str(value).strip().lower()
        if not raw:
            continue
        tokens.add(raw)
        tokens.update(part for part in raw.replace("-", "_").split("_") if part)
    return tokens


def _is_active_post_release_feedback_item(item: dict[str, Any]) -> bool:
    tokens = _feedback_status_tokens(item)
    if not tokens:
        return True
    if any(token.startswith("hold") for token in tokens):
        return False
    if tokens & FEEDBACK_ACTIVE_STATUSES:
        return True
    if tokens & FEEDBACK_ARCHIVED_STATUSES:
        return False
    if any(token.startswith(("closed", "resolved", "validated", "learned")) for token in tokens):
        return False
    if "applied" in tokens and "pending" not in tokens:
        return False
    return True


def _post_release_feedback_payload(segment_state: dict[str, Any]) -> dict[str, Any]:
    raw = _read_json_file(POST_RELEASE_FEEDBACK_STATUS_FILE)
    stable_baseline_path = str(ROOT / "source" / "spanish_old")
    broken_release_path = str(ROOT / "output" / "spanish")
    candidate_root = ROOT / "release_candidates"
    current_candidate_path = None
    if candidate_root.exists():
        candidates = sorted((p for p in candidate_root.iterdir() if p.is_dir() or p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
        current_candidate_path = str(candidates[0]) if candidates else str(candidate_root)

    base_summary = {
        "known_open_findings": None,
        "approved_pending_apply": None,
        "applied_pending_validation": None,
        "closed_findings": None,
        "accepted_holds": None,
        "parser_dynamic_backlog": None,
        "visual_locks_count": None,
        "regression_watch_count": None,
        "can_publish_hotfix": None,
    }
    if not raw:
        return {
            "instrumented": False,
            "status": "not_measured",
            "path": str(POST_RELEASE_FEEDBACK_STATUS_FILE),
            "summary": base_summary,
            "items": [],
            "active_items": [],
            "archived_items": [],
            "baseline_control": {
                "instrumented": False,
                "status": "not_measured",
                "stable_baseline_path": stable_baseline_path,
                "broken_release_path": broken_release_path,
            },
            "visual_locks": {
                "instrumented": False,
                "status": "not_measured",
                "count": None,
                "items": [],
            },
            "release_candidate": {
                "instrumented": False,
                "status": "not_measured",
                "current_candidate_path": current_candidate_path,
            },
            "source_output_update": {
                "instrumented": False,
                "status": "not_measured",
                "game_version": None,
                "source_updated": None,
                "output_synced": None,
                "latest_diff": None,
                "full_production_recommended": None,
            },
        }

    if raw.get("read_error"):
        status = "read_error"
        instrumented = False
    else:
        status = raw.get("status") or "instrumented"
        instrumented = True

    summary = {
        "known_open_findings": _nested_value(raw, ("summary", "known_open_findings"), ("summary", "known_open"), ("known_open_findings",), ("known_open",)),
        "approved_pending_apply": _nested_value(raw, ("summary", "approved_pending_apply"), ("approved_pending_apply",)),
        "applied_pending_validation": _nested_value(raw, ("summary", "applied_pending_validation"), ("applied_pending_validation",)),
        "closed_findings": _nested_value(raw, ("summary", "closed_findings"), ("summary", "closed"), ("closed_findings",), ("closed",)),
        "accepted_holds": _nested_value(raw, ("summary", "accepted_holds"), ("accepted_holds",)),
        "parser_dynamic_backlog": _nested_value(raw, ("summary", "parser_dynamic_backlog"), ("parser_dynamic_backlog",)),
        "visual_locks_count": _nested_value(raw, ("summary", "visual_locks_count"), ("visual_locks", "count"), ("visual_locks_count",)),
        "regression_watch_count": _nested_value(raw, ("summary", "regression_watch_count"), ("regression_watch_count",)),
        "can_publish_hotfix": _nested_value(raw, ("summary", "can_publish_hotfix"), ("can_publish_hotfix",)),
    }
    if summary["can_publish_hotfix"] is None:
        required = [
            summary["known_open_findings"],
            summary["approved_pending_apply"],
            summary["applied_pending_validation"],
        ]
        try:
            known_open = _int(summary["known_open_findings"])
            pending_apply = _int(summary["approved_pending_apply"])
            pending_validation = _int(summary["applied_pending_validation"])
        except (TypeError, ValueError):
            known_open = pending_apply = pending_validation = None
        if all(value is not None for value in required) and known_open is not None:
            summary["can_publish_hotfix"] = (
                known_open == 0
                and pending_apply == 0
                and pending_validation == 0
                and _int(segment_state.get("needs_apply")) == 0
            )

    items = _nested_value(raw, ("items",), ("feedback_items",), ("findings",), ("hotfix_queue", "items")) or []
    if not isinstance(items, list):
        items = []
    items = [_normalize_post_release_feedback_item(item) for item in items if isinstance(item, dict)]
    active_items = [item for item in items if _is_active_post_release_feedback_item(item)]
    archived_items = [item for item in items if not _is_active_post_release_feedback_item(item)]
    summary["active_findings"] = len(active_items)
    summary["archived_findings"] = len(archived_items)
    if summary["known_open_findings"] is None:
        summary["known_open_findings"] = len(active_items)
    if summary["closed_findings"] is None and archived_items:
        summary["closed_findings"] = len(archived_items)
    visual_locks_items = _nested_value(raw, ("visual_locks", "items"), ("visual_locks_items",)) or []
    if not isinstance(visual_locks_items, list):
        visual_locks_items = []
    baseline_control = raw.get("baseline_control") if isinstance(raw.get("baseline_control"), dict) else {}
    release_candidate = raw.get("release_candidate") if isinstance(raw.get("release_candidate"), dict) else {}
    source_output_update = raw.get("source_output_update") if isinstance(raw.get("source_output_update"), dict) else {}

    return {
        "instrumented": instrumented,
        "status": status,
        "path": str(POST_RELEASE_FEEDBACK_STATUS_FILE),
        "summary": summary,
        "items": items,
        "active_items": active_items,
        "archived_items": archived_items,
        "baseline_control": {
            "instrumented": bool(baseline_control) or instrumented,
            "status": baseline_control.get("status") or ("instrumented" if baseline_control else "not_measured"),
            "stable_baseline_path": baseline_control.get("stable_baseline_path") or stable_baseline_path,
            "broken_release_path": (
                baseline_control.get("broken_release_path")
                or baseline_control.get("broken_release_reference_path")
                or broken_release_path
            ),
        },
        "visual_locks": {
            "instrumented": bool(raw.get("visual_locks")) or bool(visual_locks_items),
            "status": _nested_value(raw, ("visual_locks", "status")) or ("instrumented" if raw.get("visual_locks") else "not_measured"),
            "count": summary["visual_locks_count"],
            "items": visual_locks_items,
        },
        "release_candidate": {
            "instrumented": bool(release_candidate) or current_candidate_path is not None,
            "status": release_candidate.get("status") or ("instrumented" if release_candidate else "not_measured"),
            "current_candidate_path": (
                release_candidate.get("current_candidate_path")
                or baseline_control.get("current_candidate_path")
                or current_candidate_path
            ),
        },
        "source_output_update": {
            "instrumented": bool(source_output_update),
            "status": source_output_update.get("status") or ("instrumented" if source_output_update else "not_measured"),
            "game_version": source_output_update.get("game_version") or source_output_update.get("detected_game_version"),
            "source_updated": source_output_update.get("source_updated"),
            "output_synced": source_output_update.get("output_synced"),
            "latest_diff": source_output_update.get("latest_diff") or source_output_update.get("latest_source_output_diff"),
            "full_production_recommended": source_output_update.get("full_production_recommended"),
            "blockers_before_full_production": source_output_update.get("blockers_before_full_production"),
        },
    }


def _normalize_post_release_feedback_item(item: dict[str, Any]) -> dict[str, Any]:
    segment_ids = item.get("segment_ids")
    if segment_ids is None and item.get("segment_id") is not None:
        segment_ids = [item.get("segment_id")]
    if not isinstance(segment_ids, list):
        segment_ids = []
    source_keys = item.get("source_keys")
    if source_keys is None and item.get("source_key") is not None:
        source_keys = [item.get("source_key")]
    if not isinstance(source_keys, list):
        source_keys = []
    status = item.get("status") or item.get("current_candidate_status") or "not_measured"
    next_action = (
        item.get("next_action")
        or item.get("current_candidate_status")
        or item.get("decision")
        or item.get("risk")
        or "not_measured"
    )
    observed = (
        item.get("observed_text")
        or item.get("text_observed")
        or item.get("evidence")
        or item.get("decision")
        or "not_measured"
    )
    return {
        **item,
        "observed_text": observed,
        "category": item.get("category") or item.get("area") or item.get("risk") or "not_measured",
        "affected_segments": item.get("affected_segments") if item.get("affected_segments") is not None else len(segment_ids),
        "segment_ids": segment_ids,
        "source_keys": source_keys,
        "visual_severity": item.get("visual_severity") or item.get("severity") or "not_measured",
        "next_action": next_action,
        "status": status,
    }


def _score_tier(value: Any) -> str:
    score = _num(value, -1)
    if score >= 0.9:
        return "high"
    if score >= 0.75:
        return "medium_high"
    if score >= 0.5:
        return "medium"
    if score >= 0:
        return "low"
    return "not_scored"


BOLD_NO_TAG_PATTERN = re.compile(r"#bol(?:d)?\s+no#!", re.IGNORECASE)
BOLD_NAO_TAG_PATTERN = re.compile(r"#bold\s+n(?:\u00e3|a)o#!", re.IGNORECASE)
BOLD_TAG_MALFORMED_PATTERN = re.compile(r"#bol\b", re.IGNORECASE)
BOLD_SPAN_PATTERN = re.compile(r"#bol(?:d)?\s+(.+?)#!", re.IGNORECASE)
STYLE_SPAN_PATTERN = re.compile(r"#(?:P|N|V|bold)\s+(.+?)#!", re.IGNORECASE)
ES_OA_FINAL_VOWEL_BEFORE_HELPER_PATTERN = re.compile(
    r"([A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u00ff]+)([oa])(\[[^\]]*Custom\('ES_OA'\)[^\]]*\])"
)
OCCIDENTAL_WORD_PATTERN = re.compile(r"\boccidental\b", re.IGNORECASE)
OCIDENTAL_WORD_PATTERN = re.compile(r"\bocidental\b", re.IGNORECASE)
VISIBLE_TOKEN_RE = re.compile(r"\$[^$\s]+\$|\[[^\]]+\]|@[A-Za-z0-9_]+!|#[A-Za-z0-9_]+|#!")
SPANISH_BOLD_RESIDUAL_HINTS = (
    "recuperacion",
    "reduccion",
    "probabilidad",
    "exito",
)
PTBR_BOLD_REPAIR_HINTS = (
    "recuperacao",
    "reducao",
    "probabilidade",
    "sucesso",
)


def _token_signature(value: Any) -> set[str]:
    return set(VISIBLE_TOKEN_RE.findall(str(value or "")))


def _score_token_signature(value: Any) -> set[str]:
    text = re.sub(BOLD_TAG_MALFORMED_PATTERN, "#bold", str(value or ""))
    return _token_signature(text)


def _fold_score_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in normalized if not unicodedata.combining(char)).lower()


def _bold_visible_spans(value: Any) -> list[str]:
    return [_fold_score_text(match.group(1)) for match in BOLD_SPAN_PATTERN.finditer(str(value or ""))]


def _style_visible_spans(value: Any) -> list[str]:
    return [_fold_score_text(match.group(1)) for match in STYLE_SPAN_PATTERN.finditer(str(value or ""))]


def _is_closed_confirmed_output_change(record: dict[str, Any]) -> bool:
    if str(record.get("state_group") or "").lower() != "closed":
        return False
    if _int(record.get("needs_output_apply")) != 0:
        return False
    output_text = record.get("output_text")
    old_text = record.get("old_text")
    if _package_compare_text(output_text) == _package_compare_text(old_text):
        return False
    if record.get("confirmed_text") is not None:
        return _package_compare_text(output_text) == _package_compare_text(record.get("confirmed_text"))
    return _int(record.get("confirmed_matches_output")) == 1


def _is_bold_no_to_nao_microrepair(record: dict[str, Any]) -> bool:
    output_text = record.get("output_text")
    if not BOLD_NAO_TAG_PATTERN.search(str(output_text or "")):
        return False
    references = (
        record.get("old_text"),
        record.get("spanish_text"),
        record.get("english_text"),
    )
    bold_no_reference = next((value for value in references if BOLD_NO_TAG_PATTERN.search(str(value or ""))), None)
    if not bold_no_reference:
        return False
    return _score_token_signature(output_text) == _score_token_signature(bold_no_reference)


def _is_bold_tag_markup_normalization_repair(record: dict[str, Any]) -> bool:
    old_text = str(record.get("old_text") or "")
    output_text = str(record.get("output_text") or "")
    if not _is_closed_confirmed_output_change(record):
        return False
    if not BOLD_TAG_MALFORMED_PATTERN.search(old_text):
        return False
    if "#bold" not in output_text.lower():
        return False
    return _score_token_signature(output_text) == _score_token_signature(old_text)


def _is_bold_visible_spanish_residual_repair(record: dict[str, Any]) -> bool:
    if not _is_closed_confirmed_output_change(record):
        return False
    old_text = record.get("old_text")
    output_text = record.get("output_text")
    if _score_token_signature(output_text) != _score_token_signature(old_text):
        return False
    old_spans = " ".join(_bold_visible_spans(old_text))
    output_spans = " ".join(_bold_visible_spans(output_text))
    if not old_spans or not output_spans:
        return False
    old_has_spanish_residue = any(hint in old_spans for hint in SPANISH_BOLD_RESIDUAL_HINTS)
    output_has_ptbr_repair = any(hint in output_spans for hint in PTBR_BOLD_REPAIR_HINTS)
    return old_has_spanish_residue and output_has_ptbr_repair


def _is_style_visible_spanish_residual_repair(record: dict[str, Any]) -> bool:
    if not _is_closed_confirmed_output_change(record):
        return False
    old_text = record.get("old_text")
    output_text = record.get("output_text")
    if _score_token_signature(output_text) != _score_token_signature(old_text):
        return False
    old_spans = " ".join(_style_visible_spans(old_text))
    output_spans = " ".join(_style_visible_spans(output_text))
    if not old_spans or not output_spans:
        return False
    old_has_spanish_residue = any(hint in old_spans for hint in SPANISH_BOLD_RESIDUAL_HINTS)
    output_has_ptbr_repair = any(hint in output_spans for hint in PTBR_BOLD_REPAIR_HINTS)
    return old_has_spanish_residue and output_has_ptbr_repair


def _trim_es_oa_final_vowel(value: Any) -> str:
    return ES_OA_FINAL_VOWEL_BEFORE_HELPER_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(3)}",
        str(value or ""),
    )


def _is_es_oa_final_vowel_trim_repair(record: dict[str, Any]) -> bool:
    if "gender_es_oa_final_vowel_trim" not in str(record.get("final_state") or ""):
        return False
    if not _is_closed_confirmed_output_change(record):
        return False
    old_text = record.get("old_text")
    output_text = record.get("output_text")
    return _package_compare_text(_trim_es_oa_final_vowel(old_text)) == _package_compare_text(output_text)


def _is_feedback_microfix_equal_output_repair(record: dict[str, Any]) -> bool:
    feedback_marker = "in_game_feedback_microfix"
    if feedback_marker not in str(record.get("final_state") or "") and feedback_marker not in str(record.get("confirmation_source") or ""):
        return False
    return _is_closed_confirmed_output_change(record)


def _is_closed_confirmed_production_microrepair(record: dict[str, Any]) -> bool:
    if not _is_closed_confirmed_output_change(record):
        return False
    source = str(record.get("confirmation_source") or "")
    label = str(record.get("confirmation_label") or "")
    final_state = str(record.get("final_state") or "")
    safe_sources = {
        "ptbr_orthographic_microfix_production",
        "short_label_compact_ui_semantic_queue222_microrepair_production",
        "short_label_compact_ui_obvious_repair_production",
        "semantic_short_label_requirement_tooltip_microrepair_production",
        "autofix_unknown_ui_surface_queue221_microrepair_production",
        "in_game_feedback_vassal_stances_claim_cb_microfix_production",
        "dynamic_acclaimed_knight_requirement_repair_production",
        "single_combat_signature_weapon_composer_batch2_0041_production",
    }
    if source not in safe_sources:
        return False
    token_signature_preserved = _score_token_signature(record.get("output_text")) == _score_token_signature(record.get("old_text"))
    token_exception_allowed = (
        source in {
            "in_game_feedback_vassal_stances_claim_cb_microfix_production",
            "dynamic_acclaimed_knight_requirement_repair_production",
        }
        or "token_policy_manual_exception_equal_output_lifecycle" in final_state
        or "dynamic_acclaimed_knight_requirement_repair" in final_state
    )
    if not token_signature_preserved and not token_exception_allowed:
        return False
    return bool(label)


def _is_closed_confirmed_short_label_semantic_repair(record: dict[str, Any]) -> bool:
    if not _is_closed_confirmed_output_change(record):
        return False
    final_state = str(record.get("final_state") or "")
    if not any(
        marker in final_state
        for marker in (
            "short_label_compact_ui_semantic_queue222_microrepair",
            "short_label_compact_ui_obvious_repair",
            "semantic_short_label_requirement_tooltip_microrepair",
        )
    ):
        return False
    return bool(str(record.get("confirmation_source") or "").endswith("_production"))


def _is_spanish_residual_safe_repair(record: dict[str, Any]) -> bool:
    if "spanish_residual" not in str(record.get("final_state") or ""):
        return False
    if not _is_closed_confirmed_output_change(record):
        return False
    return _score_token_signature(record.get("output_text")) == _score_token_signature(record.get("old_text"))


def _is_short_label_pure_no_token_microrepair(record: dict[str, Any]) -> bool:
    if "short_label_pure_no_token" not in str(record.get("final_state") or ""):
        return False
    if not _is_closed_confirmed_output_change(record):
        return False
    return _score_token_signature(record.get("output_text")) == _score_token_signature(record.get("old_text"))


def _is_occidental_to_ocidental_title_repair(record: dict[str, Any]) -> bool:
    if str(record.get("relative_path") or "") not in {"titles_l_spanish.yml", "titles_cultural_names_l_spanish.yml"}:
        return False
    if not str(record.get("source_key") or "").endswith("_adj"):
        return False
    output_text = record.get("output_text")
    if not OCIDENTAL_WORD_PATTERN.search(str(output_text or "")):
        return False
    references = (
        record.get("old_text"),
        record.get("spanish_text"),
        record.get("english_text"),
    )
    occidental_reference = next((value for value in references if OCCIDENTAL_WORD_PATTERN.search(str(value or ""))), None)
    if not occidental_reference:
        return False
    return _token_signature(output_text) == _token_signature(occidental_reference)


def _is_confirmed_title_adjective_lexical_repair(record: dict[str, Any]) -> bool:
    if str(record.get("relative_path") or "") not in {"titles_l_spanish.yml", "titles_cultural_names_l_spanish.yml"}:
        return False
    if not str(record.get("source_key") or "").endswith("_adj"):
        return False
    if str(record.get("confirmation_source") or "") not in {
        "title_landed_adjective_lexical_residue_repair_production",
        "title_landed_adjective_lexical_residue_medium_repair_production",
    }:
        return False
    if str(record.get("confirmation_label") or "") not in {
        "title_landed_adjective_lexical_residue_exact_v1",
        "title_landed_adjective_lexical_residue_direction_suffix_v1",
    }:
        return False
    if _int(record.get("confirmed_matches_output")) != 1:
        return False
    if _int(record.get("needs_output_apply")) != 0:
        return False
    output_text = str(record.get("output_text") or "").strip()
    old_text = str(record.get("old_text") or "").strip()
    return bool(output_text and old_text and output_text != old_text)


def _is_locked_human_equal_output_repair(record: dict[str, Any]) -> bool:
    if str(record.get("confirmation_level") or "") != "human_confirmed":
        return False
    if _int(record.get("locked")) != 1:
        return False
    if record.get("confirmed_text") is not None and _package_compare_text(record.get("output_text")) != _package_compare_text(record.get("confirmed_text")):
        return False
    if _int(record.get("confirmed_matches_output")) != 1:
        return False
    if str(record.get("state_group") or "") != "closed":
        return False
    return str(record.get("output_text") or "").strip() != str(record.get("old_text") or "").strip()


def _package_compare_text(value: Any) -> str:
    text = str(value if value is not None else "").replace("\r\n", "\n")
    text = text.replace('\\"', '"').replace("\\n", "\n")
    return text.strip()


def _annotate_package_integrity(record: dict[str, Any], *, has_state: bool) -> None:
    confirmed_text = record.get("confirmed_text")
    output_text = record.get("output_text")
    locked_confirmation = _int(record.get("locked")) == 1 and confirmed_text is not None
    output_matches_confirmed: bool | None = None
    if locked_confirmation:
        output_matches_confirmed = _package_compare_text(output_text) == _package_compare_text(confirmed_text)
    record["output_matches_confirmed_text"] = output_matches_confirmed
    record["package_integrity_status"] = "ok"
    record["package_integrity_reason"] = "output_diff_measured"
    if locked_confirmation and output_matches_confirmed is False:
        record["package_integrity_status"] = "locked_confirmation_output_mismatch"
        record["package_integrity_reason"] = "locked confirmed_text differs from output"
    elif _int(record.get("needs_output_apply")):
        record["package_integrity_status"] = "needs_output_apply"
        record["package_integrity_reason"] = "confirmed correction is not applied to output"
    elif has_state and str(record.get("state_group") or "").lower() != "closed":
        record["package_integrity_status"] = "not_closed"
        record["package_integrity_reason"] = "segment_state is not closed"


def _is_package_ready_output_change(record: dict[str, Any], *, has_state: bool) -> bool:
    if not has_state:
        return False
    if str(record.get("state_group") or "").lower() != "closed":
        return False
    if _int(record.get("needs_output_apply")):
        return False
    if str(record.get("package_integrity_status") or "ok") != "ok":
        return False
    return _package_compare_text(record.get("output_text")) != _package_compare_text(record.get("old_text"))


def _publication_lifecycle_label_allowed(value: Any) -> bool:
    label = str(value or "")
    if label in PUBLICATION_LIFECYCLE_CONFIRMATION_LABELS:
        return True
    return any(
        label.startswith(prefix) and label.endswith(suffix)
        for prefix in PUBLICATION_LIFECYCLE_CONFIRMATION_LABEL_PREFIXES
        for suffix in PUBLICATION_LIFECYCLE_CONFIRMATION_LABEL_SUFFIXES
    )


def _is_publication_lifecycle_apply_candidate(record: dict[str, Any], *, has_state: bool) -> bool:
    if not has_state:
        return False
    if record.get("recommended_resolution") != "use_candidate":
        return False
    if str(record.get("package_integrity_status") or "") != "not_closed":
        return False
    if str(record.get("state_group") or "").lower() == "closed":
        return False
    if str(record.get("final_state") or "") != "reopen_auto_confirmed_autofix":
        return False
    if _int(record.get("needs_output_apply")) != 0:
        return False
    if _int(record.get("confirmed_matches_output")) != 1:
        return False
    if str(record.get("confirmation_level") or "") != "human_confirmed":
        return False
    if _int(record.get("locked")) != 1:
        return False
    if str(record.get("confirmation_source") or "") not in PUBLICATION_LIFECYCLE_CONFIRMATION_SOURCES:
        return False
    if not _publication_lifecycle_label_allowed(record.get("confirmation_label")):
        return False
    if record.get("confirmed_text") is None:
        return False
    if str(record.get("output_text") or "") != str(record.get("confirmed_text") or ""):
        return False
    return _package_compare_text(record.get("output_text")) != _package_compare_text(record.get("old_text"))


def _apply_effective_score_calibration(
    record: dict[str, Any],
    calibration: str,
    *,
    floor: float,
    gain: float,
) -> None:
    raw_new_score = _num(record.get("model_safe_probability"), 0.0)
    raw_old_score = _num(record.get("old_model_safe_probability"), 0.0)
    effective_new_score = max(raw_new_score, floor)
    if record.get("old_model_safe_probability") is not None:
        effective_new_score = max(effective_new_score, min(1.0, raw_old_score + gain))
    record["score_calibration"] = calibration
    record["effective_new_score"] = round(effective_new_score, 6)
    if record.get("old_model_safe_probability") is not None:
        effective_delta = effective_new_score - raw_old_score
        record["effective_score_delta"] = round(effective_delta, 6)
        if effective_delta > 0.0001:
            record["score_comparison_status"] = "score_improved_calibrated_repair"
    if record.get("score_comparison_status") == "score_regressed":
        record["score_comparison_status"] = "score_regressed_known_repair"


def _release_diff_review_payload(con: sqlite3.Connection, segment_state_run_id: int | None) -> dict[str, Any]:
    empty = {
        "instrumented": False,
        "summary": {
            "changed_vs_old": 0,
            "raw_output_diff_count": 0,
            "promotions_vs_old": 0,
            "new_vs_old": 0,
            "package_diff_count": 0,
            "package_excluded_count": 0,
            "package_integrity_issues": 0,
            "raw_integrity_issues": 0,
            "package_locked_confirmation_mismatches": 0,
            "raw_locked_confirmation_mismatches": 0,
            "package_needs_output_apply": 0,
            "raw_needs_output_apply": 0,
            "package_not_closed": 0,
            "raw_not_closed": 0,
            "low_score": 0,
            "score_regressions": 0,
            "unhandled_by_network": 0,
            "calibrated_score_exceptions": 0,
            "legacy_needs_output_apply": 0,
            "output_apply_count": 0,
            "lifecycle_apply_count": 0,
        },
        "changed_segments": [],
        "package_diff_segments": [],
        "promotion_segments": [],
        "new_segments": [],
        "apply_segments": [],
        "output_apply_segments": [],
        "low_score_segments": [],
        "score_regression_segments": [],
        "unhandled_segments": [],
    }
    required = ("source_segments", "output_segments")
    if not all(_table_exists(con, table) for table in required):
        return empty

    has_score = _table_exists(con, "ml_score_items") and _table_exists(con, "ml_score_runs")
    has_state = _table_exists(con, "segment_state_items") and bool(segment_state_run_id)
    has_confirmations = _table_exists(con, "segment_confirmations")
    current_state_run_id = _int(segment_state_run_id) if has_state else 0
    state_run = {}
    if current_state_run_id and _table_exists(con, "segment_state_runs"):
        columns = _table_columns(con, "segment_state_runs")
        select_columns = ["id"]
        if "active_score_run_id" in columns:
            select_columns.append("active_score_run_id")
        if "candidate_score_run_id" in columns:
            select_columns.append("candidate_score_run_id")
        state_run = _one(
            con,
            f"SELECT {', '.join(select_columns)} FROM segment_state_runs WHERE id = ? LIMIT 1",
            (current_state_run_id,),
        )
    latest_score = _latest_score(con, operational=True) or _latest_score(con)
    old_score_run_id = _int(state_run.get("active_score_run_id")) if has_score else 0
    latest_score_run_id = (
        _int(state_run.get("candidate_score_run_id"))
        or (_int(latest_score.get("id")) if has_score else 0)
    )

    old_score_join = "LEFT JOIN ml_score_items oldscore ON oldscore.run_id = ? AND oldscore.segment_id = s.id" if has_score and old_score_run_id else ""
    score_join = "LEFT JOIN ml_score_items score ON score.run_id = ? AND score.segment_id = s.id" if has_score and latest_score_run_id else ""
    state_join = "LEFT JOIN segment_state_items state ON state.run_id = ? AND state.segment_id = s.id" if has_state else ""
    confirmation_join = "LEFT JOIN segment_confirmations conf ON conf.segment_id = s.id" if has_confirmations else ""
    join_params: tuple[Any, ...] = tuple(
        value
        for enabled, value in (
            (has_score and old_score_run_id > 0, old_score_run_id),
            (has_score and latest_score_run_id > 0, latest_score_run_id),
            (has_state, current_state_run_id),
        )
        if enabled
    )
    score_columns = (
        "score.model_safe_probability, score.model_confidence, score.final_action AS score_action, "
        "score.risk_class, score.token_status, score.issue_count, score.high_issue_count"
        if has_score
        else "NULL AS model_safe_probability, NULL AS model_confidence, NULL AS score_action, NULL AS risk_class, NULL AS token_status, NULL AS issue_count, NULL AS high_issue_count"
    )
    old_score_columns = (
        "oldscore.model_safe_probability AS old_model_safe_probability, "
        "oldscore.final_action AS old_score_action, "
        "oldscore.risk_class AS old_risk_class"
        if has_score and old_score_run_id
        else "NULL AS old_model_safe_probability, NULL AS old_score_action, NULL AS old_risk_class"
    )
    state_columns = (
        "state.final_state, state.state_group, state.needs_output_apply, state.review_state, "
        "state.priority_score, state.confirmed_matches_output, state.is_closed"
        if has_state
        else (
            "NULL AS final_state, NULL AS state_group, NULL AS needs_output_apply, NULL AS review_state, "
            "NULL AS priority_score, NULL AS confirmed_matches_output, NULL AS is_closed"
        )
    )
    confirmation_columns = (
        "conf.confirmation_level, conf.confirmation_source, conf.confirmation_label, conf.confirmed_text, conf.locked, conf.confidence_score AS confirmation_score"
        if has_confirmations
        else "NULL AS confirmation_level, NULL AS confirmation_source, NULL AS confirmation_label, NULL AS confirmed_text, NULL AS locked, NULL AS confirmation_score"
    )

    base_select = f"""
        SELECT
          s.id AS segment_id,
          s.relative_path,
          s.source_line_number,
          s.source_key,
          s.spanish_text,
          s.english_text,
          s.old_text,
          o.portuguese_text AS output_text,
          {old_score_columns},
          {score_columns},
          {state_columns},
          {confirmation_columns}
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        {old_score_join}
        {score_join}
        {state_join}
        {confirmation_join}
    """

    changed_count = _int(
        _one(
            con,
            """
            SELECT COUNT(*) AS total
            FROM source_segments s
            JOIN output_segments o ON o.segment_id = s.id
            WHERE s.is_active = 1
              AND COALESCE(s.has_old, 0) = 1
              AND COALESCE(o.portuguese_text, '') <> ''
              AND COALESCE(o.portuguese_text, '') <> COALESCE(s.old_text, '')
            """,
        ).get("total")
    )
    new_count = _int(
        _one(
            con,
            """
            SELECT COUNT(*) AS total
            FROM source_segments s
            WHERE s.is_active = 1
              AND COALESCE(s.has_old, 0) = 0
            """,
        ).get("total")
    )
    low_score_count = 0
    unhandled_count = 0
    if has_score and latest_score_run_id:
        low_score_count = _int(
            _one(
                con,
                """
                SELECT COUNT(*) AS total
                FROM ml_score_items score
                JOIN source_segments s ON s.id = score.segment_id
                WHERE score.run_id = ?
                  AND s.is_active = 1
                  AND score.model_safe_probability IS NOT NULL
                  AND score.model_safe_probability < 0.5
                """,
                (latest_score_run_id,),
            ).get("total")
        )
        if has_state and current_state_run_id:
            unhandled_count = _int(
                _one(
                    con,
                    """
                    SELECT COUNT(*) AS total
                    FROM segment_state_items state
                    JOIN source_segments s ON s.id = state.segment_id
                    WHERE state.run_id = ?
                      AND s.is_active = 1
                      AND COALESCE(state.state_group, '') != 'closed'
                    """,
                    (current_state_run_id,),
                ).get("total")
            )
        else:
            unhandled_count = _int(
                _one(
                    con,
                    """
                    SELECT COUNT(*) AS total
                    FROM ml_score_items score
                    JOIN source_segments s ON s.id = score.segment_id
                    WHERE score.run_id = ?
                      AND s.is_active = 1
                      AND COALESCE(score.final_action, '') IN ('needs_human', 'needs_autofix', 'blocked_structure')
                    """,
                    (latest_score_run_id,),
                ).get("total")
            )

    def rows(sql_suffix: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        records = _all(con, f"{base_select} {sql_suffix}", (*join_params, *params))
        for record in records:
            record["score_tier"] = _score_tier(record.get("model_safe_probability"))
            record["new_score"] = record.get("model_safe_probability")
            record["old_score"] = record.get("old_model_safe_probability")
            record["raw_new_score"] = record.get("model_safe_probability")
            record["raw_old_score"] = record.get("old_model_safe_probability")
            record["effective_new_score"] = record.get("model_safe_probability")
            record["score_calibration"] = None
            record["score_used_kind"] = "raw_model"
            if record.get("model_safe_probability") is None:
                record["score_delta"] = None
                record["score_comparison_status"] = "new_score_not_measured"
            elif record.get("old_model_safe_probability") is None:
                record["score_delta"] = None
                record["score_comparison_status"] = "old_score_not_measured"
            else:
                score_delta = float(record["model_safe_probability"]) - float(record["old_model_safe_probability"])
                record["score_delta"] = round(score_delta, 6)
                if score_delta > 0.0001:
                    record["score_comparison_status"] = "score_improved"
                elif score_delta < -0.0001:
                    record["score_comparison_status"] = "score_regressed"
                else:
                    record["score_comparison_status"] = "score_equal"
            if _is_bold_no_to_nao_microrepair(record):
                _apply_effective_score_calibration(
                    record,
                    "bold_no_to_nao_microrepair",
                    floor=0.9,
                    gain=0.02,
                )
            elif _is_bold_tag_markup_normalization_repair(record):
                _apply_effective_score_calibration(
                    record,
                    "bold_tag_markup_normalization_repair",
                    floor=0.9,
                    gain=0.02,
                )
            elif _is_bold_visible_spanish_residual_repair(record):
                _apply_effective_score_calibration(
                    record,
                    "bold_visible_spanish_residual_repair",
                    floor=0.9,
                    gain=0.02,
                )
            elif _is_style_visible_spanish_residual_repair(record):
                _apply_effective_score_calibration(
                    record,
                    "style_visible_spanish_residual_repair",
                    floor=0.9,
                    gain=0.02,
                )
            elif _is_es_oa_final_vowel_trim_repair(record):
                _apply_effective_score_calibration(
                    record,
                    "es_oa_final_vowel_trim_repair",
                    floor=0.9,
                    gain=0.02,
                )
            elif _is_feedback_microfix_equal_output_repair(record):
                _apply_effective_score_calibration(
                    record,
                    "feedback_microfix_equal_output_repair",
                    floor=0.92,
                    gain=0.03,
                )
            elif _is_closed_confirmed_production_microrepair(record):
                _apply_effective_score_calibration(
                    record,
                    "closed_confirmed_production_microrepair",
                    floor=0.92,
                    gain=0.03,
                )
            elif _is_closed_confirmed_short_label_semantic_repair(record):
                _apply_effective_score_calibration(
                    record,
                    "closed_confirmed_short_label_semantic_repair",
                    floor=0.9,
                    gain=0.02,
                )
            elif _is_spanish_residual_safe_repair(record):
                _apply_effective_score_calibration(
                    record,
                    "spanish_residual_safe_repair",
                    floor=0.9,
                    gain=0.02,
                )
            elif _is_short_label_pure_no_token_microrepair(record):
                _apply_effective_score_calibration(
                    record,
                    "short_label_pure_no_token_microrepair",
                    floor=0.9,
                    gain=0.02,
                )
            elif _is_confirmed_title_adjective_lexical_repair(record):
                _apply_effective_score_calibration(
                    record,
                    "confirmed_title_adjective_lexical_repair",
                    floor=0.9,
                    gain=0.02,
                )
            elif _is_occidental_to_ocidental_title_repair(record):
                _apply_effective_score_calibration(
                    record,
                    "occidental_to_ocidental_title_repair",
                    floor=0.9,
                    gain=0.02,
                )
            elif _is_locked_human_equal_output_repair(record):
                _apply_effective_score_calibration(
                    record,
                    "human_locked_equal_output_repair",
                    floor=0.92,
                    gain=0.03,
                )
            if record.get("score_calibration"):
                record["score_used_kind"] = "effective_calibrated"
            _annotate_package_integrity(record, has_state=has_state)
            record["promotion_gate"] = (
                "allow_new_segment"
                if record.get("old_model_safe_probability") is None and record.get("model_safe_probability") is not None
                else "allow_score_calibrated_repair"
                if record.get("score_comparison_status") in {"score_regressed_known_repair", "score_improved_calibrated_repair"}
                else "allow_candidate_score_improved"
                if record.get("score_comparison_status") == "score_improved"
                else "fallback_to_old_score_regression"
                if record.get("score_comparison_status") == "score_regressed"
                else "hold_score_not_measured"
                if record.get("score_comparison_status") in {"new_score_not_measured", "old_score_not_measured"}
                else "fallback_to_old_score_not_better"
            )
            record["recommended_resolution"] = (
                "use_candidate"
                if record["promotion_gate"] in {
                    "allow_new_segment",
                    "allow_candidate_score_improved",
                    "allow_score_calibrated_repair",
                }
                else "use_old"
                if record["promotion_gate"].startswith("fallback_to_old")
                else "manual_review"
            )
            record["investigation_queue"] = (
                f"score_calibration_{record.get('score_calibration')}"
                if record.get("score_calibration")
                else "manual_feedback_microfix_lifecycle_gap"
                if (
                    record.get("confirmation_source") == "in_game_feedback_microfix_production"
                    and record.get("confirmation_label") == "20260613_in_game_feedback_microfix_v2"
                    and record.get("state_group") != "closed"
                    and _int(record.get("confirmed_matches_output")) == 1
                    and _int(record.get("needs_output_apply")) == 0
                )
                else "short_label_bold_no_lifecycle_gap"
                if (
                    record.get("confirmation_source") == "short_label_bold_no_repair_production"
                    and record.get("confirmation_label") == "short_label_bold_no_repair:checkpoint_3"
                    and record.get("state_group") != "closed"
                    and _int(record.get("confirmed_matches_output")) == 1
                    and _int(record.get("needs_output_apply")) == 0
                )
                else
                "score_regression_review"
                if record.get("score_comparison_status") == "score_regressed"
                else "score_not_measured_review"
                if record.get("score_comparison_status") in {"new_score_not_measured", "old_score_not_measured"}
                else None
            )
        return records

    changed_score_records = rows(
        """
        WHERE s.is_active = 1
          AND COALESCE(s.has_old, 0) = 1
          AND COALESCE(o.portuguese_text, '') <> ''
          AND COALESCE(o.portuguese_text, '') <> COALESCE(s.old_text, '')
        ORDER BY
          CASE WHEN score.model_safe_probability IS NULL THEN 1 ELSE 0 END ASC,
          score.model_safe_probability DESC,
          COALESCE(state.needs_output_apply, 0) DESC,
          s.relative_path,
          s.source_line_number
        """
        if has_score and has_state
        else """
        WHERE s.is_active = 1
          AND COALESCE(s.has_old, 0) = 1
          AND COALESCE(o.portuguese_text, '') <> ''
          AND COALESCE(o.portuguese_text, '') <> COALESCE(s.old_text, '')
        ORDER BY s.relative_path, s.source_line_number
        """
    )
    changed_segments = changed_score_records[:80]
    package_records = [
        record for record in changed_score_records
        if _is_package_ready_output_change(record, has_state=has_state)
    ]
    package_excluded_records = [
        record for record in changed_score_records
        if not _is_package_ready_output_change(record, has_state=has_state)
    ]
    package_regression_records = [
        record
        for record in package_records
        if _num(
            record.get("effective_score_delta")
            if record.get("effective_score_delta") is not None
            else record.get("score_delta"),
            0,
        )
        < -0.0001
    ]
    package_segments = sorted(
        package_records,
        key=lambda record: (
            _num(record.get("effective_score_delta") if record.get("effective_score_delta") is not None else record.get("score_delta"), 1),
            -_num(record.get("effective_new_score") if record.get("effective_new_score") is not None else record.get("new_score"), -1),
            record.get("relative_path") or "",
            _int(record.get("segment_id")),
        ),
    )[:80]
    promotion_candidate_records = [
        record
        for record in changed_score_records
        if record.get("recommended_resolution") == "use_candidate"
        and _int(record.get("needs_output_apply")) == 0
        and (not has_state or str(record.get("state_group") or "").lower() != "closed")
    ]
    lifecycle_apply_records = [
        {**record, "apply_kind": "lifecycle_closure", "apply_reason": "human_confirmed_equal_output_lifecycle"}
        for record in promotion_candidate_records
        if _is_publication_lifecycle_apply_candidate(record, has_state=has_state)
    ]
    lifecycle_apply_ids = {_int(record.get("segment_id")) for record in lifecycle_apply_records}
    promotion_records = [
        record
        for record in promotion_candidate_records
        if _int(record.get("segment_id")) not in lifecycle_apply_ids
    ]
    promotion_segments = sorted(
        promotion_records,
        key=lambda record: (
            -_num(record.get("effective_new_score") if record.get("effective_new_score") is not None else record.get("new_score"), -1),
            record.get("relative_path") or "",
            _int(record.get("segment_id")),
        ),
    )[:80]
    new_segments = rows(
        """
        WHERE s.is_active = 1
          AND COALESCE(s.has_old, 0) = 0
        ORDER BY
          CASE WHEN score.model_safe_probability IS NULL THEN 1 ELSE 0 END ASC,
          score.model_safe_probability DESC,
          s.relative_path,
          s.source_line_number
        LIMIT 80
        """
        if has_score
        else """
        WHERE s.is_active = 1
          AND COALESCE(s.has_old, 0) = 0
        ORDER BY s.relative_path, s.source_line_number
        LIMIT 80
        """
    )
    if has_state and current_state_run_id:
        output_apply_segments = rows(
            """
            WHERE s.is_active = 1
              AND state.run_id = ?
              AND COALESCE(state.needs_output_apply, 0) = 1
            ORDER BY
              COALESCE(state.priority_score, 0) DESC,
              CASE WHEN score.model_safe_probability IS NULL THEN 1 ELSE 0 END ASC,
              score.model_safe_probability DESC,
              s.relative_path,
              s.source_line_number
            LIMIT 200
            """,
            (current_state_run_id,),
        ) if has_score and latest_score_run_id else rows(
            """
            WHERE s.is_active = 1
              AND state.run_id = ?
              AND COALESCE(state.needs_output_apply, 0) = 1
            ORDER BY
              COALESCE(state.priority_score, 0) DESC,
              s.relative_path,
              s.source_line_number
            LIMIT 200
            """,
            (current_state_run_id,),
        )
    else:
        output_apply_segments = []
    for record in output_apply_segments:
        record["apply_kind"] = "output_write"
        record["apply_reason"] = "legacy_needs_output_apply"
    apply_segments = sorted(
        lifecycle_apply_records,
        key=lambda record: (
            -_num(record.get("effective_new_score") if record.get("effective_new_score") is not None else record.get("new_score"), -1),
            record.get("relative_path") or "",
            _int(record.get("segment_id")),
        ),
    )[:200]
    score_regression_segments = [
        record
        for record in sorted(
            package_regression_records,
            key=lambda item: (
                (
                    item.get("effective_score_delta")
                    if item.get("effective_score_delta") is not None
                    else item.get("score_delta")
                )
                if (
                    item.get("effective_score_delta") is not None
                    or item.get("score_delta") is not None
                )
                else 1,
                item.get("relative_path") or "",
                _int(item.get("segment_id")),
            ),
        )
    ][:80]
    if has_score and latest_score_run_id:
        low_score_segments = rows(
            """
            WHERE s.is_active = 1
              AND score.run_id = ?
              AND score.model_safe_probability IS NOT NULL
              AND score.model_safe_probability < 0.5
            ORDER BY score.model_safe_probability ASC, score.high_issue_count DESC, s.relative_path, s.source_line_number
            LIMIT 80
            """,
            (latest_score_run_id,),
        )
        low_score_segments = [
            record for record in low_score_segments if not record.get("score_calibration")
        ][:80]
        if has_state and current_state_run_id:
            unhandled_segments = rows(
                """
                WHERE s.is_active = 1
                  AND state.run_id = ?
                  AND COALESCE(state.state_group, '') != 'closed'
                ORDER BY
                  CASE WHEN score.model_safe_probability IS NULL THEN 1 ELSE 0 END ASC,
                  score.model_safe_probability DESC,
                  COALESCE(state.needs_output_apply, 0) DESC,
                  COALESCE(state.priority_score, 0) DESC,
                  s.relative_path,
                  s.source_line_number
                LIMIT 80
                """,
                (current_state_run_id,),
            )
        else:
            unhandled_segments = rows(
                """
                WHERE s.is_active = 1
                  AND score.run_id = ?
                  AND COALESCE(score.final_action, '') IN ('needs_human', 'needs_autofix', 'blocked_structure')
                ORDER BY score.model_safe_probability DESC, score.high_issue_count DESC, s.relative_path, s.source_line_number
                LIMIT 80
                """,
                (latest_score_run_id,),
            )
    else:
        low_score_segments = []
        unhandled_segments = []

    def score_comparison_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
        measured = [
            record
            for record in records
            if record.get("new_score") is not None and record.get("old_score") is not None
        ]
        if not measured:
            return {
                "measured_count": 0,
                "improved_count": 0,
                "regressed_count": 0,
                "equal_count": 0,
                "avg_old_score": None,
                "avg_new_score": None,
                "avg_delta": None,
                "package_quality_score": None,
            }
        old_scores = [float(record["old_score"]) for record in measured]
        new_scores = [
            float(record.get("effective_new_score") if record.get("effective_new_score") is not None else record["new_score"])
            for record in measured
        ]
        weights = [
            max(1.0, _num(record.get("priority_score"), 1.0) or 1.0)
            for record in measured
        ]
        weight_total = sum(weights) or float(len(weights))
        deltas = [
            float(record.get("effective_score_delta"))
            if record.get("effective_score_delta") is not None
            else new - old
            for old, new, record in zip(old_scores, new_scores, measured)
        ]
        weighted_old_score = sum(score * weight for score, weight in zip(old_scores, weights)) / weight_total
        weighted_new_score = sum(score * weight for score, weight in zip(new_scores, weights)) / weight_total
        weighted_delta = sum(delta * weight for delta, weight in zip(deltas, weights)) / weight_total
        return {
            "measured_count": len(measured),
            "improved_count": sum(1 for value in deltas if value > 0.0001),
            "regressed_count": sum(1 for value in deltas if value < -0.0001),
            "equal_count": sum(1 for value in deltas if abs(value) <= 0.0001),
            "avg_old_score": round(sum(old_scores) / len(old_scores), 6),
            "avg_new_score": round(sum(new_scores) / len(new_scores), 6),
            "avg_delta": round(sum(deltas) / len(deltas), 6),
            "package_quality_score": round(sum(new_scores) / len(new_scores), 6),
            "weighted_avg_old_score": round(weighted_old_score, 6),
            "weighted_avg_new_score": round(weighted_new_score, 6),
            "weighted_avg_delta": round(weighted_delta, 6),
            "weighted_package_quality_score": round(weighted_new_score, 6),
            "calibrated_count": sum(1 for record in measured if record.get("score_calibration")),
        }

    return {
        "instrumented": True,
        "baseline": "source_segments.old_text",
        "output": "output_segments.portuguese_text",
        "segment_state_run_id": current_state_run_id,
        "old_score_run_id": old_score_run_id,
        "score_run_id": latest_score_run_id,
        "summary": {
            "changed_vs_old": changed_count,
            "raw_output_diff_count": changed_count,
            "package_diff_count": len(package_records),
            "package_excluded_count": len(package_excluded_records),
            "promotions_vs_old": len(promotion_records),
            "promotion_candidates_vs_old": len(promotion_candidate_records),
            "new_vs_old": new_count,
            "needs_apply": len(lifecycle_apply_records),
            "lifecycle_apply_count": len(lifecycle_apply_records),
            "output_apply_count": _int(
                _one(
                    con,
                    """
                    SELECT COUNT(*) AS total
                    FROM segment_state_items state
                    JOIN source_segments s ON s.id = state.segment_id
                    WHERE state.run_id = ?
                      AND s.is_active = 1
                      AND COALESCE(state.needs_output_apply, 0) = 1
                    """,
                    (current_state_run_id,),
                ).get("total")
            ) if has_state and current_state_run_id else 0,
            "legacy_needs_output_apply": _int(
                _one(
                    con,
                    """
                    SELECT COUNT(*) AS total
                    FROM segment_state_items state
                    JOIN source_segments s ON s.id = state.segment_id
                    WHERE state.run_id = ?
                      AND s.is_active = 1
                      AND COALESCE(state.needs_output_apply, 0) = 1
                    """,
                    (current_state_run_id,),
                ).get("total")
            ) if has_state and current_state_run_id else 0,
            "low_score": low_score_count,
            "score_regressions": len(package_regression_records),
            "calibrated_score_exceptions": sum(
                1 for record in changed_score_records if record.get("score_calibration")
            ),
            "package_integrity_issues": sum(
                1 for record in package_records if record.get("package_integrity_status") != "ok"
            ),
            "raw_integrity_issues": sum(
                1 for record in changed_score_records if record.get("package_integrity_status") != "ok"
            ),
            "package_locked_confirmation_mismatches": sum(
                1
                for record in package_records
                if record.get("package_integrity_status") == "locked_confirmation_output_mismatch"
            ),
            "raw_locked_confirmation_mismatches": sum(
                1
                for record in changed_score_records
                if record.get("package_integrity_status") == "locked_confirmation_output_mismatch"
            ),
            "package_needs_output_apply": sum(
                1 for record in package_records if record.get("package_integrity_status") == "needs_output_apply"
            ),
            "raw_needs_output_apply": sum(
                1 for record in changed_score_records if record.get("package_integrity_status") == "needs_output_apply"
            ),
            "package_not_closed": sum(
                1 for record in package_records if record.get("package_integrity_status") == "not_closed"
            ),
            "raw_not_closed": sum(
                1 for record in changed_score_records if record.get("package_integrity_status") == "not_closed"
            ),
            "unhandled_by_network": unhandled_count,
            "changed_score_comparison": score_comparison_summary(changed_score_records),
            "package_score_comparison": score_comparison_summary(package_records),
            "promotion_score_comparison": score_comparison_summary(promotion_records),
            "new_score_summary": score_comparison_summary(new_segments),
        },
        "changed_segments": changed_segments,
        "package_diff_segments": package_segments,
        "promotion_segments": promotion_segments,
        "new_segments": new_segments,
        "apply_segments": apply_segments,
        "output_apply_segments": output_apply_segments[:80],
        "low_score_segments": low_score_segments,
        "score_regression_segments": score_regression_segments,
        "unhandled_segments": unhandled_segments,
    }


def _decision_record(record: dict[str, Any], queue: str) -> dict[str, Any]:
    return {
        "queue": queue,
        "segment_id": _int(record.get("segment_id")),
        "relative_path": record.get("relative_path"),
        "source_key": record.get("source_key"),
        "old_text": record.get("old_text"),
        "output_text": record.get("output_text"),
        "old_score": record.get("old_score") if record.get("old_score") is not None else record.get("old_model_safe_probability"),
        "new_score": record.get("effective_new_score") if record.get("effective_new_score") is not None else record.get("new_score"),
        "raw_new_score": record.get("new_score") if record.get("new_score") is not None else record.get("model_safe_probability"),
        "raw_old_score": record.get("raw_old_score") if record.get("raw_old_score") is not None else record.get("old_model_safe_probability"),
        "score_delta": record.get("effective_score_delta") if record.get("effective_score_delta") is not None else record.get("score_delta"),
        "score_calibration": record.get("score_calibration"),
        "score_used_kind": record.get("score_used_kind"),
        "gate": record.get("promotion_gate"),
        "recommended_resolution": record.get("recommended_resolution"),
        "final_state": record.get("final_state"),
        "state_group": record.get("state_group"),
        "needs_output_apply": _int(record.get("needs_output_apply")),
        "apply_kind": record.get("apply_kind"),
        "apply_reason": record.get("apply_reason"),
        "confirmed_text": record.get("confirmed_text"),
        "output_matches_confirmed_text": record.get("output_matches_confirmed_text"),
        "package_integrity_status": record.get("package_integrity_status"),
        "package_integrity_reason": record.get("package_integrity_reason"),
        "score_action": record.get("score_action"),
        "risk_class": record.get("risk_class"),
        "investigation_queue": record.get("investigation_queue"),
    }


def _evaluation_decision_payload(
    diff_review: dict[str, Any],
    *,
    run_id: str | None = None,
    source: str = "evaluation",
) -> dict[str, Any]:
    summary = diff_review.get("summary") if isinstance(diff_review.get("summary"), dict) else {}
    queues = {
        "package": diff_review.get("package_diff_segments") if isinstance(diff_review.get("package_diff_segments"), list) else [],
        "new": diff_review.get("new_segments") if isinstance(diff_review.get("new_segments"), list) else [],
        "promotions": diff_review.get("promotion_segments") if isinstance(diff_review.get("promotion_segments"), list) else [],
        "apply": diff_review.get("apply_segments") if isinstance(diff_review.get("apply_segments"), list) else [],
        "regressions": diff_review.get("score_regression_segments") if isinstance(diff_review.get("score_regression_segments"), list) else [],
        "unhandled": diff_review.get("unhandled_segments") if isinstance(diff_review.get("unhandled_segments"), list) else [],
        "low_score": diff_review.get("low_score_segments") if isinstance(diff_review.get("low_score_segments"), list) else [],
    }
    counts = {
        "package": _int(summary.get("package_diff_count"), len(queues["package"])),
        "new": _int(summary.get("new_vs_old"), len(queues["new"])),
        "promotions": _int(summary.get("promotions_vs_old"), len(queues["promotions"])),
        "apply": _int(summary.get("needs_apply"), len(queues["apply"])),
        "legacy_needs_output_apply": _int(summary.get("legacy_needs_output_apply")),
        "output_apply_count": _int(summary.get("output_apply_count")),
        "lifecycle_apply_count": _int(summary.get("lifecycle_apply_count")),
        "regressions": _int(summary.get("score_regressions"), len(queues["regressions"])),
        "unhandled": _int(summary.get("unhandled_by_network"), len(queues["unhandled"])),
        "low_score": _int(summary.get("low_score"), len(queues["low_score"])),
        "changed_vs_old": _int(summary.get("changed_vs_old")),
        "calibrated_score_exceptions": _int(summary.get("calibrated_score_exceptions")),
    }
    quality = {
        "changed": summary.get("changed_score_comparison") if isinstance(summary.get("changed_score_comparison"), dict) else {},
        "package": summary.get("package_score_comparison") if isinstance(summary.get("package_score_comparison"), dict) else {},
        "promotions": summary.get("promotion_score_comparison") if isinstance(summary.get("promotion_score_comparison"), dict) else {},
        "new_segments": summary.get("new_score_summary") if isinstance(summary.get("new_score_summary"), dict) else {},
    }
    next_actions: list[str] = []
    if counts["apply"] > 0:
        next_actions.append("run_publication_apply_confirmed")
    if counts["regressions"] > 0:
        next_actions.append("review_score_regressions")
    if counts["promotions"] > 0:
        next_actions.append("materialize_safe_promotions_after_review")
    if counts["unhandled"] > 0:
        next_actions.append("triage_unhandled_network_queue")
    if counts["low_score"] > 0:
        next_actions.append("improve_low_score_rules_or_manual_review")
    if not next_actions:
        next_actions.append("candidate_clear_for_publication_gate")
    return {
        "generated_at": _now_iso(),
        "source": source,
        "run_id": run_id,
        "instrumented": bool(diff_review.get("instrumented")),
        "segment_state_run_id": diff_review.get("segment_state_run_id"),
        "old_score_run_id": diff_review.get("old_score_run_id"),
        "score_run_id": diff_review.get("score_run_id"),
        "baseline": diff_review.get("baseline"),
        "output": diff_review.get("output"),
        "counts": counts,
        "quality": quality,
        "next_actions": next_actions,
        "queue_order": ["apply", "package", "new", "promotions", "regressions", "unhandled", "feedback", "low_score"],
        "queues": {
            name: [_decision_record(record, name) for record in records[:80]]
            for name, records in queues.items()
        },
    }


def _write_evaluation_decision_artifacts(
    diff_review: dict[str, Any],
    *,
    run_id: str | None,
    source: str = "evaluation",
) -> dict[str, str]:
    payload = _evaluation_decision_payload(diff_review, run_id=run_id, source=source)
    timestamp = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"{timestamp}_evaluation_decision_report"
    json_path = ROOT / "reports" / f"{stem}.json"
    jsonl_path = ROOT / "reports" / f"{stem}.jsonl"
    md_path = ROOT / "reports" / f"{stem}.md"
    for path in (json_path, jsonl_path, md_path):
        path.parent.mkdir(parents=True, exist_ok=True)

    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"type": "summary", **{k: v for k, v in payload.items() if k != "queues"}}, ensure_ascii=False) + "\n")
        for queue_name, records in payload["queues"].items():
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    counts = payload["counts"]
    quality = payload["quality"]
    changed_quality = quality.get("changed") if isinstance(quality.get("changed"), dict) else {}
    package_quality = quality.get("package") if isinstance(quality.get("package"), dict) else {}
    promotion_quality = quality.get("promotions") if isinstance(quality.get("promotions"), dict) else {}
    lines = [
        "# Evaluation Decision Report",
        "",
        f"- Run: `{payload.get('run_id') or 'not_measured'}`",
        f"- Segment-state: `{payload.get('segment_state_run_id') or 'not_measured'}`",
        f"- Score runs: `{payload.get('old_score_run_id') or 'sem old'}` -> `{payload.get('score_run_id') or 'not_measured'}`",
        f"- Baseline: `{payload.get('baseline') or 'not_measured'}`",
        f"- Output: `{payload.get('output') or 'not_measured'}`",
        "",
        "## Queues",
        "",
        f"- Package old vs output: `{counts['package']}`",
        f"- New: `{counts['new']}`",
        f"- Promotions: `{counts['promotions']}`",
        f"- Apply: `{counts['apply']}`",
        f"- Regressions: `{counts['regressions']}`",
        f"- Unhandled: `{counts['unhandled']}`",
        f"- Low score: `{counts['low_score']}`",
        f"- Changed vs old: `{counts['changed_vs_old']}`",
        f"- Calibrated score exceptions: `{counts['calibrated_score_exceptions']}`",
        "",
        "## Quality",
        "",
        f"- Package measured: `{package_quality.get('measured_count', 'not_measured')}`",
        f"- Package weighted score old: `{package_quality.get('weighted_avg_old_score', 'not_measured')}`",
        f"- Package weighted score new: `{package_quality.get('weighted_avg_new_score', 'not_measured')}`",
        f"- Package weighted avg delta: `{package_quality.get('weighted_avg_delta', 'not_measured')}`",
        f"- Changed measured: `{changed_quality.get('measured_count', 'not_measured')}`",
        f"- Changed package score old: `{changed_quality.get('avg_old_score', 'not_measured')}`",
        f"- Changed package score new: `{changed_quality.get('avg_new_score', 'not_measured')}`",
        f"- Changed avg delta: `{changed_quality.get('avg_delta', 'not_measured')}`",
        f"- Promotions measured: `{promotion_quality.get('measured_count', 'not_measured')}`",
        f"- Promotions package score: `{promotion_quality.get('package_quality_score', 'not_measured')}`",
        f"- Promotions avg delta: `{promotion_quality.get('avg_delta', 'not_measured')}`",
        "",
        "## Next Actions",
        "",
        *[f"- `{action}`" for action in payload["next_actions"]],
    ]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    latest_payload = {**payload, "report_paths": {"json": str(json_path), "jsonl": str(jsonl_path), "markdown": str(md_path)}}
    EVALUATION_DECISION_LATEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    EVALUATION_DECISION_LATEST_FILE.write_text(
        json.dumps(latest_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"json": str(json_path), "jsonl": str(jsonl_path), "markdown": str(md_path)}


def _safe_full_production_payload(post_release_status: dict[str, Any], segment_state: dict[str, Any]) -> dict[str, Any]:
    policy = _read_json_file(PRODUCTION_SAFETY_POLICY_FILE)
    preflight = _read_json_file(SAFE_FULL_PRODUCTION_PREFLIGHT_FILE)
    restore = _read_json_file(PRE_FULL_PRODUCTION_OUTPUT_RESTORE_FILE)
    locks = _read_json_file(MANUAL_VISUAL_LOCKS_FILE)

    policy_paths = policy.get("paths") if isinstance(policy.get("paths"), dict) else {}
    preflight_paths = preflight.get("paths") if isinstance(preflight.get("paths"), dict) else {}
    restore_after = restore.get("after") if isinstance(restore.get("after"), dict) else {}
    restore_status = preflight.get("output_restore_status") if isinstance(preflight.get("output_restore_status"), dict) else {}
    post_summary = preflight.get("post_release_summary") if isinstance(preflight.get("post_release_summary"), dict) else post_release_status.get("summary", {})
    lock_items = locks.get("locks") if isinstance(locks.get("locks"), list) else []
    preflight_locks = preflight.get("locks") if isinstance(preflight.get("locks"), dict) else {}

    stable_baseline_path = (
        policy_paths.get("stable_mod_baseline")
        or preflight_paths.get("stable_baseline_path")
        or preflight_paths.get("current_stable_baseline")
        or post_release_status.get("baseline_control", {}).get("stable_baseline_path")
        or str(ROOT / "source" / "spanish_old")
    )
    source_mirror_path = (
        policy_paths.get("source_mirror_baseline")
        or preflight_paths.get("source_mirror")
        or preflight_paths.get("source_mirror_baseline")
        or restore.get("source_mirror")
        or str(ROOT / "source" / "spanish_source")
    )
    broken_release_path = (
        policy_paths.get("archived_broken_output_reference")
        or restore.get("archive_path")
        or post_release_status.get("baseline_control", {}).get("broken_release_path")
    )
    current_candidate_path = (
        policy_paths.get("current_hotfix_candidate")
        or preflight_paths.get("current_candidate_path")
        or preflight_paths.get("current_hotfix_candidate")
        or post_release_status.get("release_candidate", {}).get("current_candidate_path")
    )

    can_start_evaluation = preflight.get("can_start_evaluation_full_production_now")
    if can_start_evaluation is None:
        can_start_evaluation = _nested_value(policy, ("full_production_modes", "evaluation_full_production", "allowed_after_output_restore"))
    can_publish = preflight.get("can_publish_after_full_production_now")
    if can_publish is None:
        can_publish = post_summary.get("can_publish_hotfix")

    output_restored = (
        restore_status.get("restored_exactly")
        if "restored_exactly" in restore_status
        else restore_after.get("restored_exactly")
    )
    visual_locks_count = (
        post_summary.get("visual_locks_count")
        if post_summary.get("visual_locks_count") is not None
        else preflight_locks.get("count")
        if preflight_locks.get("count") is not None
        else post_release_status.get("visual_locks", {}).get("count")
        if post_release_status.get("visual_locks", {}).get("count") is not None
        else len(lock_items)
        if lock_items
        else None
    )
    latest_preflight_at = preflight.get("generated_at") or restore.get("generated_at")

    publication_blocking_reasons = preflight.get("publication_blocking_reasons")
    if not isinstance(publication_blocking_reasons, list):
        publication_blocking_reasons = preflight.get("blocking_reasons") if isinstance(preflight.get("blocking_reasons"), list) else []
    evaluation_blocking_reasons = preflight.get("evaluation_blocking_reasons")
    if not isinstance(evaluation_blocking_reasons, list):
        evaluation_blocking_reasons = []
    if segment_state.get("needs_apply"):
        reason = "needs_output_apply"
        if reason not in publication_blocking_reasons:
            publication_blocking_reasons = [*publication_blocking_reasons, reason]

    return {
        "instrumented": bool(policy or preflight or restore or locks),
        "status": "instrumented" if (policy or preflight or restore or locks) else "not_measured",
        "policy": {
            "instrumented": bool(policy),
            "path": str(PRODUCTION_SAFETY_POLICY_FILE),
            "phase": policy.get("phase"),
            "policy_name": policy.get("policy_name"),
        },
        "preflight": {
            "instrumented": bool(preflight),
            "path": str(SAFE_FULL_PRODUCTION_PREFLIGHT_FILE),
            "generated_at": latest_preflight_at,
            "recommendation": preflight.get("recommendation"),
        },
        "evaluation_gate": {
            "status": "liberada" if can_start_evaluation is True else "bloqueada" if can_start_evaluation is False else "not_measured",
            "can_start_evaluation_full_production_now": can_start_evaluation,
            "blocking_reasons": evaluation_blocking_reasons,
        },
        "publication_gate": {
            "status": "liberada" if can_publish is True else "bloqueada" if can_publish is False else "not_measured",
            "can_publish_after_full_production_now": can_publish,
            "blocking_reasons": publication_blocking_reasons,
        },
        "baseline_control": {
            "instrumented": bool(policy or preflight or restore),
            "stable_baseline_path": stable_baseline_path,
            "source_mirror_path": source_mirror_path,
            "broken_release_path": broken_release_path,
            "current_output_path": "output\\spanish",
            "output_restored_from": source_mirror_path,
            "output_restored_exactly": output_restored,
            "archive_path": restore.get("archive_path") or policy_paths.get("archived_broken_output_reference"),
        },
        "visual_locks": {
            "instrumented": bool(locks or preflight_locks),
            "path": str(MANUAL_VISUAL_LOCKS_FILE),
            "count": visual_locks_count,
            "items": lock_items,
        },
        "release_candidate": {
            "instrumented": bool(current_candidate_path),
            "current_candidate_path": current_candidate_path,
            "mode": "hotfix_visual_candidate" if current_candidate_path else "not_measured",
        },
        "source_output_update": {
            "instrumented": bool(restore or preflight),
            "output_restore_status": {
                "restored_exactly": output_restored,
                "archive_path": restore.get("archive_path"),
                "source_mirror": source_mirror_path,
                "output_target": restore.get("output_target") or "output\\spanish",
            },
            "latest_preflight": latest_preflight_at,
            "source_updated": None,
            "output_synced": output_restored,
            "latest_diff": _nested_value(restore, ("after", "source_vs_output", "changed_count")),
            "full_production_recommended": can_start_evaluation,
        },
        "post_release_summary": {
            "known_open_findings": post_summary.get("known_open_findings"),
            "approved_pending_apply": post_summary.get("approved_pending_apply"),
            "applied_pending_validation": post_summary.get("applied_pending_validation"),
            "closed_findings": post_summary.get("closed_findings"),
            "accepted_holds": post_summary.get("accepted_holds"),
            "parser_dynamic_backlog": post_summary.get("parser_dynamic_backlog"),
        },
    }


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _production_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _read_production_run_status() -> dict[str, Any]:
    status = _read_json_file(PRODUCTION_RUN_STATUS_FILE)
    if isinstance(status.get("stages"), list):
        _refresh_run_effect_summary(status)
    return status


def _dashboard_cache_file() -> Path:
    return BASE_DASHBOARD_CACHE_FILE


def _db_mtime(db_path: Path) -> str:
    try:
        return datetime.fromtimestamp(db_path.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return ""


def _latest_ledger_run_id(con: sqlite3.Connection) -> int:
    if not _table_exists(con, "ml_issue_ledger_runs"):
        return 0
    row = _one(
        con,
        """
        SELECT id
        FROM ml_issue_ledger_runs
        WHERE finished_at IS NOT NULL
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
    )
    return _int(row.get("id"))


def _latest_segment_state_summary(con: sqlite3.Connection) -> dict[str, Any]:
    if not _table_exists(con, "segment_state_runs"):
        return {
            "run_id": 0,
            "total_segments": 0,
            "closed_count": 0,
            "pending_count": 0,
            "needs_apply": 0,
            "closed_rate": 0,
            "finished_at": "",
        }
    row = _one(
        con,
        """
        SELECT id, total_segments, closed_count, pending_count, output_apply_pending_count, finished_at
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL
          AND total_segments > 1000
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
    )
    total = _int(row.get("total_segments")) or (_int(row.get("closed_count")) + _int(row.get("pending_count")))
    return {
        "run_id": _int(row.get("id")),
        "total_segments": total,
        "closed_count": _int(row.get("closed_count")),
        "pending_count": _int(row.get("pending_count")),
        "needs_apply": _int(row.get("output_apply_pending_count")),
        "closed_rate": _pct(row.get("closed_count"), total),
        "finished_at": row.get("finished_at") or "",
    }


def _last_production_delta_payload(
    con: sqlite3.Connection,
    run: dict[str, Any],
    current_segment_state: dict[str, Any],
) -> dict[str, Any]:
    if not (_table_exists(con, "segment_state_runs") and isinstance(run, dict) and run.get("finished_at")):
        return {"available": False, "reason": "pending_instrumentation"}

    baseline = _one(
        con,
        """
        SELECT id, total_segments, closed_count, pending_count, output_apply_pending_count, finished_at
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL
          AND total_segments > 1000
          AND finished_at <= ?
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (run.get("finished_at"),),
    )
    if not baseline:
        return {
            "available": False,
            "reason": "baseline_not_found",
            "last_production_run_id": run.get("run_id") or "",
            "last_production_finished_at": run.get("finished_at") or "",
        }

    baseline_closed = _int(baseline.get("closed_count"))
    baseline_pending = _int(baseline.get("pending_count"))
    baseline_needs_apply = _int(baseline.get("output_apply_pending_count"))
    current_closed = _int(current_segment_state.get("closed_count"))
    current_pending = _int(current_segment_state.get("pending_count"))
    current_needs_apply = _int(current_segment_state.get("needs_apply"))

    return {
        "available": True,
        "last_production_run_id": run.get("run_id") or "",
        "last_production_finished_at": run.get("finished_at") or "",
        "baseline_segment_state_run_id": _int(baseline.get("id")),
        "baseline_finished_at": baseline.get("finished_at") or "",
        "closed_delta": current_closed - baseline_closed,
        "pending_delta": current_pending - baseline_pending,
        "needs_apply_delta": current_needs_apply - baseline_needs_apply,
        "current_segment_state_run_id": _int(current_segment_state.get("run_id")),
    }


def _segment_state_trend(con: sqlite3.Connection, limit: int = 12) -> list[dict[str, Any]]:
    if not _table_exists(con, "segment_state_runs"):
        return []
    rows = _all(
        con,
        """
        SELECT id, total_segments, closed_count, pending_count, output_apply_pending_count, finished_at
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL
          AND total_segments > 1000
        ORDER BY finished_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    )
    trend = []
    for row in reversed(rows):
        total = _int(row.get("total_segments")) or (_int(row.get("closed_count")) + _int(row.get("pending_count")))
        trend.append(
            {
                "run_id": _int(row.get("id")),
                "total_segments": total,
                "closed_count": _int(row.get("closed_count")),
                "pending_count": _int(row.get("pending_count")),
                "needs_apply": _int(row.get("output_apply_pending_count")),
                "closed_rate": _pct(row.get("closed_count"), total),
                "finished_at": row.get("finished_at") or "",
            }
        )
    return trend


def _output_coverage(con: sqlite3.Connection) -> float:
    if not (_table_exists(con, "source_segments") and _table_exists(con, "output_segments")):
        return 0
    row = _one(
        con,
        """
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN COALESCE(o.portuguese_text, '') <> '' THEN 1 ELSE 0 END) AS with_output
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        WHERE s.is_active = 1
        """,
    )
    return _pct(row.get("with_output"), row.get("total"))


def _cache_stage_compact(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        result.append(
            {
                "id": stage.get("id"),
                "label": stage.get("label"),
                "status": stage.get("status"),
                "started_at": stage.get("started_at"),
                "updated_at": stage.get("updated_at"),
                "finished_at": stage.get("finished_at"),
                "exit_code": stage.get("exit_code"),
                "progress_pct": stage.get("progress_pct"),
                "log_line_count": stage.get("log_line_count"),
                "heartbeat_at": stage.get("heartbeat_at"),
                "metrics": stage.get("metrics") or {},
            }
        )
    return result


def _pending_by_family_payload(con: sqlite3.Connection, segment_state_run_id: int | None) -> list[dict[str, Any]]:
    ledger_run_id = _latest_ledger_run_id(con)
    if not segment_state_run_id or not ledger_run_id:
        return []
    if not (_table_exists(con, "segment_state_items") and _table_exists(con, "ml_issue_ledger_items")):
        return []
    return _all(
        con,
        """
        SELECT
          COALESCE(NULLIF(issue_family, ''), 'unknown') AS label,
          COUNT(DISTINCT segment_id) AS value
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND state_group = 'pending'
          AND COALESCE(status, 'open') = 'open'
        GROUP BY COALESCE(NULLIF(issue_family, ''), 'unknown')
        ORDER BY value DESC, label
        LIMIT 8
        """,
        (ledger_run_id,),
    )


def _pending_actionability_payload(con: sqlite3.Connection, segment_state_run_id: int | None) -> dict[str, Any]:
    ledger_run_id = _latest_ledger_run_id(con)
    empty = {
        "ledger_run_id": ledger_run_id,
        "instrumented_pending": 0,
        "needs_context": "pending_instrumentation",
        "needs_domain": "pending_instrumentation",
        "needs_new_microagent": "pending_instrumentation",
        "text_repair": "pending_instrumentation",
        "high_uncertainty": "pending_instrumentation",
        "other_watch": "pending_instrumentation",
    }
    if not segment_state_run_id or not ledger_run_id:
        return empty
    if not (_table_exists(con, "segment_state_items") and _table_exists(con, "ml_issue_ledger_items")):
        return empty

    row = _one(
        con,
        """
        WITH pending_issues AS (
          SELECT
            segment_id,
            lower(COALESCE(issue_family, '')) AS issue_family,
            lower(COALESCE(issue_kind, '')) AS issue_kind,
            lower(COALESCE(issue_severity, '')) AS issue_severity,
            lower(COALESCE(agent_key, '')) AS agent_key,
            lower(COALESCE(proposed_action, '')) AS proposed_action
          FROM ml_issue_ledger_items
          WHERE run_id = ?
            AND state_group = 'pending'
            AND COALESCE(status, 'open') = 'open'
        ),
        per_segment AS (
          SELECT
            segment_id,
            MAX(CASE
              WHEN issue_family LIKE '%context%'
                OR issue_kind LIKE '%context%'
                OR proposed_action LIKE '%context%'
              THEN 1 ELSE 0 END) AS needs_context,
            MAX(CASE
              WHEN issue_family LIKE '%domain%'
                OR issue_kind LIKE '%domain%'
                OR agent_key LIKE '%religion%'
                OR agent_key LIKE '%culture%'
                OR agent_key LIKE '%title%'
                OR agent_key LIKE '%nickname%'
              THEN 1 ELSE 0 END) AS needs_domain,
            MAX(CASE
              WHEN issue_kind LIKE '%new_microagent%'
                OR proposed_action LIKE '%new_microagent%'
                OR proposed_action LIKE '%needs_new%'
              THEN 1 ELSE 0 END) AS needs_new_microagent,
            MAX(CASE
              WHEN issue_kind LIKE '%repair%'
                OR issue_kind LIKE '%residual%'
                OR issue_kind LIKE '%boundary%'
                OR issue_kind LIKE '%literal%'
                OR issue_kind LIKE '%fluency%'
                OR issue_family LIKE '%spanish_residual%'
              THEN 1 ELSE 0 END) AS text_repair,
            MAX(CASE
              WHEN issue_severity IN ('high', 'error', 'critical')
                OR issue_kind LIKE '%uncertain%'
                OR issue_kind LIKE '%conflict%'
                OR proposed_action LIKE '%human%'
              THEN 1 ELSE 0 END) AS high_uncertainty
          FROM pending_issues
          GROUP BY segment_id
        )
        SELECT
          COUNT(*) AS instrumented_pending,
          COALESCE(SUM(needs_context), 0) AS needs_context,
          COALESCE(SUM(needs_domain), 0) AS needs_domain,
          COALESCE(SUM(needs_new_microagent), 0) AS needs_new_microagent,
          COALESCE(SUM(text_repair), 0) AS text_repair,
          COALESCE(SUM(high_uncertainty), 0) AS high_uncertainty,
          COALESCE(SUM(CASE
            WHEN needs_context = 0
             AND needs_domain = 0
             AND needs_new_microagent = 0
             AND text_repair = 0
             AND high_uncertainty = 0
            THEN 1 ELSE 0 END), 0) AS other_watch
        FROM per_segment
        """,
        (ledger_run_id,),
    )
    if not row:
        return empty
    if _int(row.get("instrumented_pending")) == 0:
        return empty
    return {
        "ledger_run_id": ledger_run_id,
        "instrumented_pending": _int(row.get("instrumented_pending")),
        "needs_context": _int(row.get("needs_context")),
        "needs_domain": _int(row.get("needs_domain")),
        "needs_new_microagent": _int(row.get("needs_new_microagent")),
        "text_repair": _int(row.get("text_repair")),
        "high_uncertainty": _int(row.get("high_uncertainty")),
        "other_watch": _int(row.get("other_watch")),
    }


def _pending_next_focus_payload(
    taxonomy: dict[str, Any],
    pending_by_family: list[dict[str, Any]],
    actionability: dict[str, Any],
    needs_apply: int,
) -> dict[str, Any]:
    if needs_apply > 0:
        return {
            "label": "needs_apply",
            "reason": "ha output aguardando aplicacao segura",
            "status": "acao pronta",
            "count": needs_apply,
        }
    actionable = _int(taxonomy.get("actionable_pending"))
    if actionable > 0:
        return {
            "label": "fila acionavel",
            "reason": "existe revisao/aplicacao operacional pronta",
            "status": "acao pronta",
            "count": actionable,
        }
    bridge = _int(taxonomy.get("governed_bridge_pending"))
    if bridge > 0:
        return {
            "label": "ponte governada",
            "reason": "ha lifecycle/bridge pendente de decisao",
            "status": "aguardando politica",
            "count": bridge,
        }
    if pending_by_family:
        top = pending_by_family[0]
        return {
            "label": top.get("label") or "familia pendente",
            "reason": "maior volume pendente mapeado pelo ledger",
            "status": "amadurecer microagente/politica",
            "count": _int(top.get("value")),
        }
    instrumented = actionability.get("instrumented_pending")
    return {
        "label": "pending_instrumentation",
        "reason": "sem familia acionavel suficiente no payload atual",
        "status": "instrumentacao pendente",
        "count": _int(instrumented),
    }


def _build_app_state_payload(db_path: Path) -> dict[str, Any]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        segment_state = _latest_segment_state_summary(con)
        post_release_status = _post_release_feedback_payload(segment_state)
        safety_status = _safe_full_production_payload(post_release_status, segment_state)
        learning = _learning_status_payload(con)
        run = _read_production_run_status()
        production_delta = _last_production_delta_payload(con, run, segment_state)
        segment_trend = _segment_state_trend(con)
        previous_state = segment_trend[-2] if len(segment_trend) > 1 else {}
        taxonomy = _pending_taxonomy_payload(con, segment_state["run_id"])
        diff_review = _release_diff_review_payload(con, segment_state["run_id"])
        evaluation_decision = _read_json_file(EVALUATION_DECISION_LATEST_FILE)
        if evaluation_decision:
            evaluation_decision["stale"] = (
                _int(evaluation_decision.get("segment_state_run_id")) != _int(segment_state.get("run_id"))
            )
        pending_by_family = _pending_by_family_payload(con, segment_state["run_id"])
        pending_actionability = _pending_actionability_payload(con, segment_state["run_id"])
        pending_next_focus = _pending_next_focus_payload(
            taxonomy,
            pending_by_family,
            pending_actionability,
            segment_state["needs_apply"],
        )
        run_active = run.get("status") in {"starting", "running"}
        stages = run.get("stages") if run.get("run_id") else _new_production_run_status("preview").get("stages", [])
        stages = stages if isinstance(stages, list) else []
        done_stages = sum(1 for stage in stages if isinstance(stage, dict) and stage.get("status") == "done")
        running_stage = next((stage for stage in stages if isinstance(stage, dict) and stage.get("status") == "running"), None)
        running_progress = 0.0
        if running_stage:
            running_progress = min(90.0, max(0.0, float(running_stage.get("progress_pct") or 0))) / 100.0
        if stages:
            progress_pct = round(((done_stages + running_progress) / len(stages)) * 100)
            if run.get("status") == "completed":
                progress_pct = 100
        else:
            progress_pct = 100 if run.get("status") == "completed" else 0
        generated_at = datetime.now().isoformat(timespec="seconds")
        disk_preflight = _production_disk_preflight_payload()
        release_status = (
            "needs_apply"
            if segment_state["needs_apply"]
            else "ready_with_known_issues"
            if segment_state["pending_count"]
            else "ready_for_release"
        )
        return {
            "generated_at": generated_at,
            "cache": {
                "generated_at": generated_at,
                "source_db_mtime": _db_mtime(db_path),
                "stale": False,
            },
            "release": {
                "readiness": release_status,
                "total_segments": segment_state["total_segments"],
                "closed_count": segment_state["closed_count"],
                "pending_count": segment_state["pending_count"],
                "closed_rate": segment_state["closed_rate"],
                "needs_apply": segment_state["needs_apply"],
                "output_coverage": _output_coverage(con),
                "latest_segment_state_run_id": segment_state["run_id"],
                "latest_ledger_run_id": _latest_ledger_run_id(con),
                "segment_state_finished_at": segment_state["finished_at"],
                "segment_state_trend": segment_trend,
                "previous_segment_state_delta": {
                    "available": bool(previous_state),
                    "baseline_segment_state_run_id": _int(previous_state.get("run_id")),
                    "closed_delta": segment_state["closed_count"] - _int(previous_state.get("closed_count")),
                    "pending_delta": segment_state["pending_count"] - _int(previous_state.get("pending_count")),
                    "needs_apply_delta": segment_state["needs_apply"] - _int(previous_state.get("needs_apply")),
                } if previous_state else {"available": False, "reason": "pending_instrumentation"},
                "since_last_production": production_delta,
                "qualified_pending": {
                    "actionable_pending": _int(taxonomy.get("actionable_pending")),
                    "context_domain": taxonomy.get("context_domain", "pending_instrumentation"),
                    "policy_waiting": _int(taxonomy.get("governed_bridge_pending")),
                    "high_uncertainty": _int(taxonomy.get("model_suspicion_watch")),
                    "raw_pending": _int(taxonomy.get("raw_pending")),
                },
                "pending_by_family": pending_by_family,
                "pending_actionability": pending_actionability,
                "pending_next_focus": pending_next_focus,
                "post_release": {
                    **post_release_status,
                    **post_release_status.get("summary", {}),
                    **safety_status.get("post_release_summary", {}),
                    "diff_review": diff_review,
                    "evaluation_decision": evaluation_decision,
                    "stable_baseline_path": post_release_status.get("baseline_control", {}).get("stable_baseline_path"),
                    "broken_release_path": post_release_status.get("baseline_control", {}).get("broken_release_path"),
                    "current_candidate_path": post_release_status.get("release_candidate", {}).get("current_candidate_path"),
                    "summary": {
                        **post_release_status.get("summary", {}),
                        **safety_status.get("post_release_summary", {}),
                        "known_open": post_release_status.get("summary", {}).get("known_open_findings"),
                        "closed": post_release_status.get("summary", {}).get("closed_findings"),
                        "parser_dynamic_backlog": post_release_status.get("summary", {}).get("parser_dynamic_backlog"),
                    },
                    "feedback_items": post_release_status.get("items", []),
                    "active_feedback_items": post_release_status.get("active_items", []),
                    "archived_feedback_items": post_release_status.get("archived_items", []),
                },
                "safety": safety_status,
                "evaluation_gate": safety_status.get("evaluation_gate", {}),
                "publication_gate": safety_status.get("publication_gate", {}),
                "baseline_control": {
                    **post_release_status.get("baseline_control", {}),
                    **safety_status.get("baseline_control", {}),
                },
                "visual_locks": {
                    **post_release_status.get("visual_locks", {}),
                    **safety_status.get("visual_locks", {}),
                },
                "release_candidate": {
                    **post_release_status.get("release_candidate", {}),
                    **safety_status.get("release_candidate", {}),
                },
                "source_output_update": {
                    **post_release_status.get("source_output_update", {}),
                    **safety_status.get("source_output_update", {}),
                },
                "disk_preflight": disk_preflight,
                "operational_integrity": {
                    "source_status": "pending_instrumentation",
                    "output_status": "pending_instrumentation",
                    "confirmations_status": "aligned" if segment_state["needs_apply"] == 0 else "attention",
                    "needs_apply_status": "ok" if segment_state["needs_apply"] == 0 else "attention",
                    "learning_gate_status": "released" if bool(learning.get("can_start_production")) else "blocked",
                },
            },
            "learning_gate": {
                "can_start_production": bool(learning.get("can_start_production")),
                "production_safe": bool(learning.get("production_safe")),
                "status": learning.get("status"),
                "reason": learning.get("reason") or learning.get("gate_message"),
                "current_phase_label": learning.get("current_phase_label"),
                "progress_pct": learning.get("progress_pct"),
                "lock": learning.get("lock") or {},
                "next_action": learning.get("next_action"),
            },
            "production": {
                "active": bool(run_active),
                "last_run": run or {},
                "current_stage": run.get("current_stage"),
                "progress_pct": progress_pct,
                "stages_compact": _cache_stage_compact(stages),
                "summary": {},
                "readiness": {},
                "disk_preflight": disk_preflight,
            },
            "navigation": {
                "dashboard_url": "http://127.0.0.1:5173/#Dashboard",
                "network_url": "http://127.0.0.1:5173/#Dashboard/Network",
            },
        }
    finally:
        con.close()


def _refresh_dashboard_cache(db_path: Path) -> dict[str, Any]:
    global DASHBOARD_CACHE
    payload = _build_app_state_payload(db_path)
    cache_payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_db_mtime": _db_mtime(db_path),
        "app_state": payload,
    }
    cache_file = _dashboard_cache_file()
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(cache_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    DASHBOARD_CACHE = cache_payload
    return cache_payload


def _get_dashboard_cache(db_path: Path, *, force: bool = False) -> dict[str, Any]:
    global DASHBOARD_CACHE
    with DASHBOARD_CACHE_LOCK:
        if not force and DASHBOARD_CACHE is not None:
            return DASHBOARD_CACHE
        cache_file = _dashboard_cache_file()
        if not force and cache_file.exists():
            try:
                DASHBOARD_CACHE = json.loads(cache_file.read_text(encoding="utf-8"))
                return DASHBOARD_CACHE
            except json.JSONDecodeError:
                DASHBOARD_CACHE = None
        return _refresh_dashboard_cache(db_path)


def _run_diagnostic_segment_state(db_path: Path) -> dict[str, Any]:
    started_at = _now_iso()
    previous_run_id = 0
    try:
        with sqlite3.connect(db_path) as con:
            con.row_factory = sqlite3.Row
            previous_run_id = _latest_segment_state_summary(con).get("run_id") or 0
    except Exception:
        previous_run_id = 0

    process = subprocess.run(
        [sys.executable, "pipeline/main.py", "segment-state"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=900,
    )

    new_run_id = 0
    try:
        with sqlite3.connect(db_path) as con:
            con.row_factory = sqlite3.Row
            new_run_id = _latest_segment_state_summary(con).get("run_id") or 0
    except Exception:
        new_run_id = 0

    stdout_lines = (process.stdout or "").splitlines()
    stderr_lines = (process.stderr or "").splitlines()
    return {
        "started_at": started_at,
        "finished_at": _now_iso(),
        "exit_code": process.returncode,
        "previous_segment_state_run_id": previous_run_id,
        "new_segment_state_run_id": new_run_id,
        "created_new_segment_state_run": bool(new_run_id and new_run_id != previous_run_id),
        "stdout_tail": stdout_lines[-12:],
        "stderr_tail": stderr_lines[-12:],
    }


def _app_state_payload(db_path: Path, *, force: bool = False) -> dict[str, Any]:
    cache = _get_dashboard_cache(db_path, force=force)
    app_state = cache.get("app_state") or {}
    app_state.setdefault("cache", {})
    app_state["cache"].update(
        {
            "generated_at": cache.get("generated_at"),
            "source_db_mtime": cache.get("source_db_mtime"),
            "stale": _db_mtime(db_path) != cache.get("source_db_mtime"),
        }
    )
    return app_state


def _latest_local_learning_run_id(db_path: Path) -> int:
    try:
        with sqlite3.connect(db_path) as con:
            con.row_factory = sqlite3.Row
            if _table_exists(con, "local_learning_candidates"):
                row = con.execute("SELECT MAX(run_id) AS run_id FROM local_learning_candidates").fetchone()
                if row is not None and row["run_id"] is not None:
                    return int(row["run_id"])
            if _table_exists(con, "local_learning_runs"):
                row = con.execute("SELECT MAX(id) AS run_id FROM local_learning_runs").fetchone()
                if row is not None and row["run_id"] is not None:
                    return int(row["run_id"])
    except Exception:
        pass
    return 0


def _publication_lifecycle_apply_readiness(
    db_path: Path,
    *,
    state_run_id: int | None,
    segment_ids: list[int],
) -> dict[str, Any]:
    if not segment_ids:
        return {
            "available": True,
            "state_run_id": state_run_id,
            "candidates": 0,
            "ready": 0,
            "blocked": 0,
            "result_counts": {},
        }
    pipeline_dir = ROOT / "pipeline"
    if str(pipeline_dir) not in sys.path:
        sys.path.insert(0, str(pipeline_dir))
    try:
        import general_pipe_article_gender_lifecycle_policy_materializer as bridge  # type: ignore
        import human_confirmed_local_learning_lifecycle_policy_materializer as materializer  # type: ignore
    except Exception as exc:
        return {
            "available": False,
            "state_run_id": state_run_id,
            "candidates": len(segment_ids),
            "ready": 0,
            "blocked": len(segment_ids),
            "result_counts": {"materializer_import_failed": len(segment_ids)},
            "error": str(exc),
        }

    previous_rule_version = getattr(bridge, "RULE_VERSION", None)
    previous_target_segment_ids = getattr(bridge, "TARGET_SEGMENT_IDS", ())
    previous_default_state_run_id = getattr(bridge, "DEFAULT_SEGMENT_STATE_RUN_ID", None)
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only = ON")
        try:
            selected_state_run_id = _int(state_run_id) or _int(_latest_segment_state_summary(con).get("run_id"))
            ledger_run_id = _latest_ledger_run_id(con)
            if not selected_state_run_id or not ledger_run_id:
                return {
                    "available": False,
                    "state_run_id": selected_state_run_id,
                    "ledger_run_id": ledger_run_id,
                    "candidates": len(segment_ids),
                    "ready": 0,
                    "blocked": len(segment_ids),
                    "result_counts": {"missing_state_or_ledger": len(segment_ids)},
                }
            materializer.configure_bridge(tuple(segment_ids), selected_state_run_id)
            rows = bridge.collect_rows(con, selected_state_run_id, ledger_run_id)
        finally:
            con.close()
    except Exception as exc:
        return {
            "available": False,
            "state_run_id": state_run_id,
            "candidates": len(segment_ids),
            "ready": 0,
            "blocked": len(segment_ids),
            "result_counts": {"lifecycle_readiness_failed": len(segment_ids)},
            "error": str(exc),
        }
    finally:
        if previous_rule_version is not None:
            bridge.RULE_VERSION = previous_rule_version
        bridge.TARGET_SEGMENT_IDS = previous_target_segment_ids
        if previous_default_state_run_id is not None:
            bridge.DEFAULT_SEGMENT_STATE_RUN_ID = previous_default_state_run_id

    result_counts: dict[str, int] = {}
    ready = 0
    for row in rows:
        if row.get("policy_allowed"):
            ready += 1
            result_counts["ready"] = result_counts.get("ready", 0) + 1
        else:
            reason = str(row.get("block_reason") or "blocked")
            result_counts[reason] = result_counts.get(reason, 0) + 1
    missing = max(0, len(segment_ids) - len(rows))
    if missing:
        result_counts["missing_from_materializer"] = result_counts.get("missing_from_materializer", 0) + missing
    return {
        "available": True,
        "state_run_id": state_run_id,
        "candidates": len(segment_ids),
        "materializer_rows": len(rows),
        "ready": ready,
        "blocked": max(0, len(segment_ids) - ready),
        "result_counts": result_counts,
        "policy_name": getattr(bridge, "POLICY_NAME", "human_confirmed_post_apply_repair_lifecycle_bridge"),
        "policy_action": getattr(bridge, "POLICY_ACTION", "close_reopen_human_confirmed_post_apply_repair_lifecycle"),
    }


def _publication_preflight_payload(db_path: Path, *, force: bool = False) -> dict[str, Any]:
    app_state = _app_state_payload(db_path, force=force)
    release = app_state.get("release") or {}
    post_release = release.get("post_release") or {}
    diff_review = post_release.get("diff_review") or {}
    diff_summary = diff_review.get("summary") or {}
    safety = release.get("safety") or {}
    publication_gate = safety.get("publication_gate") or release.get("publication_gate") or {}
    active_feedback = post_release.get("active_feedback_items") or post_release.get("active_items") or []

    state_run_id = _int(release.get("latest_segment_state_run_id")) or None
    apply_records = diff_review.get("apply_segments") if isinstance(diff_review.get("apply_segments"), list) else []
    apply_count = _int(diff_summary.get("needs_apply"), len(apply_records))
    apply_preview_count = len(apply_records)
    apply_segment_ids = _publication_apply_segment_ids({"app_state": app_state})
    apply_kind_counts: dict[str, int] = {}
    for record in apply_records:
        if not isinstance(record, dict):
            continue
        kind = str(record.get("apply_kind") or "unknown")
        apply_kind_counts[kind] = apply_kind_counts.get(kind, 0) + 1
    lifecycle_apply_count = apply_kind_counts.get("lifecycle_closure", 0)
    output_apply_count = apply_kind_counts.get("output_write", 0)
    mixed_apply_kind = len([count for count in apply_kind_counts.values() if count > 0]) > 1
    token_policy_run_id = _latest_apply_token_policy_run_id(db_path, state_run_id) if output_apply_count > 0 else None
    apply_readiness = (
        _publication_lifecycle_apply_readiness(
            db_path,
            state_run_id=state_run_id,
            segment_ids=apply_segment_ids,
        )
        if lifecycle_apply_count > 0 and output_apply_count == 0
        else _publication_apply_readiness(
            db_path,
            state_run_id=state_run_id,
            segment_ids=apply_segment_ids,
        )
        if output_apply_count > 0 and lifecycle_apply_count == 0
        else {
            "available": not mixed_apply_kind,
            "candidates": apply_count,
            "ready": 0,
            "blocked": apply_count,
            "result_counts": {"mixed_apply_kind_not_supported": apply_count} if mixed_apply_kind else {},
        }
    )
    apply_policy_readiness = (
        _publication_apply_readiness(
            db_path,
            state_run_id=state_run_id,
            segment_ids=apply_segment_ids,
            require_token_policy_decision=True,
            token_policy_run_id=token_policy_run_id,
        )
        if output_apply_count > 0 and lifecycle_apply_count == 0 and token_policy_run_id is not None
        else {
            "available": token_policy_run_id is not None or apply_count == 0 or lifecycle_apply_count > 0,
            "state_run_id": state_run_id,
            "require_token_policy_decision": True,
            "token_policy_run_id": token_policy_run_id,
            "candidates": 0,
            "ready": 0,
            "blocked": output_apply_count,
            "result_counts": {"missing_token_policy_run": output_apply_count} if output_apply_count > 0 else {},
        }
    )
    apply_mixed_readiness = (
        _publication_apply_readiness(
            db_path,
            state_run_id=state_run_id,
            segment_ids=apply_segment_ids,
            allow_token_policy_decision=True,
            token_policy_run_id=token_policy_run_id,
        )
        if output_apply_count > 0 and lifecycle_apply_count == 0 and token_policy_run_id is not None
        else {
            "available": token_policy_run_id is not None or apply_count == 0 or lifecycle_apply_count > 0,
            "state_run_id": state_run_id,
            "allow_token_policy_decision": True,
            "token_policy_run_id": token_policy_run_id,
            "candidates": 0,
            "ready": 0,
            "blocked": output_apply_count,
            "result_counts": {"missing_token_policy_run": output_apply_count} if output_apply_count > 0 else {},
        }
    )
    apply_ready_count = _int(apply_readiness.get("ready"))
    apply_blocked_count = _int(apply_readiness.get("blocked"))
    apply_result_counts = apply_readiness.get("result_counts") if isinstance(apply_readiness.get("result_counts"), dict) else {}
    apply_policy_ready_count = _int(apply_policy_readiness.get("ready"))
    apply_policy_blocked_count = _int(apply_policy_readiness.get("blocked"))
    apply_policy_result_counts = (
        apply_policy_readiness.get("result_counts")
        if isinstance(apply_policy_readiness.get("result_counts"), dict)
        else {}
    )
    apply_mixed_ready_count = _int(apply_mixed_readiness.get("ready"))
    apply_mixed_blocked_count = _int(apply_mixed_readiness.get("blocked"))
    apply_mixed_result_counts = (
        apply_mixed_readiness.get("result_counts")
        if isinstance(apply_mixed_readiness.get("result_counts"), dict)
        else {}
    )
    promotion_count = _int(diff_summary.get("promotions_vs_old"), len(diff_review.get("promotion_segments") or []))
    regression_count = _int(diff_summary.get("score_regressions"))
    unhandled_count = _int(diff_summary.get("unhandled_by_network"), _int(release.get("pending_count")))
    low_score_count = _int(diff_summary.get("low_score"))
    feedback_count = len(active_feedback) if isinstance(active_feedback, list) else _int(post_release.get("active_findings"))
    changed_quality = diff_summary.get("changed_score_comparison") if isinstance(diff_summary.get("changed_score_comparison"), dict) else {}
    package_quality = diff_summary.get("package_score_comparison") if isinstance(diff_summary.get("package_score_comparison"), dict) else {}
    promotion_quality = diff_summary.get("promotion_score_comparison") if isinstance(diff_summary.get("promotion_score_comparison"), dict) else {}
    new_quality = diff_summary.get("new_score_summary") if isinstance(diff_summary.get("new_score_summary"), dict) else {}
    package_old_score = package_quality.get("weighted_avg_old_score") or package_quality.get("avg_old_score")
    package_new_score = (
        package_quality.get("weighted_avg_new_score")
        or package_quality.get("avg_new_score")
        or package_quality.get("package_quality_score")
    )
    package_delta = package_quality.get("weighted_avg_delta") or package_quality.get("avg_delta")

    blocking_reasons: list[str] = []
    warning_reasons: list[str] = []

    if apply_count > 0:
        blocking_reasons.append("apply_queue_pending")
    if mixed_apply_kind:
        blocking_reasons.append("mixed_apply_kind_not_supported")
    for reason in publication_gate.get("blocking_reasons") or []:
        if reason not in blocking_reasons:
            blocking_reasons.append(str(reason))
    if feedback_count > 0:
        blocking_reasons.append("active_feedback")
    if regression_count > 0:
        warning_reasons.append("score_regressions_require_fallback_review")
    if unhandled_count > 0:
        warning_reasons.append("unhandled_segments_require_backlog")
    if low_score_count > 0:
        warning_reasons.append("low_score_segments_require_backlog")

    can_publish_now = not blocking_reasons and publication_gate.get("can_publish_after_full_production_now") is True
    can_apply_normal = (
        apply_count > 0
        and apply_preview_count == apply_count
        and output_apply_count == apply_count
        and apply_ready_count == apply_count
    )
    can_apply_token_policy = (
        apply_count > 0
        and apply_preview_count == apply_count
        and output_apply_count == apply_count
        and apply_policy_ready_count == apply_count
    )
    can_apply_mixed_token_policy = (
        apply_count > 0
        and apply_preview_count == apply_count
        and output_apply_count == apply_count
        and apply_mixed_ready_count == apply_count
    )
    can_apply_lifecycle = (
        apply_count > 0
        and apply_preview_count == apply_count
        and lifecycle_apply_count == apply_count
        and apply_ready_count == apply_count
    )
    can_apply_pending = can_apply_lifecycle or can_apply_normal or can_apply_token_policy or can_apply_mixed_token_policy
    apply_strategy = (
        "lifecycle_closure"
        if can_apply_lifecycle
        else "normal"
        if can_apply_normal
        else "token_policy"
        if can_apply_token_policy
        else "mixed_token_policy"
        if can_apply_mixed_token_policy
        else "blocked"
    )
    status = "ready" if can_publish_now else "blocked"
    lifecycle_apply_blocked = lifecycle_apply_count > 0 and apply_blocked_count > 0
    output_apply_blocked = output_apply_count > 0 and apply_blocked_count > 0
    next_action = (
        "publish_candidate"
        if can_publish_now
        else "apply_confirmed_queue"
        if can_apply_pending
        else "review_lifecycle_apply_queue"
        if lifecycle_apply_blocked
        else "review_token_mismatch_queue"
        if output_apply_blocked
        else "review_apply_queue"
        if apply_count > 0
        else "review_publication_blockers"
    )

    payload = {
        "generated_at": _now_iso(),
        "status": status,
        "can_publish_now": can_publish_now,
        "can_apply_pending": can_apply_pending,
        "apply_strategy": apply_strategy,
        "next_action": next_action,
        "segment_state_run_id": release.get("latest_segment_state_run_id"),
        "token_policy_run_id": token_policy_run_id,
        "score_run_id": diff_summary.get("score_run_id") or diff_review.get("score_run_id"),
        "old_score_run_id": diff_summary.get("old_score_run_id") or diff_review.get("old_score_run_id"),
        "counts": {
            "needs_apply": apply_count,
            "apply_preview": apply_preview_count,
            "apply_lifecycle": lifecycle_apply_count,
            "apply_output_write": output_apply_count,
            "apply_kind_mixed": 1 if mixed_apply_kind else 0,
            "apply_ready": apply_ready_count,
            "apply_blocked": apply_blocked_count,
            "lifecycle_apply_ready": apply_ready_count if lifecycle_apply_count else 0,
            "lifecycle_apply_blocked": apply_blocked_count if lifecycle_apply_count else 0,
            "apply_token_mismatch": _int(apply_result_counts.get("token_mismatch")),
            "apply_missing_token_policy": _int(apply_result_counts.get("missing_token_policy_decision")),
            "apply_policy_ready": apply_policy_ready_count,
            "apply_policy_blocked": apply_policy_blocked_count,
            "apply_policy_token_mismatch": _int(apply_policy_result_counts.get("token_mismatch")),
            "apply_policy_missing_decision": _int(apply_policy_result_counts.get("missing_token_policy_decision")),
            "apply_policy_missing_run": _int(apply_policy_result_counts.get("missing_token_policy_run")),
            "apply_policy_stale_confirmed_hash": _int(apply_policy_result_counts.get("stale_token_policy_confirmed_hash")),
            "apply_policy_stale_output_hash": _int(apply_policy_result_counts.get("stale_token_policy_output_hash")),
            "apply_mixed_ready": apply_mixed_ready_count,
            "apply_mixed_blocked": apply_mixed_blocked_count,
            "apply_mixed_token_mismatch": _int(apply_mixed_result_counts.get("token_mismatch")),
            "apply_mixed_token_policy_ready": _int(apply_mixed_result_counts.get("ready_token_policy_decision")),
            "apply_mixed_missing_decision": _int(apply_mixed_result_counts.get("missing_token_policy_decision")),
            "apply_mixed_stale_confirmed_hash": _int(apply_mixed_result_counts.get("stale_token_policy_confirmed_hash")),
            "apply_mixed_stale_output_hash": _int(apply_mixed_result_counts.get("stale_token_policy_output_hash")),
            "apply_already_matches": _int(apply_result_counts.get("already_matches"))
            + _int(apply_result_counts.get("already_matches_line")),
            "package_diff": _int(diff_summary.get("package_diff_count")),
            "raw_output_diff": _int(diff_summary.get("raw_output_diff_count"), _int(diff_summary.get("changed_vs_old"))),
            "package_excluded": _int(diff_summary.get("package_excluded_count")),
            "promotions": promotion_count,
            "score_regressions": regression_count,
            "unhandled_by_network": unhandled_count,
            "low_score": low_score_count,
            "active_feedback": feedback_count,
        },
        "apply_readiness": apply_readiness,
        "apply_policy_readiness": apply_policy_readiness,
        "apply_mixed_readiness": apply_mixed_readiness,
        "quality": {
            "package_old_score": package_old_score,
            "package_new_score": package_new_score,
            "package_delta": package_delta,
            "package": package_quality,
            "changed": changed_quality,
            "promotions": promotion_quality,
            "new_segments": new_quality,
        },
        "blocking_reasons": blocking_reasons,
        "warning_reasons": warning_reasons,
        "publication_gate": publication_gate,
        "summary": {
            "apply_queue": (
                "ready_for_protected_apply"
                if can_apply_pending
                else "lifecycle_review_required"
                if lifecycle_apply_blocked
                else "token_policy_or_review_required"
                if output_apply_blocked
                else "needs_review"
                if apply_count > 0
                else "empty"
            ),
            "promotions": "available_for_review" if promotion_count > 0 else "empty",
            "regressions": "fallback_or_review_required" if regression_count > 0 else "clear",
            "publication": "ready" if can_publish_now else "blocked",
        },
    }
    return {"ok": True, "publication_preflight": payload, "app_state": app_state}


def _publication_apply_segment_ids(preflight_payload: dict[str, Any]) -> list[int]:
    app_state = preflight_payload.get("app_state") if isinstance(preflight_payload, dict) else {}
    release = app_state.get("release") if isinstance(app_state, dict) else {}
    post_release = release.get("post_release") if isinstance(release, dict) else {}
    diff_review = post_release.get("diff_review") if isinstance(post_release, dict) else {}
    segment_ids: list[int] = []
    for item in diff_review.get("apply_segments") or []:
        if not isinstance(item, dict):
            continue
        segment_id = _int(item.get("segment_id"))
        if segment_id:
            segment_ids.append(segment_id)
    return list(dict.fromkeys(segment_ids))


def _latest_apply_token_policy_run_id(db_path: Path, state_run_id: int | None) -> int | None:
    try:
        with sqlite3.connect(db_path) as con:
            con.row_factory = sqlite3.Row
            if not _table_exists(con, "segment_token_policy_runs"):
                return None
            params: tuple[Any, ...] = ()
            state_sql = ""
            if state_run_id:
                state_sql = "AND state_run_id = ?"
                params = (state_run_id,)
            row = con.execute(
                f"""
                SELECT id
                FROM segment_token_policy_runs
                WHERE finished_at IS NOT NULL
                  AND total_candidates > 0
                  {state_sql}
                ORDER BY finished_at DESC, id DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
            if row is not None:
                return int(row["id"])
            if state_run_id:
                row = con.execute(
                    """
                    SELECT id
                    FROM segment_token_policy_runs
                    WHERE finished_at IS NOT NULL
                      AND total_candidates > 0
                    ORDER BY finished_at DESC, id DESC
                    LIMIT 1
                    """
                ).fetchone()
                if row is not None:
                    return int(row["id"])
    except Exception:
        return None
    return None


def _publication_apply_readiness(
    db_path: Path,
    *,
    state_run_id: int | None,
    segment_ids: list[int],
    require_token_policy_decision: bool = False,
    allow_token_policy_decision: bool = False,
    token_policy_run_id: int | None = None,
) -> dict[str, Any]:
    if not segment_ids:
        return {
            "available": True,
            "candidates": 0,
            "ready": 0,
            "blocked": 0,
            "result_counts": {},
        }
    pipeline_dir = str(ROOT / "pipeline")
    if pipeline_dir not in sys.path:
        sys.path.insert(0, pipeline_dir)
    try:
        import apply_segment_state_updates as segment_apply
    except Exception as exc:  # pragma: no cover - import problem should surface in preflight
        return {
            "available": False,
            "error": str(exc),
            "candidates": 0,
            "ready": 0,
            "blocked": len(segment_ids),
            "result_counts": {"readiness_import_error": len(segment_ids)},
        }

    settings = segment_apply.db.load_settings()
    output_root = segment_apply.db.project_path(settings["output_spanish"])
    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        selected_run_id = state_run_id or segment_apply.latest_state_run_id(con)
        candidates = segment_apply.fetch_candidates(
            con,
            state_run_id=selected_run_id,
            review_states=segment_apply.parse_review_states("human_locked,human_confirmed", True),
            limit=None,
            path_like=None,
            segment_ids=set(segment_ids),
            include_intentional_blank=True,
            require_token_policy_decision=require_token_policy_decision,
            allow_token_policy_decision=allow_token_policy_decision,
            token_policy_run_id=token_policy_run_id,
        )
    result_counts, _previews = segment_apply.evaluate_best_candidates(
        candidates,
        output_root=output_root,
        allow_locked_token_override=False,
        require_token_policy_decision=require_token_policy_decision,
        allow_token_policy_decision=allow_token_policy_decision,
        include_intentional_blank=True,
    )
    if require_token_policy_decision:
        missing_decision_candidates = max(0, len(segment_ids) - len(candidates))
        if missing_decision_candidates:
            result_counts["missing_token_policy_decision"] += missing_decision_candidates
    ready = (
        int(result_counts.get("ready", 0))
        + int(result_counts.get("ready_token_override", 0))
        + int(result_counts.get("ready_token_policy_decision", 0))
    )
    return {
        "available": True,
        "state_run_id": selected_run_id,
        "require_token_policy_decision": require_token_policy_decision,
        "allow_token_policy_decision": allow_token_policy_decision,
        "token_policy_run_id": token_policy_run_id,
        "candidates": len(candidates),
        "ready": ready,
        "blocked": max(0, len(segment_ids) - ready),
        "result_counts": dict(result_counts),
    }


def _write_production_run_status(status: dict[str, Any]) -> None:
    status["updated_at"] = _now_iso()
    PRODUCTION_RUN_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PRODUCTION_RUN_STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _production_run_active() -> bool:
    status = _read_production_run_status()
    return status.get("status") in {"starting", "running"}


def _new_production_run_status(run_id: str, *, evaluation_only: bool = True) -> dict[str, Any]:
    stages = [
        {"id": "snapshot", "label": "Snapshot pre-run", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "snapshot_archive", "label": "Arquivar snapshot", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "preflight_sync", "label": "Sincronizar indice", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "segment_state_before", "label": "Segment-state inicial", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "apply_general_dry_run", "label": "Dry-run geral", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "apply_token_policy_dry_run", "label": "Dry-run politica de tokens", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "controlled_token_subpolicy_dry_run", "label": "Dry-run subpolitica controlada", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "select_cstring_bridge_dry_run", "label": "Dry-run Select_CString", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "same_token_boundary_repair_audit", "label": "Auditoria same-token", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "same_token_boundary_repair_dry_run", "label": "Dry-run reparo same-token", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "title_landed_es_repair_dry_run", "label": "Dry-run titulos -es", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
    ]
    if not evaluation_only:
        stages.extend([
        {"id": "apply_general_write", "label": "Escrita regular", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "apply_token_policy_write", "label": "Escrita por politica de token", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "controlled_token_subpolicy_write", "label": "Escrita subpolitica controlada", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "select_cstring_bridge_write", "label": "Escrita Select_CString", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "same_token_boundary_repair_write", "label": "Escrita/fechamento same-token", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "title_landed_es_repair_write", "label": "Escrita titulos -es", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "apply_locked_override_write", "label": "Escrita overrides manuais", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "segment_state_after", "label": "Segment-state pos-escrita", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "token_policy_after", "label": "Politica de tokens pos-escrita", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "controlled_token_subpolicy_reaudit", "label": "Reauditoria subpolitica controlada", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "select_cstring_bridge_reaudit", "label": "Reauditoria Select_CString", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "same_token_boundary_repair_reaudit", "label": "Reauditoria same-token", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "title_landed_es_repair_reaudit", "label": "Reauditoria titulos -es", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        ])
    stages.append({"id": "composite_review_progress", "label": "Progresso composto", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None})
    if evaluation_only:
        stages.append({"id": "package_score_recalibration", "label": "Recalibrar score do pacote", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None})
    stages.append({"id": "production_report", "label": "Relatorio final", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None})
    snapshot_path = PRODUCTION_SNAPSHOT_ROOT / run_id
    return {
        "run_id": run_id,
        "status": "starting",
        "mode": "evaluation_full_production" if evaluation_only else "full_production_apply",
        "run_mode": "evaluation" if evaluation_only else "full_production",
        "apply_output": not evaluation_only,
        "started_at": _now_iso(),
        "updated_at": _now_iso(),
        "finished_at": "",
        "last_event_at": "",
        "current_stage": "",
        "stages": stages,
        "log_path": str(ROOT / "logs" / f"{run_id}_production_run.log"),
        "report_path": str(ROOT / "reports" / f"{run_id}_production_run.txt"),
        "snapshot_path": str(snapshot_path),
        "snapshot_manifest_path": str(snapshot_path / "manifest.json"),
        "snapshot_archive_path": str(PRODUCTION_SNAPSHOT_ARCHIVE_ROOT / f"{run_id}.zip"),
        "report_paths": [],
        "logs_tail": [],
        "message": "Evaluation run queued." if evaluation_only else "Full production run queued.",
    }


def _new_publication_apply_status(run_id: str) -> dict[str, Any]:
    stages = [
        {"id": "publication_preflight", "label": "Preflight publicavel", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "apply_confirmed_dry_run", "label": "Dry-run apply confirmado", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "apply_confirmed_write", "label": "Aplicar fila confirmada", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "segment_state_after", "label": "Segment-state pos-apply", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "publication_preflight_after", "label": "Preflight pos-apply", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
        {"id": "production_report", "label": "Relatorio final", "status": "pending", "started_at": "", "finished_at": "", "exit_code": None},
    ]
    return {
        "run_id": run_id,
        "status": "starting",
        "mode": "publication_apply_confirmed",
        "run_mode": "publication",
        "apply_output": True,
        "started_at": _now_iso(),
        "updated_at": _now_iso(),
        "finished_at": "",
        "last_event_at": "",
        "current_stage": "",
        "stages": stages,
        "log_path": str(ROOT / "logs" / f"{run_id}_publication_apply.log"),
        "report_path": str(ROOT / "reports" / f"{run_id}_publication_apply_run.txt"),
        "snapshot_path": "",
        "snapshot_manifest_path": "",
        "snapshot_archive_path": "",
        "report_paths": [],
        "logs_tail": [],
        "message": "Publication apply run queued.",
        "publication_apply_segment_ids": [],
    }


def _update_stage(run_status: dict[str, Any], stage_id: str, **updates: Any) -> None:
    for stage in run_status.get("stages", []):
        if stage.get("id") == stage_id:
            stage.update(updates)
            break


def _stage_label(run_status: dict[str, Any], stage_id: str | None) -> str:
    if not stage_id:
        return ""
    for stage in run_status.get("stages", []):
        if isinstance(stage, dict) and stage.get("id") == stage_id:
            return str(stage.get("label") or stage_id)
    return str(stage_id)


def _append_run_log(status: dict[str, Any], line: str) -> None:
    logs_tail = list(status.get("logs_tail") or [])
    logs_tail.append(line.rstrip())
    status["logs_tail"] = logs_tail[-120:]
    status["last_event_at"] = _now_iso()


def _start_inline_stage(status: dict[str, Any], stage_id: str, message: str, log_handle) -> None:
    now = _now_iso()
    _update_stage(status, stage_id, status="running", started_at=now, updated_at=now, progress_pct=15)
    status["status"] = "running"
    status["current_stage"] = stage_id
    status["current_label"] = _stage_label(status, stage_id)
    status["message"] = message
    log_handle.write(f"\n=== {stage_id} ===\n")
    log_handle.write(f"{message}\n")
    log_handle.flush()
    _append_run_log(status, f"[{stage_id}] {message}")
    _write_production_run_status(status)


def _finish_inline_stage(status: dict[str, Any], stage_id: str, *, metrics: dict[str, Any] | None = None, message: str = "") -> None:
    now = _now_iso()
    updates: dict[str, Any] = {
        "status": "done",
        "updated_at": now,
        "finished_at": now,
        "exit_code": 0,
        "progress_pct": 100,
    }
    if metrics:
        updates["metrics"] = metrics
    _update_stage(status, stage_id, **updates)
    if message:
        status["message"] = message
        _append_run_log(status, f"[{stage_id}] {message}")
    _write_production_run_status(status)


def _update_stage_metric(run_status: dict[str, Any], stage_id: str, key: str, value: Any) -> None:
    for stage in run_status.get("stages", []):
        if stage.get("id") == stage_id:
            metrics = dict(stage.get("metrics") or {})
            metrics[key] = value
            stage["metrics"] = metrics
            break


def _parse_count_suffix(line: str) -> int | None:
    try:
        return int(line.rsplit(":", 1)[1].strip().replace(",", ""))
    except Exception:
        return None


def _path_size(path: Path, ignore_func=None) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            children = list(current.iterdir())
        except OSError:
            continue
        ignored = ignore_func(str(current), [child.name for child in children]) if ignore_func else set()
        for child in children:
            if child.name in ignored:
                continue
            if child.is_dir():
                stack.append(child)
            elif child.is_file():
                total += child.stat().st_size
    return total


def _snapshot_ignore(dir_path: str, names: list[str]) -> set[str]:
    ignored = {
        "__pycache__",
        ".pytest_cache",
        "node_modules",
        "dist",
        ".git",
    }
    path = Path(dir_path)
    if path.name == "memory":
        ignored.update(
            {
                "backups",
                "bkp banco",
                "production_snapshots",
                "production_snapshot_archives",
                "translation_engine.sqlite",
                "translation_engine.sqlite-shm",
                "translation_engine.sqlite-wal",
            }
        )
        ignored.update(name for name in names if name.startswith("translation_engine_before_"))
    return {name for name in names if name in ignored}


def _production_snapshot_sources() -> dict[str, Path]:
    sources = {
        "source": ROOT / "source",
        "output": ROOT / "output",
        "reports": ROOT / "reports",
        "logs": ROOT / "logs",
        "memory_light": ROOT / "memory",
    }
    optional_models = ROOT / "models"
    if optional_models.exists():
        sources["models"] = optional_models
    return sources


def _production_disk_preflight_payload() -> dict[str, Any]:
    sources = _production_snapshot_sources()
    sqlite_db_size = DEFAULT_DB.stat().st_size if DEFAULT_DB.exists() else 0
    estimated_size = DEFAULT_DB.stat().st_size if PRODUCTION_FULL_SQLITE_BACKUP_ENABLED and DEFAULT_DB.exists() else 0
    estimated_size += sum(_path_size(path, _snapshot_ignore) for path in sources.values())
    required_space = int(estimated_size * 1.08)
    usage_path = PRODUCTION_SNAPSHOT_ROOT if PRODUCTION_SNAPSHOT_ROOT.exists() else ROOT
    usage = shutil.disk_usage(usage_path)
    missing = max(0, required_space - usage.free)
    ok = usage.free >= required_space
    return {
        "checked_at": _now_iso(),
        "status": "ok" if ok else "insufficient_disk_space",
        "ok": ok,
        "path": str(usage_path),
        "estimated_snapshot_bytes": estimated_size,
        "required_free_bytes": required_space,
        "free_bytes": usage.free,
        "missing_bytes": missing,
        "margin_ratio": 1.08,
        "sqlite_full_backup_enabled": PRODUCTION_FULL_SQLITE_BACKUP_ENABLED,
        "sqlite_backup_mode": "full_backup" if PRODUCTION_FULL_SQLITE_BACKUP_ENABLED else "metadata_only",
        "sqlite_db_size_bytes": sqlite_db_size,
        "message": (
            "Disk preflight ok."
            if ok
            else f"Insufficient disk space for production snapshot. Required {required_space} bytes, free {usage.free} bytes."
        ),
        "suggestion": "" if ok else "Limpar ou mover snapshots antigos antes de iniciar a producao.",
    }


def _copy_tree_snapshot(source: Path, target: Path) -> None:
    if source.exists():
        shutil.copytree(source, target, ignore=_snapshot_ignore, dirs_exist_ok=True)


def _sqlite_backup_snapshot(target_db: Path) -> None:
    target_db.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(DEFAULT_DB)
    try:
        target = sqlite3.connect(target_db)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _sqlite_snapshot_metadata() -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "path": str(DEFAULT_DB),
        "exists": DEFAULT_DB.exists(),
        "full_backup_enabled": PRODUCTION_FULL_SQLITE_BACKUP_ENABLED,
        "full_backup_status": "enabled" if PRODUCTION_FULL_SQLITE_BACKUP_ENABLED else "skipped_metadata_only",
    }
    if not DEFAULT_DB.exists():
        return metadata

    stat = DEFAULT_DB.stat()
    metadata.update(
        {
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        }
    )
    try:
        con = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True)
        try:
            page_size = con.execute("PRAGMA page_size").fetchone()[0]
            page_count = con.execute("PRAGMA page_count").fetchone()[0]
            freelist_count = con.execute("PRAGMA freelist_count").fetchone()[0]
            table_count = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
            metadata.update(
                {
                    "page_size": page_size,
                    "page_count": page_count,
                    "freelist_count": freelist_count,
                    "table_count": table_count,
                }
            )
        finally:
            con.close()
    except Exception as exc:
        metadata["metadata_error"] = str(exc)
    return metadata


def _create_production_snapshot(status: dict[str, Any], log_handle) -> None:
    stage_id = "snapshot"
    now = _now_iso()
    _update_stage(status, stage_id, status="running", started_at=now, updated_at=now, progress_pct=0)
    status["status"] = "running"
    status["current_stage"] = stage_id
    status["current_label"] = _stage_label(status, stage_id)
    status["message"] = "Creating pre-run snapshot"
    _write_production_run_status(status)

    snapshot_root = Path(status["snapshot_path"])
    if snapshot_root.exists():
        raise RuntimeError(f"Snapshot path already exists: {snapshot_root}")
    snapshot_root.mkdir(parents=True, exist_ok=False)

    sources = _production_snapshot_sources()
    disk_preflight = _production_disk_preflight_payload()
    estimated_size = int(disk_preflight.get("estimated_snapshot_bytes") or 0)
    free_space = int(disk_preflight.get("free_bytes") or 0)
    required_space = int(disk_preflight.get("required_free_bytes") or 0)
    manifest: dict[str, Any] = {
        "run_id": status.get("run_id"),
        "created_at": _now_iso(),
        "snapshot_root": str(snapshot_root),
        "sqlite_backup": (
            str(snapshot_root / "memory" / "translation_engine.sqlite")
            if PRODUCTION_FULL_SQLITE_BACKUP_ENABLED
            else None
        ),
        "sqlite_metadata": _sqlite_snapshot_metadata(),
        "estimated_bytes": estimated_size,
        "required_bytes_with_margin": required_space,
        "free_bytes_before": free_space,
        "disk_preflight": disk_preflight,
        "copied": [],
        "skipped": [
            "memory/backups",
            "memory/bkp banco",
            "memory/production_snapshots",
            "memory/production_snapshot_archives",
            "memory/translation_engine.sqlite",
            "memory/translation_engine.sqlite-wal",
            "memory/translation_engine.sqlite-shm",
            "memory/translation_engine_before_*",
        ],
    }
    if free_space < required_space:
        manifest_path = snapshot_root / "manifest.json"
        manifest["error"] = "insufficient_free_space"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        status["disk_preflight"] = disk_preflight
        status["failed_stage"] = stage_id
        status["failure"] = {
            "reason": "insufficient_disk_space",
            "stage": stage_id,
            "message": disk_preflight["message"],
            "disk_preflight": disk_preflight,
        }
        _write_production_run_status(status)
        raise RuntimeError(
            f"Insufficient disk space for production snapshot. Required {required_space} bytes, free {free_space} bytes."
        )

    log_handle.write("\n=== snapshot ===\n")
    log_handle.write(f"Snapshot root: {snapshot_root}\n")
    log_handle.write(f"Estimated bytes: {estimated_size}\n")
    log_handle.flush()
    _append_run_log(status, f"[snapshot] root: {snapshot_root}")
    _append_run_log(status, f"[snapshot] estimated bytes: {estimated_size}")

    if PRODUCTION_FULL_SQLITE_BACKUP_ENABLED:
        _sqlite_backup_snapshot(snapshot_root / "memory" / "translation_engine.sqlite")
        manifest["copied"].append("memory/translation_engine.sqlite")
        _append_run_log(status, "[snapshot] SQLite backup completed")
        _write_production_run_status(status)
    else:
        _append_run_log(status, "[snapshot] SQLite full backup skipped; metadata recorded")
        log_handle.write("SQLite full backup: skipped; metadata recorded in manifest.\n")
        log_handle.flush()
        _write_production_run_status(status)

    for name, source_path in sources.items():
        target_name = "memory" if name == "memory_light" else name
        target_path = snapshot_root / target_name
        _copy_tree_snapshot(source_path, target_path)
        manifest["copied"].append(str(source_path.relative_to(ROOT)))
        _append_run_log(status, f"[snapshot] copied {source_path.relative_to(ROOT)}")
        _write_production_run_status(status)

    manifest_path = Path(status["snapshot_manifest_path"])
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _update_stage(status, stage_id, status="done", updated_at=_now_iso(), finished_at=_now_iso(), exit_code=0, progress_pct=100)
    status["message"] = "Pre-run snapshot completed"
    _write_production_run_status(status)


def _archive_production_snapshot(status: dict[str, Any], log_handle) -> int:
    run_id = str(status.get("run_id") or "")
    archive_path = PRODUCTION_SNAPSHOT_ARCHIVE_ROOT / f"{run_id}.zip"
    status["snapshot_archive_path"] = str(archive_path)
    return _run_production_command(
        status,
        stage_id="snapshot_archive",
        command=[
            sys.executable,
            "pipeline/snapshot_archive.py",
            "archive",
            run_id,
            "--delete-original",
        ],
        log_handle=log_handle,
    )


def _write_production_report(status: dict[str, Any]) -> None:
    report_path = Path(status["report_path"])
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "CK3 PT-BR production run report",
        f"Run id: {status.get('run_id')}",
        f"Mode: {status.get('mode')}",
        f"Apply output: {status.get('apply_output')}",
        f"Status: {status.get('status')}",
        f"Started at: {status.get('started_at')}",
        f"Finished at: {status.get('finished_at')}",
        f"Snapshot: {status.get('snapshot_path')}",
        f"Snapshot manifest: {status.get('snapshot_manifest_path')}",
        f"Snapshot archive: {status.get('snapshot_archive_path')}",
        "",
        "Stages:",
    ]
    for stage in status.get("stages", []):
        lines.append(
            f"- {stage.get('id')}: {stage.get('status')} exit={stage.get('exit_code')} "
            f"started={stage.get('started_at')} finished={stage.get('finished_at')}"
        )
    if status.get("mode") == "publication_apply_confirmed":
        preflight = status.get("publication_preflight") if isinstance(status.get("publication_preflight"), dict) else {}
        counts = preflight.get("counts") if isinstance(preflight.get("counts"), dict) else {}
        quality = preflight.get("quality") if isinstance(preflight.get("quality"), dict) else {}
        apply_ids = status.get("publication_apply_segment_ids") if isinstance(status.get("publication_apply_segment_ids"), list) else []
        lines.extend(
            [
                "",
                "Publication summary:",
                f"- Apply ids: {len(apply_ids)}",
                f"- Needs apply after run: {counts.get('needs_apply', 'not_measured')}",
                f"- Apply ready: {counts.get('apply_ready', 'not_measured')}",
                f"- Apply blocked: {counts.get('apply_blocked', 'not_measured')}",
                f"- Apply token mismatch: {counts.get('apply_token_mismatch', 'not_measured')}",
                f"- Promotions measured: {counts.get('promotions', 'not_measured')}",
                f"- Score regressions: {counts.get('score_regressions', 'not_measured')}",
                f"- Unhandled by network: {counts.get('unhandled_by_network', 'not_measured')}",
                f"- Package score old: {quality.get('package_old_score', 'not_measured')}",
                f"- Package score new: {quality.get('package_new_score', 'not_measured')}",
                f"- Package delta: {quality.get('package_delta', 'not_measured')}",
                f"- Next action: {preflight.get('next_action', 'not_measured')}",
            ]
        )
    lines.extend(
        [
            "",
            "Pipeline reports:",
            *[f"- {path}" for path in status.get("report_paths", [])],
            "",
            "Interpretation:",
            (
                "- This evaluation executor creates, verifies and archives a snapshot, then measures candidates without writing output."
                if not status.get("apply_output")
                else "- This production executor creates, verifies and archives a snapshot before writing output."
            ),
            (
                "- Publication/output writes are reserved for the confirmed apply queue."
                if not status.get("apply_output")
                else "- Output writes are restricted to confirmed rows that pass the segment-token policy gate."
            ),
            "- Remaining pending, low-confidence and token-policy items are reported for the learning front.",
            "",
            "Recent log tail:",
            *[f"- {line}" for line in (status.get("logs_tail") or [])[-40:]],
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _attach_evaluation_decision_report(status: dict[str, Any], *, raise_on_error: bool = False) -> dict[str, Any] | None:
    state_run_id = _int(status.get("new_segment_state_run_id") or status.get("segment_state_run_id"))
    try:
        con = sqlite3.connect(ACTIVE_DB_PATH)
        con.row_factory = sqlite3.Row
        try:
            if not state_run_id:
                state_run_id = _int(_latest_segment_state_summary(con).get("run_id"))
            diff_review = _release_diff_review_payload(con, state_run_id)
        finally:
            con.close()
        paths = _write_evaluation_decision_artifacts(
            diff_review,
            run_id=str(status.get("run_id") or ""),
            source="evaluation_full_production",
        )
        status["evaluation_decision_report"] = {
            "generated_at": _now_iso(),
            "segment_state_run_id": state_run_id,
            "paths": paths,
        }
        existing_paths = status.get("report_paths") if isinstance(status.get("report_paths"), list) else []
        for path in paths.values():
            if path not in existing_paths:
                existing_paths.append(path)
        status["report_paths"] = existing_paths
        _append_run_log(status, f"[evaluation_decision] report: {paths.get('markdown')}")
        return diff_review
    except Exception as exc:  # pragma: no cover - report metadata should not fail the run
        status["evaluation_decision_report"] = {
            "generated_at": _now_iso(),
            "segment_state_run_id": state_run_id,
            "error": str(exc),
        }
        _append_run_log(status, f"[evaluation_decision] report failed: {exc}")
        if raise_on_error:
            raise
        return None


def _run_package_score_recalibration_stage(status: dict[str, Any], log_handle) -> None:
    stage_id = "package_score_recalibration"
    _start_inline_stage(
        status,
        stage_id,
        "Recalculating package score, promotion gates and old-vs-output queues with current calibration.",
        log_handle,
    )
    diff_review = _attach_evaluation_decision_report(status, raise_on_error=True) or {}
    summary = diff_review.get("summary") if isinstance(diff_review.get("summary"), dict) else {}
    package_score = summary.get("package_score_comparison") if isinstance(summary.get("package_score_comparison"), dict) else {}
    metrics = {
        "score_run_id": summary.get("score_run_id") or diff_review.get("score_run_id"),
        "old_score_run_id": summary.get("old_score_run_id") or diff_review.get("old_score_run_id"),
        "package_diff": _int(summary.get("package_diff_count")),
        "promotions": _int(summary.get("promotions_vs_old")),
        "needs_apply": _int(summary.get("needs_apply")),
        "score_regressions": _int(summary.get("score_regressions")),
        "low_score": _int(summary.get("low_score")),
        "unhandled_by_network": _int(summary.get("unhandled_by_network")),
        "package_old_score": package_score.get("weighted_avg_old_score") or package_score.get("avg_old_score"),
        "package_new_score": package_score.get("weighted_avg_new_score") or package_score.get("avg_new_score"),
        "package_delta": package_score.get("weighted_avg_delta") or package_score.get("avg_delta"),
    }
    status["package_score_recalibration"] = {
        "generated_at": _now_iso(),
        "method": "release_diff_review_payload_current_calibration",
        "metrics": metrics,
    }
    log_handle.write(
        "[package_score_recalibration] "
        f"package_diff={metrics['package_diff']} promotions={metrics['promotions']} "
        f"needs_apply={metrics['needs_apply']} regressions={metrics['score_regressions']} "
        f"low_score={metrics['low_score']} unhandled={metrics['unhandled_by_network']}\n"
    )
    log_handle.flush()
    _finish_inline_stage(
        status,
        stage_id,
        metrics=metrics,
        message="Package score and evaluation queues recalibrated with current rules.",
    )


def _run_production_command(status: dict[str, Any], *, stage_id: str, command: list[str], log_handle) -> int:
    stage_started_at = _now_iso()
    _update_stage(status, stage_id, status="running", started_at=stage_started_at, updated_at=stage_started_at, progress_pct=0)
    status["status"] = "running"
    status["current_stage"] = stage_id
    status["current_label"] = _stage_label(status, stage_id)
    status["message"] = f"Running {status['current_label'] or stage_id}"
    _write_production_run_status(status)
    log_handle.write(f"\n=== {stage_id} ===\n")
    log_handle.write(f"Command: {' '.join(command)}\n")
    log_handle.flush()

    metric_rules = [
        ("[apply_segment_state_updates]", {
            "Candidates inspected": "candidates",
            "Ready to apply": "ready",
            "Applied updates": "applied",
            "Files with ready updates": "files_with_ready_updates",
            "token_mismatch": "token_mismatch",
            "stale_token_policy_confirmed_hash": "stale_token_policy_confirmed_hash",
        }),
        ("[controlled_token_subpolicy_apply]", {
            "Candidates": "candidates",
            "Eligible": "eligible",
            "Ready": "ready",
            "Applied": "applied",
            "Stale": "stale",
            "Blocked": "blocked",
            "Confirmation promoted": "confirmation_promoted",
            "Output written": "output_written",
            "Estimated closed gain": "estimated_closed_gain",
            "Actual closed gain after reaudit": "actual_closed_gain_after_reaudit",
        }),
        ("[auto_confirmation_reopen_text_boundary_repair_production_audit]", {
            "Audit run id": "audit_run_id",
            "Lifecycle run id": "lifecycle_run_id",
            "State run id": "segment_state_run_id",
            "Eligible controlled production": "eligible",
            "Estimated closed gain": "estimated_closed_gain",
            "Real repairs": "real_repairs",
            "No-op observations": "noop_observations",
            "Blocked": "blocked",
        }),
        ("[same_token_boundary_repair_apply]", {
            "Audit run id": "audit_run_id",
            "Lifecycle run id": "lifecycle_run_id",
            "Segment-state run id": "segment_state_run_id",
            "Candidates": "candidates",
            "Eligible": "eligible",
            "Ready": "ready",
            "Applied": "applied",
            "Noop closed": "noop_closed",
            "Skipped": "skipped",
            "Stale": "stale",
            "Blocked": "blocked",
            "Confirmation promoted": "confirmation_promoted",
            "Output written": "output_written",
            "Estimated closed gain": "estimated_closed_gain",
            "Actual closed gain after segment-state": "actual_closed_gain_after_segment_state",
        }),
        ("[select_cstring_governed_bridge_apply]", {
            "Proposal run id": "proposal_run_id",
            "Maturity audit run id": "maturity_audit_run_id",
            "Final overlay run id": "final_overlay_run_id",
            "Bridge candidate": "bridge_candidate",
            "Production release allowed": "production_release_allowed",
            "Candidates": "candidates",
            "Ready": "ready",
            "Applied": "applied",
            "Already applied": "already_applied",
            "Stale": "stale",
            "Blocked": "blocked",
            "Confirmation promoted": "confirmation_promoted",
            "Output written": "output_written",
            "Same token": "same_token",
            "Dynamic payload delta": "dynamic_payload_delta",
            "Estimated closed gain": "estimated_closed_gain",
            "Actual closed gain after reaudit": "actual_closed_gain_after_reaudit",
        }),
        ("[title_landed_es_reviewed_repair_dry_run]", {
            "Queue run id": "queue_run_id",
            "State run id": "segment_state_run_id",
            "Candidates inspected": "candidates",
            "ready_confirmation_and_output": "ready",
            "already_aligned": "already_aligned",
            "blocked_state": "blocked_state",
            "blocked_quality": "blocked_quality",
            "blocked_token_or_structure": "blocked_token_or_structure",
            "blocked_stale_decision": "blocked_stale_decision",
        }),
        ("[title_landed_es_reviewed_repair_apply]", {
            "Segment-state run id": "segment_state_run_id",
            "Candidates": "candidates",
            "Dry-run ready": "dry_run_ready",
            "Ready": "ready",
            "Applied": "applied",
            "Already applied": "already_applied",
            "Stale": "stale",
            "Blocked": "blocked",
            "Skipped": "skipped",
            "Confirmation promoted": "confirmation_promoted",
            "Output written": "output_written",
            "Files touched": "files_touched",
        }),
    ]

    def parse_line(line: str) -> None:
        if "Report:" in line:
            report_path = line.split("Report:", 1)[1].strip()
            if report_path and report_path not in status["report_paths"]:
                status["report_paths"].append(report_path)
        key_value_metric_map = {
            "candidate_count": "candidates",
            "released_count": "released",
            "blocked_count": "blocked",
            "policy_run_id": "policy_run_id",
            "learning_run_id": "learning_run_id",
        }
        if "=" in line:
            key, value = line.split("=", 1)
            metric_key = key_value_metric_map.get(key.strip())
            if metric_key:
                try:
                    _update_stage_metric(status, stage_id, metric_key, int(value.strip()))
                except ValueError:
                    pass
        for marker, metric_map in metric_rules:
            if marker not in line:
                continue
            for label, key in metric_map.items():
                if label in line:
                    parsed = _parse_count_suffix(line)
                    if parsed is not None:
                        _update_stage_metric(status, stage_id, key, parsed)

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        command,
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    line_count = 0
    stage_started_monotonic = time.monotonic()
    last_status_write = time.monotonic()
    heartbeat_stop = threading.Event()

    def heartbeat_status() -> None:
        while not heartbeat_stop.wait(5.0):
            if process.poll() is not None:
                break
            now = _now_iso()
            elapsed = time.monotonic() - stage_started_monotonic
            progress_pct = min(90, max(8, int(8 + (elapsed // 30) * 5)))
            status["updated_at"] = now
            status["message"] = f"Running {stage_id}"
            _update_stage(
                status,
                stage_id,
                updated_at=now,
                heartbeat_at=now,
                log_line_count=line_count,
                progress_pct=progress_pct,
            )
            _write_production_run_status(status)

    heartbeat_thread = threading.Thread(target=heartbeat_status, daemon=True)
    heartbeat_thread.start()
    try:
        for raw_line in process.stdout:
            line_count += 1
            line = raw_line.rstrip("\n")
            log_handle.write(raw_line)
            parse_line(line)
            _append_run_log(status, line)
            event_at = status.get("last_event_at") or _now_iso()
            status["updated_at"] = event_at
            _update_stage(status, stage_id, updated_at=event_at, log_line_count=line_count)
            now_monotonic = time.monotonic()
            if now_monotonic - last_status_write >= 1.0:
                _write_production_run_status(status)
                last_status_write = now_monotonic
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1.0)

    exit_code = process.wait()
    log_handle.flush()
    finished_at = _now_iso()
    status["updated_at"] = finished_at
    _update_stage(
        status,
        stage_id,
        status="done" if exit_code == 0 else "failed",
        updated_at=finished_at,
        finished_at=finished_at,
        exit_code=exit_code,
        progress_pct=100 if exit_code == 0 else None,
    )
    if exit_code == 0 and "segment_state" in stage_id:
        try:
            con = sqlite3.connect(ACTIVE_DB_PATH)
            con.row_factory = sqlite3.Row
            try:
                state_summary = _latest_segment_state_summary(con)
            finally:
                con.close()
            state_run_id = _int(state_summary.get("run_id"))
            if state_run_id:
                _update_stage_metric(status, stage_id, "segment_state_run_id", state_run_id)
        except Exception as exc:  # pragma: no cover - diagnostic metadata only
            _append_run_log(status, f"[dashboard] segment-state metric refresh failed: {exc}")
    _write_production_run_status(status)
    return exit_code


def _refresh_run_effect_summary(status: dict[str, Any]) -> None:
    output_written = 0
    files_touched = 0
    segment_state_ids: list[int] = []
    for stage in status.get("stages") or []:
        if not isinstance(stage, dict):
            continue
        stage_id = str(stage.get("id") or "")
        metrics = stage.get("metrics") if isinstance(stage.get("metrics"), dict) else {}
        is_write_stage = stage_id.endswith("_write")
        stage_output_written = _int(metrics.get("output_written"))
        stage_files_touched = _int(metrics.get("files_touched"))
        if is_write_stage:
            # segment-apply reports written rows as "Applied updates"; other
            # writers usually expose "Output written". Treat both as output
            # effects, but only on write stages so dry-runs do not trip the gate.
            stage_output_written = max(stage_output_written, _int(metrics.get("applied")))
            if not stage_files_touched and stage_output_written:
                stage_files_touched = _int(metrics.get("files_with_ready_updates"))
        output_written += stage_output_written
        files_touched += stage_files_touched
        state_id = _int(metrics.get("segment_state_run_id") or metrics.get("state_run_id"))
        if state_id:
            segment_state_ids.append(state_id)

    status["output_written_count"] = output_written
    status["files_touched_count"] = files_touched
    status["output_changed"] = output_written > 0 or files_touched > 0
    status["new_segment_state_run_id"] = segment_state_ids[-1] if segment_state_ids else None
    status["new_segment_state_created"] = bool(segment_state_ids)


def _failure_payload(status: dict[str, Any], message: str, stage_id: str | None = None) -> dict[str, Any]:
    failed_stage = stage_id or status.get("failed_stage") or status.get("current_stage") or ""
    failure: dict[str, Any] = {
        "reason": "stage_failed",
        "stage": failed_stage,
        "message": message,
        "report_path": status.get("report_path"),
    }
    if "insufficient disk space" in message.lower() or status.get("failure", {}).get("reason") == "insufficient_disk_space":
        disk_preflight = status.get("disk_preflight") or status.get("failure", {}).get("disk_preflight") or _production_disk_preflight_payload()
        failure.update(
            {
                "reason": "insufficient_disk_space",
                "message": disk_preflight.get("message") or message,
                "disk_preflight": disk_preflight,
            }
        )
        status["disk_preflight"] = disk_preflight
    return failure


def _stage_metrics(status: dict[str, Any], stage_id: str) -> dict[str, Any]:
    for stage in status.get("stages") or []:
        if isinstance(stage, dict) and stage.get("id") == stage_id:
            return stage.get("metrics") if isinstance(stage.get("metrics"), dict) else {}
    return {}


def _run_publication_preflight_stage(
    status: dict[str, Any],
    log_handle,
    *,
    stage_id: str,
    force: bool = False,
    require_apply_queue: bool = False,
) -> tuple[list[int], dict[str, Any]]:
    _start_inline_stage(status, stage_id, "Validating publication preflight.", log_handle)
    payload = _publication_preflight_payload(ACTIVE_DB_PATH, force=force)
    preflight = payload.get("publication_preflight") if isinstance(payload, dict) else {}
    if not isinstance(preflight, dict):
        preflight = {}
    counts = preflight.get("counts") if isinstance(preflight.get("counts"), dict) else {}
    segment_ids = _publication_apply_segment_ids(payload)
    expected_apply_count = _int(counts.get("needs_apply"))
    status["publication_preflight"] = preflight
    status["publication_apply_segment_ids"] = segment_ids
    metrics = {
        "needs_apply": expected_apply_count,
        "apply_preview": _int(counts.get("apply_preview")),
        "apply_ready": _int(counts.get("apply_ready")),
        "apply_blocked": _int(counts.get("apply_blocked")),
        "apply_token_mismatch": _int(counts.get("apply_token_mismatch")),
        "apply_missing_token_policy": _int(counts.get("apply_missing_token_policy")),
        "apply_policy_ready": _int(counts.get("apply_policy_ready")),
        "apply_policy_blocked": _int(counts.get("apply_policy_blocked")),
        "apply_policy_missing_decision": _int(counts.get("apply_policy_missing_decision")),
        "apply_policy_missing_run": _int(counts.get("apply_policy_missing_run")),
        "apply_policy_stale_confirmed_hash": _int(counts.get("apply_policy_stale_confirmed_hash")),
        "apply_policy_stale_output_hash": _int(counts.get("apply_policy_stale_output_hash")),
        "apply_mixed_ready": _int(counts.get("apply_mixed_ready")),
        "apply_mixed_blocked": _int(counts.get("apply_mixed_blocked")),
        "apply_mixed_token_policy_ready": _int(counts.get("apply_mixed_token_policy_ready")),
        "apply_mixed_token_mismatch": _int(counts.get("apply_mixed_token_mismatch")),
        "apply_mixed_missing_decision": _int(counts.get("apply_mixed_missing_decision")),
        "apply_mixed_stale_confirmed_hash": _int(counts.get("apply_mixed_stale_confirmed_hash")),
        "apply_mixed_stale_output_hash": _int(counts.get("apply_mixed_stale_output_hash")),
        "apply_lifecycle": _int(counts.get("apply_lifecycle")),
        "apply_output_write": _int(counts.get("apply_output_write")),
        "apply_kind_mixed": _int(counts.get("apply_kind_mixed")),
        "lifecycle_apply_ready": _int(counts.get("lifecycle_apply_ready")),
        "lifecycle_apply_blocked": _int(counts.get("lifecycle_apply_blocked")),
        "apply_segment_ids": len(segment_ids),
        "apply_strategy": str(preflight.get("apply_strategy") or "blocked"),
        "token_policy_run_id": _int(preflight.get("token_policy_run_id")),
        "promotions": _int(counts.get("promotions")),
        "score_regressions": _int(counts.get("score_regressions")),
        "unhandled_by_network": _int(counts.get("unhandled_by_network")),
        "low_score": _int(counts.get("low_score")),
        "can_apply_pending": 1 if preflight.get("can_apply_pending") else 0,
        "can_publish_now": 1 if preflight.get("can_publish_now") else 0,
    }
    if require_apply_queue:
        if preflight.get("can_apply_pending") is not True:
            raise RuntimeError(
                "Publication apply blocked by preflight: "
                f"{preflight.get('next_action') or 'unknown'} "
                f"(ready {_int(counts.get('apply_ready'))} / needs_apply {expected_apply_count}; "
                f"token_mismatch {_int(counts.get('apply_token_mismatch'))}; "
                f"policy_ready {_int(counts.get('apply_policy_ready'))}; "
                f"policy_missing {_int(counts.get('apply_policy_missing_decision'))}; "
                f"mixed_ready {_int(counts.get('apply_mixed_ready'))})."
            )
        if expected_apply_count <= 0:
            raise RuntimeError("Publication apply blocked: no confirmed apply queue.")
        if len(segment_ids) != expected_apply_count:
            raise RuntimeError(
                f"Publication apply blocked: preview ids {len(segment_ids)} != needs_apply {expected_apply_count}."
            )
    _finish_inline_stage(
        status,
        stage_id,
        metrics=metrics,
        message=f"Publication preflight ok: {len(segment_ids)} apply ids.",
    )
    return segment_ids, preflight


def _publication_apply_worker(run_id: str) -> None:
    status = _new_publication_apply_status(run_id)
    log_path = Path(status["log_path"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _write_production_run_status(status)
    try:
        with log_path.open("w", encoding="utf-8", newline="\n") as log_handle:
            log_handle.write(f"CK3 PT-BR publication apply run {run_id}\n")
            log_handle.write("Mode: publication_apply_confirmed\n")
            segment_ids, preflight = _run_publication_preflight_stage(
                status,
                log_handle,
                stage_id="publication_preflight",
                force=False,
                require_apply_queue=True,
            )
            segment_ids_csv = ",".join(str(segment_id) for segment_id in segment_ids)
            apply_strategy = str(preflight.get("apply_strategy") or "normal")
            token_policy_run_id = _int(preflight.get("token_policy_run_id"))
            status["publication_apply_strategy"] = apply_strategy
            base_apply_command = [
                sys.executable,
                "pipeline/main.py",
                "segment-apply",
                "--segment-ids",
                segment_ids_csv,
                "--segment-include-auto-confirmed",
                "--segment-include-intentional-blank",
            ]
            if apply_strategy == "token_policy":
                base_apply_command.append("--segment-require-token-policy-decision")
                if token_policy_run_id:
                    base_apply_command.extend(["--token-policy-run-id", str(token_policy_run_id)])
            elif apply_strategy == "mixed_token_policy":
                base_apply_command.append("--segment-allow-token-policy-decision")
                if token_policy_run_id:
                    base_apply_command.extend(["--token-policy-run-id", str(token_policy_run_id)])
            log_handle.write(f"Apply strategy: {apply_strategy}\n")
            if token_policy_run_id:
                log_handle.write(f"Token policy run id: {token_policy_run_id}\n")
            if apply_strategy == "lifecycle_closure":
                state_run_id = _int(preflight.get("segment_state_run_id"))
                learning_run_id = _latest_local_learning_run_id(ACTIVE_DB_PATH)
                if not state_run_id:
                    raise RuntimeError("Publication lifecycle apply blocked: missing segment-state run id.")
                if not learning_run_id:
                    raise RuntimeError("Publication lifecycle apply blocked: missing learning run id.")
                lifecycle_command = [
                    sys.executable,
                    "pipeline/human_confirmed_local_learning_lifecycle_policy_materializer.py",
                    "--learning-run-id",
                    str(learning_run_id),
                    "--segment-state-run-id",
                    str(state_run_id),
                    "--segment-ids",
                    segment_ids_csv,
                ]
                dry_exit = _run_production_command(
                    status,
                    stage_id="apply_confirmed_dry_run",
                    command=lifecycle_command,
                    log_handle=log_handle,
                )
                if dry_exit != 0:
                    raise RuntimeError("Publication lifecycle dry-run failed.")
                dry_metrics = _stage_metrics(status, "apply_confirmed_dry_run")
                released_count = _int(dry_metrics.get("released"))
                blocked_count = _int(dry_metrics.get("blocked"))
                if released_count != len(segment_ids) or blocked_count:
                    raise RuntimeError(
                        f"Publication lifecycle dry-run mismatch: released {released_count}, "
                        f"blocked {blocked_count}, expected {len(segment_ids)}."
                    )
                write_exit = _run_production_command(
                    status,
                    stage_id="apply_confirmed_write",
                    command=[*lifecycle_command, "--apply"],
                    log_handle=log_handle,
                )
                if write_exit != 0:
                    raise RuntimeError("Publication lifecycle apply failed.")
                write_metrics = _stage_metrics(status, "apply_confirmed_write")
                released_count = _int(write_metrics.get("released"))
                blocked_count = _int(write_metrics.get("blocked"))
                policy_run_id = _int(write_metrics.get("policy_run_id"))
                if released_count != len(segment_ids) or blocked_count or not policy_run_id:
                    raise RuntimeError(
                        f"Publication lifecycle apply mismatch: released {released_count}, "
                        f"blocked {blocked_count}, policy_run_id {policy_run_id}, expected {len(segment_ids)}."
                    )
            else:
                dry_exit = _run_production_command(
                    status,
                    stage_id="apply_confirmed_dry_run",
                    command=base_apply_command,
                    log_handle=log_handle,
                )
                if dry_exit != 0:
                    raise RuntimeError("Publication dry-run failed.")
                dry_metrics = _stage_metrics(status, "apply_confirmed_dry_run")
                ready_count = _int(dry_metrics.get("ready"))
                if ready_count != len(segment_ids):
                    raise RuntimeError(
                        f"Publication dry-run mismatch: ready {ready_count} != expected {len(segment_ids)}."
                    )
                write_exit = _run_production_command(
                    status,
                    stage_id="apply_confirmed_write",
                    command=[*base_apply_command, "--auto-apply"],
                    log_handle=log_handle,
                )
                if write_exit != 0:
                    raise RuntimeError("Publication apply failed.")
                write_metrics = _stage_metrics(status, "apply_confirmed_write")
                applied_count = _int(write_metrics.get("applied") or write_metrics.get("output_written"))
                if applied_count != len(segment_ids):
                    raise RuntimeError(
                        f"Publication apply mismatch: applied {applied_count} != expected {len(segment_ids)}."
                    )
            state_exit = _run_production_command(
                status,
                stage_id="segment_state_after",
                command=[sys.executable, "pipeline/main.py", "segment-state"],
                log_handle=log_handle,
            )
            if state_exit != 0:
                raise RuntimeError("Publication segment-state failed.")
            _run_publication_preflight_stage(
                status,
                log_handle,
                stage_id="publication_preflight_after",
                force=True,
                require_apply_queue=False,
            )
            status["status"] = "completed"
            status["message"] = "Publication apply run completed."
            _refresh_run_effect_summary(status)
            _attach_evaluation_decision_report(status)
            status["finished_at"] = _now_iso()
            status["current_stage"] = "production_report"
            status["current_label"] = _stage_label(status, "production_report")
            _update_stage(status, "production_report", status="running", started_at=_now_iso(), updated_at=_now_iso(), progress_pct=0)
            _update_stage(status, "production_report", status="done", updated_at=_now_iso(), finished_at=_now_iso(), exit_code=0, progress_pct=100)
            _write_production_run_status(status)
            _write_production_report(status)
            try:
                _refresh_dashboard_cache(ACTIVE_DB_PATH)
            except Exception as exc:  # pragma: no cover - cache should not fail publication apply
                _append_run_log(status, f"[dashboard_cache] refresh failed: {exc}")
                _write_production_run_status(status)
    except Exception as exc:  # pragma: no cover - background publication path
        current_stage = status.get("current_stage")
        if current_stage:
            _update_stage(status, current_stage, status="failed", finished_at=_now_iso(), exit_code=1)
        status["status"] = "failed"
        status["message"] = f"Publication apply run failed: {exc}"
        status["failed_stage"] = current_stage or status.get("failed_stage") or ""
        status["failure"] = _failure_payload(status, str(exc), status["failed_stage"])
        _refresh_run_effect_summary(status)
        status["finished_at"] = _now_iso()
        _append_run_log(status, status["message"])
        _write_production_report(status)
        _write_production_run_status(status)


def _production_run_worker(run_id: str) -> None:
    status = _new_production_run_status(run_id)
    log_path = Path(status["log_path"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _write_production_run_status(status)
    commands = [
        ("preflight_sync", [sys.executable, "pipeline/main.py", "cycle"]),
        ("segment_state_before", [sys.executable, "pipeline/main.py", "segment-state"]),
        (
            "apply_general_dry_run",
            [
                sys.executable,
                "pipeline/main.py",
                "segment-apply",
                "--segment-include-auto-confirmed",
                "--segment-include-intentional-blank",
            ],
        ),
        (
            "apply_token_policy_dry_run",
            [
                sys.executable,
                "pipeline/main.py",
                "segment-apply",
                "--segment-include-auto-confirmed",
                "--segment-include-intentional-blank",
                "--segment-require-token-policy-decision",
            ],
        ),
        (
            "controlled_token_subpolicy_dry_run",
            [
                sys.executable,
                "pipeline/main.py",
                "controlled-token-subpolicy-apply",
            ],
        ),
        (
            "select_cstring_bridge_dry_run",
            [
                sys.executable,
                "pipeline/main.py",
                "select-cstring-governed-bridge-apply",
            ],
        ),
        (
            "same_token_boundary_repair_audit",
            [
                sys.executable,
                "pipeline/main.py",
                "auto-confirmation-text-boundary-repair-production-audit",
            ],
        ),
        (
            "same_token_boundary_repair_dry_run",
            [
                sys.executable,
                "pipeline/main.py",
                "same-token-boundary-repair-apply",
            ],
        ),
        (
            "title_landed_es_repair_dry_run",
            [
                sys.executable,
                "pipeline/title_landed_es_reviewed_repair_dry_run.py",
                "--queue-run-id",
                "150",
            ],
        ),
        (
            "apply_general_write",
            [
                sys.executable,
                "pipeline/main.py",
                "segment-apply",
                "--segment-include-auto-confirmed",
                "--segment-include-intentional-blank",
                "--auto-apply",
            ],
        ),
        (
            "apply_token_policy_write",
            [
                sys.executable,
                "pipeline/main.py",
                "segment-apply",
                "--segment-include-auto-confirmed",
                "--segment-include-intentional-blank",
                "--segment-require-token-policy-decision",
                "--auto-apply",
            ],
        ),
        (
            "controlled_token_subpolicy_write",
            [
                sys.executable,
                "pipeline/main.py",
                "controlled-token-subpolicy-apply",
                "--auto-apply",
            ],
        ),
        (
            "select_cstring_bridge_write",
            [
                sys.executable,
                "pipeline/main.py",
                "select-cstring-governed-bridge-apply",
                "--auto-apply",
            ],
        ),
        (
            "same_token_boundary_repair_write",
            [
                sys.executable,
                "pipeline/main.py",
                "same-token-boundary-repair-apply",
                "--auto-apply",
            ],
        ),
        (
            "title_landed_es_repair_write",
            [
                sys.executable,
                "pipeline/title_landed_es_reviewed_repair_apply.py",
                "--apply",
            ],
        ),
        (
            "apply_locked_override_write",
            [
                sys.executable,
                "pipeline/main.py",
                "segment-apply",
                "--segment-review-states",
                "human_locked,human_confirmed",
                "--segment-include-intentional-blank",
                "--segment-allow-locked-token-override",
                "--auto-apply",
            ],
        ),
        ("segment_state_after", [sys.executable, "pipeline/main.py", "segment-state"]),
        (
            "token_policy_after",
            [
                sys.executable,
                "pipeline/main.py",
                "segment-token-policy",
                "--segment-include-auto-confirmed",
            ],
        ),
        (
            "controlled_token_subpolicy_reaudit",
            [
                sys.executable,
                "pipeline/main.py",
                "controlled-token-subpolicy-apply",
                "--controlled-token-subpolicy-reaudit",
            ],
        ),
        (
            "select_cstring_bridge_reaudit",
            [
                sys.executable,
                "pipeline/main.py",
                "select-cstring-governed-bridge-apply",
                "--select-cstring-bridge-reaudit",
            ],
        ),
        (
            "same_token_boundary_repair_reaudit",
            [
                sys.executable,
                "pipeline/main.py",
                "same-token-boundary-repair-apply",
                "--same-token-boundary-repair-reaudit",
            ],
        ),
        (
            "title_landed_es_repair_reaudit",
            [
                sys.executable,
                "pipeline/title_landed_es_reviewed_repair_apply.py",
                "--reaudit",
            ],
        ),
        ("composite_review_progress", [sys.executable, "pipeline/main.py", "ml-composite-review-progress"]),
    ]
    if not status.get("apply_output"):
        evaluation_stage_ids = {
            "preflight_sync",
            "segment_state_before",
            "apply_general_dry_run",
            "apply_token_policy_dry_run",
            "controlled_token_subpolicy_dry_run",
            "select_cstring_bridge_dry_run",
            "same_token_boundary_repair_audit",
            "same_token_boundary_repair_dry_run",
            "title_landed_es_repair_dry_run",
            "composite_review_progress",
        }
        commands = [(stage_id, command) for stage_id, command in commands if stage_id in evaluation_stage_ids]
    try:
        with log_path.open("w", encoding="utf-8", newline="\n") as log_handle:
            log_handle.write(f"CK3 PT-BR production run {run_id}\n")
            log_handle.write(f"Mode: {status.get('mode')}\n")
            _create_production_snapshot(status, log_handle)
            archive_exit = _archive_production_snapshot(status, log_handle)
            if archive_exit != 0:
                status["status"] = "failed"
                status["message"] = "Stage failed: snapshot_archive"
                status["failed_stage"] = "snapshot_archive"
                status["failure"] = _failure_payload(status, status["message"], "snapshot_archive")
            for stage_id, command in commands:
                if status.get("status") == "failed":
                    break
                exit_code = _run_production_command(status, stage_id=stage_id, command=command, log_handle=log_handle)
                if exit_code != 0:
                    status["status"] = "failed"
                    status["message"] = f"Stage failed: {stage_id}"
                    status["failed_stage"] = stage_id
                    status["failure"] = _failure_payload(status, status["message"], stage_id)
                    break
            _refresh_run_effect_summary(status)
            if status.get("status") != "failed" and not status.get("apply_output"):
                _run_package_score_recalibration_stage(status, log_handle)
            if status.get("status") != "failed":
                status["status"] = "completed"
                status["message"] = (
                    "Evaluation run completed."
                    if not status.get("apply_output")
                    else "Full production run completed."
                )
            status["finished_at"] = _now_iso()
            status["current_stage"] = "production_report"
            status["current_label"] = _stage_label(status, "production_report")
            _update_stage(status, "production_report", status="running", started_at=_now_iso(), updated_at=_now_iso(), progress_pct=0)
            _update_stage(status, "production_report", status="done", updated_at=_now_iso(), finished_at=_now_iso(), exit_code=0, progress_pct=100)
            _write_production_run_status(status)
            _write_production_report(status)
            try:
                _refresh_dashboard_cache(ACTIVE_DB_PATH)
            except Exception as exc:  # pragma: no cover - cache should not fail production
                _append_run_log(status, f"[dashboard_cache] refresh failed: {exc}")
                _write_production_run_status(status)
    except Exception as exc:  # pragma: no cover - background production path
        current_stage = status.get("current_stage")
        if current_stage:
            _update_stage(status, current_stage, status="failed", finished_at=_now_iso(), exit_code=1)
        status["status"] = "failed"
        status["message"] = f"Production run failed: {exc}"
        status["failed_stage"] = current_stage or status.get("failed_stage") or ""
        status["failure"] = _failure_payload(status, str(exc), status["failed_stage"])
        _refresh_run_effect_summary(status)
        status["finished_at"] = _now_iso()
        _append_run_log(status, status["message"])
        _write_production_report(status)
        _write_production_run_status(status)


def _start_production_run() -> dict[str, Any]:
    with PRODUCTION_RUN_LOCK:
        if _production_run_active():
            status = _read_production_run_status()
            return {"accepted": False, "status": "already_running", "run": status}
        run_id = _production_run_id()
        status = _new_production_run_status(run_id)
        _write_production_run_status(status)
        thread = threading.Thread(target=_production_run_worker, args=(run_id,), daemon=True)
        thread.start()
        return {"accepted": True, "status": "running", "run": status}


def _start_publication_apply_run() -> dict[str, Any]:
    with PRODUCTION_RUN_LOCK:
        if _production_run_active():
            status = _read_production_run_status()
            return {"accepted": False, "status": "already_running", "run": status}
        run_id = _production_run_id()
        status = _new_publication_apply_status(run_id)
        _write_production_run_status(status)
        thread = threading.Thread(target=_publication_apply_worker, args=(run_id,), daemon=True)
        thread.start()
        return {"accepted": True, "status": "running", "run": status}


def _training_lock_payload(con: sqlite3.Connection) -> dict[str, Any]:
    file_locks = []
    for path in TRAINING_LOCK_FILES:
        if not path.exists():
            continue
        payload: dict[str, Any] = {"path": str(path), "reason": "lock_file_present"}
        if path.suffix == ".json":
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    payload.update(parsed)
            except Exception as exc:
                payload["read_error"] = str(exc)
        file_locks.append(payload)

    unfinished_model = {}
    if _table_exists(con, "ml_model_runs"):
        unfinished_model = _one(
            con,
            """
            SELECT id, model_kind, model_version, started_at, finished_at
            FROM ml_model_runs
            WHERE finished_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
        )

    locked = bool(file_locks or unfinished_model)
    if file_locks:
        reason = "training_or_learning_lock_file"
    elif unfinished_model:
        reason = "unfinished_model_run"
    else:
        reason = "released_for_production"
    return {
        "locked": locked,
        "reason": reason,
        "file_locks": file_locks,
        "unfinished_model_run": unfinished_model,
        "message": "Producao liberada" if not locked else "Producao bloqueada ate o chat de aprendizado liberar",
    }


def _learning_status_payload(con: sqlite3.Connection) -> dict[str, Any]:
    status = _read_json_file(LEARNING_STATUS_FILE)
    lock = _training_lock_payload(con)
    if not status:
        status = {
            "schema_version": 1,
            "owner": "",
            "status": "idle",
            "production_safe": not bool(lock.get("locked")),
            "objective": "No active learning cycle",
            "current_phase": "",
            "current_phase_label": "",
            "phase_total": 0,
            "phase_completed": 0,
            "phase_pending": 0,
            "progress_pct": 100.0 if not lock.get("locked") else 0.0,
            "phases": [],
            "last_report": "",
            "blockers": [],
            "warnings": [],
            "next_action": "Production may run" if not lock.get("locked") else "Wait for learning release",
        }
    status["lock"] = lock
    status["production_safe"] = bool(status.get("production_safe")) and not bool(lock.get("locked"))
    status["can_start_production"] = status["production_safe"]
    status["gate_message"] = (
        "Learning front released production"
        if status["can_start_production"]
        else "Production blocked by learning front"
    )
    return status


def _production_payload(con: sqlite3.Connection) -> dict[str, Any]:
    lifecycle = _lifecycle_payload(con)
    agents = _agents_payload(con)
    summary = lifecycle.get("summary") or {}
    output_application = lifecycle.get("outputApplication") or []
    output_apply = lifecycle.get("outputApply") or {}
    active_gate = agents.get("activeGate") or {}
    latest_apply_runs = output_apply.get("runs") or []
    last_apply = next((row for row in latest_apply_runs if _int(row.get("apply")) == 1), latest_apply_runs[0] if latest_apply_runs else {})
    lock = _training_lock_payload(con)
    learning_status = _learning_status_payload(con)
    production_run = _read_production_run_status()
    production_run_active = production_run.get("status") in {"starting", "running"}

    active_segments = _int(summary.get("total_segments"))
    closed = _int(summary.get("closed_count"))
    pending = _int(summary.get("pending_count"))
    needs_apply = _int(summary.get("output_apply_pending_count"))
    taxonomy = _pending_taxonomy_payload(con, _int(summary.get("run_id")))
    actionable_pending = _int(taxonomy.get("actionable_pending"))
    model_watch = _int(taxonomy.get("model_suspicion_watch"))
    governed_bridge_pending = _int(taxonomy.get("governed_bridge_pending"))
    applied_segments = sum(
        _int(row.get("total"))
        for row in output_application
        if row.get("apply_state") == "applied"
    )
    blocked_critical = _int(active_gate.get("invalid_release_count"))
    auto_apply_allowed = bool(_int(active_gate.get("auto_apply_allowed")))
    readiness_status = "blocked"
    recommended_action = "review_blockers"
    if not learning_status.get("can_start_production"):
        readiness_status = "learning_locked"
        recommended_action = "wait_learning_release"
    elif blocked_critical > 0 or auto_apply_allowed:
        readiness_status = "blocked"
        recommended_action = "review_governance"
    elif actionable_pending > 0 or model_watch > 0:
        readiness_status = "ready_with_known_issues"
        if needs_apply > 0:
            recommended_action = "run_production_apply"
        elif governed_bridge_pending > 0:
            recommended_action = "investigate_governed_bridge"
        elif model_watch > 0:
            recommended_action = "sample_model_watch"
        else:
            recommended_action = "review_actionable_pending"
    else:
        readiness_status = "ready_for_game_test"
        recommended_action = "run_game_test"

    stages = [
        {
            "id": "source_audit",
            "label": "Source Audit",
            "status": "done",
            "total": active_segments,
            "completed": active_segments,
            "pending": 0,
            "last_report": "",
        },
        {
            "id": "mirror_check",
            "label": "Mirror Check",
            "status": "done" if active_segments else "pending",
            "total": active_segments,
            "completed": active_segments,
            "pending": 0,
            "last_report": "",
        },
        {
            "id": "indexing",
            "label": "Indexing",
            "status": "done" if active_segments else "pending",
            "total": active_segments,
            "completed": active_segments,
            "pending": 0,
            "last_report": "",
        },
        {
            "id": "memory_apply",
            "label": "Memory Apply",
            "status": "done",
            "total": active_segments,
            "completed": closed,
            "pending": actionable_pending,
            "last_report": "",
        },
        {
            "id": "deterministic_guards",
            "label": "Deterministic Guards",
            "status": "done" if blocked_critical == 0 else "blocked",
            "total": _int(active_gate.get("guarded_release_count")),
            "completed": _int(active_gate.get("guarded_release_count")) - blocked_critical,
            "pending": blocked_critical,
            "last_report": active_gate.get("latest_report_path") or "",
        },
        {
            "id": "ml_network",
            "label": "ML Network",
            "status": "done" if _int(active_gate.get("active_overlay_run_id")) else "pending",
            "total": _int(active_gate.get("guarded_release_count")),
            "completed": _int(active_gate.get("guarded_release_count")),
            "pending": 0,
            "last_report": active_gate.get("latest_report_path") or "",
        },
        {
            "id": "controlled_output_apply",
            "label": "Controlled Output Apply",
            "status": "blocked" if needs_apply else "done",
            "total": _int(last_apply.get("candidates_count")) or needs_apply + _int(last_apply.get("applied_count")),
            "completed": _int(last_apply.get("applied_count")),
            "pending": needs_apply,
            "last_report": last_apply.get("report_path") or "",
        },
        {
            "id": "final_validation",
            "label": "Final Validation",
            "status": "pending" if actionable_pending else "done",
            "total": active_segments,
            "completed": closed,
            "pending": actionable_pending,
            "last_report": "",
        },
        {
            "id": "release_report",
            "label": "Release Report",
            "status": "pending",
            "total": 1,
            "completed": 0,
            "pending": 1,
            "last_report": "",
        },
        {
            "id": "learning_handoff",
            "label": "Learning Handoff",
            "status": "pending" if actionable_pending or model_watch else "done",
            "total": actionable_pending + model_watch,
            "completed": 0 if actionable_pending or model_watch else 1,
            "pending": actionable_pending + model_watch,
            "last_report": "",
        },
    ]

    blockers = []
    if not learning_status.get("can_start_production"):
        blockers.append(
            {
                "type": "learning_lock",
                "count": 1,
                "message": learning_status.get("gate_message") or lock.get("message"),
            }
        )
    if needs_apply:
        blockers.append({"type": "token_policy", "count": needs_apply, "message": "Outputs confirmados ainda precisam de politica de token ou revalidacao"})
    if governed_bridge_pending:
        blockers.append({"type": "governed_bridge_pending", "count": governed_bridge_pending, "message": "Ponte governada pendente; investigar antes de aplicar as cegas"})
    remaining_actionable = max(actionable_pending - governed_bridge_pending - needs_apply, 0)
    if remaining_actionable:
        blockers.append({"type": "actionable_pending", "count": remaining_actionable, "message": "Pendencias acionaveis reais para revisao, autofix ou subpolitica"})
    if model_watch:
        blockers.append({"type": "model_suspicion_watch", "count": model_watch, "message": "Suspeitas conservadoras do modelo; amostrar/calibrar, nao revisar tudo manualmente"})
    if blocked_critical:
        blockers.append({"type": "critical_gate", "count": blocked_critical, "message": "Gate ativo encontrou releases invalidos"})

    return {
        "status": "blocked" if not learning_status.get("can_start_production") else "idle",
        "active_run_id": production_run.get("run_id") if production_run_active else None,
        "run": production_run if production_run_active else None,
        "lock": lock,
        "learning": {
            "status": learning_status.get("status"),
            "production_safe": learning_status.get("production_safe"),
            "can_start_production": learning_status.get("can_start_production"),
            "objective": learning_status.get("objective"),
            "current_phase": learning_status.get("current_phase"),
            "current_phase_label": learning_status.get("current_phase_label"),
            "progress_pct": learning_status.get("progress_pct"),
            "last_report": learning_status.get("last_report"),
            "next_action": learning_status.get("next_action"),
        },
        "source": {
            "changed": False,
            "last_index_at": summary.get("started_at") or "",
            "active_segments": active_segments,
        },
        "output": {
            "changed_files": _int(last_apply.get("files_touched_count")),
            "structure_valid": True,
            "needs_apply": needs_apply,
            "last_apply_count": _int(last_apply.get("applied_count")),
        },
        "gate": {
            "active_overlay_run_id": _int(active_gate.get("active_overlay_run_id")),
            "auto_apply_allowed": auto_apply_allowed,
            "invalid_releases": blocked_critical,
            "guarded_releases": _int(active_gate.get("guarded_release_count")),
        },
        "readiness": {
            "status": readiness_status,
            "closed_pct": _pct(closed, active_segments),
            "pending_operational": actionable_pending,
            "raw_pending": pending,
            "actionable_pending": actionable_pending,
            "model_suspicion_watch": model_watch,
            "governed_bridge_pending": governed_bridge_pending,
            "blocked_critical": blocked_critical,
            "recommended_action": recommended_action,
        },
        "summary": {
            "active_segments": active_segments,
            "closed_segments": closed,
            "closed_pct": _pct(closed, active_segments),
            "pending_operational": actionable_pending,
            "actionable_pending": actionable_pending,
            "model_suspicion_watch": model_watch,
            "governed_bridge_pending": governed_bridge_pending,
            "raw_pending": pending,
            "learning_backlog": _int(taxonomy.get("learning_backlog")),
            "applied": applied_segments,
            "needs_apply": needs_apply,
            "valid_blank": _int(summary.get("blank_valid_count")),
            "intentional_blank": _int(summary.get("blank_intentional_count")),
            "blocked_critical": blocked_critical,
            "select_cstring": taxonomy.get("select_cstring") or {},
            "taxonomy_distribution": taxonomy.get("distribution") or [],
        },
        "stages": stages,
        "logs": [
            {
                "time": datetime.now().strftime("%H:%M:%S"),
                "stage": "production-status",
                "level": "info",
                "message": f"Estado de producao: {readiness_status}",
            }
        ],
        "blockers": blockers,
        "links": {
            "analytic_dashboard": "http://127.0.0.1:5173/#Operational",
            "managerial_dashboard": "http://127.0.0.1:5173/#Managerial",
            "latest_report": last_apply.get("report_path") or "",
        },
    }


def _dataset_negative_coverage(con: sqlite3.Connection, dataset_run_id: Any) -> float:
    row = _one(
        con,
        """
        SELECT negative_count, total_count
        FROM ml_dataset_runs
        WHERE id = ?
        """,
        (_int(dataset_run_id),),
    )
    return round(_pct(row.get("negative_count"), row.get("total_count")) / 100, 4)


def _policy_payload(con: sqlite3.Connection) -> dict[str, Any]:
    exists = _one(
        con,
        """
        SELECT COUNT(*) AS total
        FROM sqlite_master
        WHERE type = 'table'
          AND name IN ('ml_policy_runs', 'ml_policy_items')
        """,
    )
    if _int(exists.get("total")) < 2:
        return {"available": False, "message": "Nenhuma politica operacional executada ainda"}

    summary = _one(
        con,
        """
        SELECT
          pr.id AS policy_run_id,
          pr.rule_version,
          pr.score_run_id,
          pr.model_run_id,
          pr.model_version,
          pr.scored_count,
          pr.active_auto_safe_count,
          pr.policy_auto_safe_count,
          pr.new_safe_count,
          pr.demoted_safe_count,
          pr.protect_active_safe,
          ROUND(100.0 * pr.active_auto_safe_count / NULLIF(pr.scored_count, 0), 2) AS active_auto_safe_pct,
          ROUND(100.0 * pr.policy_auto_safe_count / NULLIF(pr.scored_count, 0), 2) AS policy_auto_safe_pct,
          ROUND(100.0 * pr.new_safe_count / NULLIF(pr.scored_count, 0), 4) AS new_safe_pct,
          pr.started_at,
          pr.finished_at
        FROM ml_policy_runs pr
        ORDER BY pr.id DESC
        LIMIT 1
        """,
    )
    if not summary:
        return {"available": False, "message": "Nenhuma politica operacional executada ainda"}

    policy_run_id = _int(summary.get("policy_run_id"))
    comparison_rows = _all(
        con,
        """
        WITH score_counts AS (
          SELECT
            'score' AS source,
            msi.final_action AS action,
            COUNT(*) AS total
          FROM ml_policy_runs pr
          JOIN ml_score_items msi ON msi.run_id = pr.score_run_id
          WHERE pr.id = ?
          GROUP BY msi.final_action
        ),
        policy_counts AS (
          SELECT
            'policy' AS source,
            mpi.policy_action AS action,
            COUNT(*) AS total
          FROM ml_policy_items mpi
          WHERE mpi.run_id = ?
          GROUP BY mpi.policy_action
        )
        SELECT source, action, total
        FROM score_counts
        UNION ALL
        SELECT source, action, total
        FROM policy_counts
        ORDER BY source, action
        """,
        (policy_run_id, policy_run_id),
    )
    action_order = ["auto_safe", "needs_human", "needs_autofix", "blocked_structure"]
    comparison_by_action = {
        action: {"action": action, "score": 0, "policy": 0}
        for action in action_order
    }
    for row in comparison_rows:
        action = row.get("action")
        if action not in comparison_by_action:
            comparison_by_action[action] = {"action": action, "score": 0, "policy": 0}
        comparison_by_action[action][row["source"]] = _int(row.get("total"))
    comparison = [comparison_by_action[action] for action in comparison_by_action]

    group_gain = _all(
        con,
        """
        SELECT
          policy_group,
          COUNT(*) AS total_segments,
          SUM(CASE WHEN score_final_action = 'auto_safe' THEN 1 ELSE 0 END) AS score_auto_safe,
          SUM(CASE WHEN policy_action = 'auto_safe' THEN 1 ELSE 0 END) AS policy_auto_safe,
          SUM(new_safe) AS new_safe,
          SUM(CASE WHEN new_safe = 1 AND learned_positive = 1 THEN 1 ELSE 0 END) AS new_safe_learned_positive,
          SUM(CASE WHEN learned_negative = 1 THEN 1 ELSE 0 END) AS learned_negative_count,
          MIN(policy_threshold) AS min_threshold,
          MAX(policy_threshold) AS max_threshold,
          ROUND(100.0 * SUM(new_safe) / NULLIF(COUNT(*), 0), 4) AS new_safe_pct_in_group,
          ROUND(100.0 * SUM(CASE WHEN policy_action = 'auto_safe' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 2) AS policy_auto_safe_pct
        FROM ml_policy_items
        WHERE run_id = ?
        GROUP BY policy_group
        ORDER BY new_safe DESC, policy_group
        LIMIT 12
        """,
        (policy_run_id,),
    )

    audit_items = _all(
        con,
        """
        SELECT
          mpi.segment_id,
          mpi.policy_group,
          mpi.relative_path,
          mpi.source_key,
          mpi.model_safe_probability,
          mpi.policy_threshold,
          mpi.policy_require_learned_positive,
          mpi.score_final_action,
          mpi.policy_action,
          mpi.learned_positive,
          mpi.learned_negative,
          msi.candidate_text,
          os.portuguese_text AS output_text,
          mpi.reasons_json
        FROM ml_policy_items mpi
        JOIN ml_score_items msi ON msi.id = mpi.score_item_id
        LEFT JOIN output_segments os ON os.segment_id = mpi.segment_id
        WHERE mpi.run_id = ?
          AND mpi.new_safe = 1
        ORDER BY mpi.policy_group, mpi.model_safe_probability DESC, mpi.segment_id
        LIMIT 120
        """,
        (policy_run_id,),
    )

    history = _all(
        con,
        """
        SELECT
          id AS policy_run_id,
          rule_version,
          score_run_id,
          model_version,
          scored_count,
          active_auto_safe_count,
          policy_auto_safe_count,
          new_safe_count,
          demoted_safe_count,
          ROUND(100.0 * policy_auto_safe_count / NULLIF(scored_count, 0), 2) AS policy_auto_safe_pct,
          started_at,
          finished_at
        FROM ml_policy_runs
        ORDER BY id ASC
        """,
    )

    return {
        "available": True,
        "summary": summary,
        "comparison": comparison,
        "groupGain": group_gain,
        "auditItems": audit_items,
        "history": history,
    }


def _lab_payload(con: sqlite3.Connection) -> dict[str, Any]:
    summary = _one(
        con,
        """
        WITH active_registry AS (
          SELECT active_model_run_id
          FROM ml_model_registry
          WHERE model_kind = 'risk_action_classifier'
          LIMIT 1
        ),
        candidate_model AS (
          SELECT *
          FROM ml_model_runs
          WHERE model_kind = 'risk_action_classifier'
          ORDER BY id DESC
          LIMIT 1
        ),
        active_model AS (
          SELECT m.*
          FROM ml_model_runs m
          JOIN active_registry ar ON ar.active_model_run_id = m.id
        ),
        candidate_score AS (
          SELECT *
          FROM ml_score_runs
          WHERE model_run_id = (SELECT id FROM candidate_model)
          ORDER BY id DESC
          LIMIT 1
        ),
        active_score AS (
          SELECT *
          FROM ml_score_runs
          WHERE model_run_id = (SELECT id FROM active_model)
          ORDER BY id DESC
          LIMIT 1
        ),
        candidate_promotion AS (
          SELECT *
          FROM ml_model_promotions
          WHERE candidate_model_run_id = (SELECT id FROM candidate_model)
          ORDER BY id DESC
          LIMIT 1
        )
        SELECT
          cm.id AS candidate_model_run_id,
          cm.model_version AS candidate_model_version,
          cm.dataset_run_id AS candidate_dataset_run_id,
          cm.accuracy AS candidate_accuracy,
          cm.macro_f1 AS candidate_macro_f1,
          cm.false_safe_count AS candidate_false_safe_count,
          cm.false_safe_rate AS candidate_false_safe_rate,
          cm.safe_precision AS candidate_safe_precision,
          cm.safe_recall AS candidate_safe_recall,
          cm.safe_threshold AS candidate_safe_threshold,
          cs.id AS candidate_score_run_id,
          cs.scored_count AS candidate_scored_count,
          cs.final_auto_safe_count AS candidate_auto_safe_count,
          ROUND(100.0 * cs.final_auto_safe_count / NULLIF(cs.scored_count, 0), 2) AS candidate_auto_safe_pct,
          am.id AS active_model_run_id,
          am.model_version AS active_model_version,
          am.safe_precision AS active_safe_precision,
          am.safe_recall AS active_safe_recall,
          am.macro_f1 AS active_macro_f1,
          ast.id AS active_score_run_id,
          ast.scored_count AS active_scored_count,
          ast.final_auto_safe_count AS active_auto_safe_count,
          ROUND(100.0 * ast.final_auto_safe_count / NULLIF(ast.scored_count, 0), 2) AS active_auto_safe_pct,
          cp.id AS promotion_run_id,
          cp.decision AS promotion_decision,
          cp.policy_version AS promotion_policy_version,
          cp.reason AS promotion_reason,
          cp.created_at AS promotion_created_at
        FROM candidate_model cm
        CROSS JOIN active_model am
        LEFT JOIN candidate_score cs ON 1 = 1
        LEFT JOIN active_score ast ON 1 = 1
        LEFT JOIN candidate_promotion cp ON 1 = 1
        """,
    )
    if not summary:
        return {"available": False, "message": "Nenhum modelo experimental encontrado"}

    bars_rows = _all(
        con,
        """
        WITH active_registry AS (
          SELECT active_model_run_id
          FROM ml_model_registry
          WHERE model_kind = 'risk_action_classifier'
          LIMIT 1
        ),
        candidate_model AS (
          SELECT id
          FROM ml_model_runs
          WHERE model_kind = 'risk_action_classifier'
          ORDER BY id DESC
          LIMIT 1
        ),
        active_score AS (
          SELECT *
          FROM ml_score_runs
          WHERE model_run_id = (SELECT active_model_run_id FROM active_registry)
          ORDER BY id DESC
          LIMIT 1
        ),
        candidate_score AS (
          SELECT *
          FROM ml_score_runs
          WHERE model_run_id = (SELECT id FROM candidate_model)
          ORDER BY id DESC
          LIMIT 1
        )
        SELECT
          'active' AS model_role,
          id AS score_run_id,
          model_run_id,
          scored_count,
          final_auto_safe_count AS auto_safe,
          needs_human_count AS needs_human,
          needs_autofix_count AS needs_autofix,
          blocked_structure_count AS blocked_structure,
          deterministic_block_count AS deterministic_blocks,
          ROUND(100.0 * final_auto_safe_count / NULLIF(scored_count, 0), 2) AS auto_safe_pct
        FROM active_score
        UNION ALL
        SELECT
          'candidate' AS model_role,
          id AS score_run_id,
          model_run_id,
          scored_count,
          final_auto_safe_count AS auto_safe,
          needs_human_count AS needs_human,
          needs_autofix_count AS needs_autofix,
          blocked_structure_count AS blocked_structure,
          deterministic_block_count AS deterministic_blocks,
          ROUND(100.0 * final_auto_safe_count / NULLIF(scored_count, 0), 2) AS auto_safe_pct
        FROM candidate_score
        """,
    )
    role_rows = {row["model_role"]: row for row in bars_rows}
    action_comparison = []
    for key, label in [
        ("auto_safe", "Auto-safe"),
        ("needs_human", "Human"),
        ("needs_autofix", "Autofix"),
        ("blocked_structure", "Blocked"),
    ]:
        action_comparison.append(
            {
                "action": label,
                "active": _int(role_rows.get("active", {}).get(key)),
                "candidate": _int(role_rows.get("candidate", {}).get(key)),
            }
        )

    recent_models = _all(
        con,
        """
        WITH latest_score_by_model AS (
          SELECT model_run_id, MAX(id) AS latest_score_run_id
          FROM ml_score_runs
          GROUP BY model_run_id
        ),
        latest_promotion_by_model AS (
          SELECT candidate_model_run_id, MAX(id) AS latest_promotion_id
          FROM ml_model_promotions
          GROUP BY candidate_model_run_id
        )
        SELECT
          m.id AS model_run_id,
          m.model_version,
          m.dataset_run_id,
          m.safe_threshold,
          m.accuracy,
          m.macro_f1,
          m.false_safe_count,
          m.false_safe_rate,
          m.safe_precision,
          m.safe_recall,
          s.id AS latest_score_run_id,
          s.scored_count,
          s.final_auto_safe_count,
          ROUND(100.0 * s.final_auto_safe_count / NULLIF(s.scored_count, 0), 2) AS operational_auto_safe_pct,
          p.decision AS latest_promotion_decision,
          p.reason AS latest_promotion_reason,
          m.started_at,
          m.finished_at
        FROM ml_model_runs m
        LEFT JOIN latest_score_by_model lsm ON lsm.model_run_id = m.id
        LEFT JOIN ml_score_runs s ON s.id = lsm.latest_score_run_id
        LEFT JOIN latest_promotion_by_model lpm ON lpm.candidate_model_run_id = m.id
        LEFT JOIN ml_model_promotions p ON p.id = lpm.latest_promotion_id
        WHERE m.model_kind = 'risk_action_classifier'
        ORDER BY m.id DESC
        LIMIT 30
        """,
    )
    recent_models.reverse()

    candidate_distribution = _all(
        con,
        """
        WITH candidate_model AS (
          SELECT id
          FROM ml_model_runs
          WHERE model_kind = 'risk_action_classifier'
          ORDER BY id DESC
          LIMIT 1
        ),
        candidate_score AS (
          SELECT id
          FROM ml_score_runs
          WHERE model_run_id = (SELECT id FROM candidate_model)
          ORDER BY id DESC
          LIMIT 1
        )
        SELECT
          final_action,
          risk_class,
          COUNT(*) AS total,
          ROUND(AVG(model_safe_probability), 4) AS avg_safe_probability,
          SUM(CASE WHEN token_status <> 'ok' THEN 1 ELSE 0 END) AS token_mismatch_count,
          SUM(CASE WHEN deterministic_blocked = 1 THEN 1 ELSE 0 END) AS deterministic_blocked_count
        FROM ml_score_items
        WHERE run_id = (SELECT id FROM candidate_score)
        GROUP BY final_action, risk_class
        ORDER BY total DESC
        """,
    )

    file_regressions = _all(
        con,
        """
        WITH active_registry AS (
          SELECT active_model_run_id
          FROM ml_model_registry
          WHERE model_kind = 'risk_action_classifier'
          LIMIT 1
        ),
        candidate_model AS (
          SELECT id
          FROM ml_model_runs
          WHERE model_kind = 'risk_action_classifier'
          ORDER BY id DESC
          LIMIT 1
        ),
        active_score AS (
          SELECT id
          FROM ml_score_runs
          WHERE model_run_id = (SELECT active_model_run_id FROM active_registry)
          ORDER BY id DESC
          LIMIT 1
        ),
        candidate_score AS (
          SELECT id
          FROM ml_score_runs
          WHERE model_run_id = (SELECT id FROM candidate_model)
          ORDER BY id DESC
          LIMIT 1
        ),
        joined AS (
          SELECT
            a.segment_id,
            a.relative_path,
            a.source_key,
            a.final_action AS active_action,
            c.final_action AS candidate_action,
            c.model_safe_probability AS candidate_safe_probability
          FROM ml_score_items a
          JOIN ml_score_items c ON c.segment_id = a.segment_id
          WHERE a.run_id = (SELECT id FROM active_score)
            AND c.run_id = (SELECT id FROM candidate_score)
        )
        SELECT
          relative_path,
          COUNT(*) AS compared_segments,
          SUM(CASE WHEN active_action = 'auto_safe' AND candidate_action <> 'auto_safe' THEN 1 ELSE 0 END) AS operational_regressions,
          SUM(CASE WHEN active_action <> 'auto_safe' AND candidate_action = 'auto_safe' THEN 1 ELSE 0 END) AS candidate_recoveries,
          SUM(CASE WHEN active_action = 'auto_safe' AND candidate_action = 'auto_safe' THEN 1 ELSE 0 END) AS both_auto_safe,
          SUM(CASE WHEN active_action <> 'auto_safe' AND candidate_action <> 'auto_safe' THEN 1 ELSE 0 END) AS both_not_safe,
          ROUND(AVG(candidate_safe_probability), 4) AS avg_candidate_safe_probability
        FROM joined
        GROUP BY relative_path
        ORDER BY operational_regressions DESC, candidate_recoveries DESC
        LIMIT 50
        """,
    )

    regression_audit = _all(
        con,
        """
        WITH active_registry AS (
          SELECT active_model_run_id
          FROM ml_model_registry
          WHERE model_kind = 'risk_action_classifier'
          LIMIT 1
        ),
        candidate_model AS (
          SELECT id
          FROM ml_model_runs
          WHERE model_kind = 'risk_action_classifier'
          ORDER BY id DESC
          LIMIT 1
        ),
        active_score AS (
          SELECT id
          FROM ml_score_runs
          WHERE model_run_id = (SELECT active_model_run_id FROM active_registry)
          ORDER BY id DESC
          LIMIT 1
        ),
        candidate_score AS (
          SELECT id
          FROM ml_score_runs
          WHERE model_run_id = (SELECT id FROM candidate_model)
          ORDER BY id DESC
          LIMIT 1
        )
        SELECT
          c.segment_id,
          c.relative_path,
          c.source_key,
          a.final_action AS active_action,
          c.final_action AS candidate_action,
          c.model_safe_probability AS candidate_safe_probability,
          c.risk_class AS candidate_risk_class,
          c.token_status,
          c.issue_count,
          c.deterministic_blocked,
          c.candidate_text,
          c.reasons_json
        FROM ml_score_items a
        JOIN ml_score_items c ON c.segment_id = a.segment_id
        WHERE a.run_id = (SELECT id FROM active_score)
          AND c.run_id = (SELECT id FROM candidate_score)
          AND a.final_action = 'auto_safe'
          AND c.final_action <> 'auto_safe'
        ORDER BY c.model_safe_probability DESC, c.relative_path, c.source_key
        LIMIT 500
        """,
    )

    return {
        "available": True,
        "summary": summary,
        "actionComparison": action_comparison,
        "recentModels": recent_models,
        "candidateDistribution": candidate_distribution,
        "fileRegressions": file_regressions,
        "regressionAudit": regression_audit,
        "gaps": {
            "auto_safe_gap_count": _int(summary.get("active_auto_safe_count")) - _int(summary.get("candidate_auto_safe_count")),
            "auto_safe_gap_pct_points": round(_num(summary.get("active_auto_safe_pct")) - _num(summary.get("candidate_auto_safe_pct")), 2),
        },
    }


def _specialists_payload(con: sqlite3.Connection) -> dict[str, Any]:
    overview = _all(
        con,
        """
        WITH ranked AS (
          SELECT
            m.*,
            ROW_NUMBER() OVER (PARTITION BY m.model_kind ORDER BY m.id DESC) AS rn
          FROM ml_model_runs m
          WHERE m.model_kind LIKE 'specialist_%'
            AND m.finished_at IS NOT NULL
        ),
        latest_score AS (
          SELECT model_run_id, MAX(id) AS score_run_id
          FROM ml_score_runs
          WHERE finished_at IS NOT NULL
          GROUP BY model_run_id
        )
        SELECT
          r.model_kind,
          r.model_version,
          r.id AS model_run_id,
          r.dataset_run_id,
          r.training_examples,
          r.test_examples,
          r.safe_threshold,
          r.accuracy,
          r.macro_f1,
          r.safe_precision,
          r.safe_recall,
          r.false_safe_count,
          s.score_run_id,
          sr.scored_count,
          sr.final_auto_safe_count,
          ROUND(100.0 * sr.final_auto_safe_count / NULLIF(sr.scored_count, 0), 2) AS auto_safe_pct,
          r.finished_at,
          d.positive_count AS positive_examples,
          d.negative_count AS negative_examples
        FROM ranked r
        LEFT JOIN latest_score s ON s.model_run_id = r.id
        LEFT JOIN ml_score_runs sr ON sr.id = s.score_run_id
        LEFT JOIN ml_dataset_runs d ON d.id = r.dataset_run_id
        WHERE r.rn = 1
        ORDER BY r.model_kind
        """,
    )

    auditor_by_specialist = _all(
        con,
        """
        WITH general_score AS (
          SELECT r.id AS general_score_run_id
          FROM ml_score_runs r
          JOIN ml_model_runs m ON m.id = r.model_run_id
          WHERE m.model_kind = 'risk_action_classifier'
            AND r.finished_at IS NOT NULL
          ORDER BY r.id DESC
          LIMIT 1
        ),
        latest_specialist_score AS (
          SELECT
            m.model_kind,
            MAX(r.id) AS specialist_score_run_id
          FROM ml_score_runs r
          JOIN ml_model_runs m ON m.id = r.model_run_id
          WHERE m.model_kind LIKE 'specialist_%'
            AND r.finished_at IS NOT NULL
          GROUP BY m.model_kind
        ),
        joined AS (
          SELECT
            lss.model_kind,
            g.run_id AS general_score_run_id,
            s.run_id AS specialist_score_run_id,
            s.segment_id,
            s.relative_path,
            s.source_key,
            g.final_action AS general_action,
            s.final_action AS specialist_action,
            g.model_safe_probability AS general_probability,
            s.model_safe_probability AS specialist_probability
          FROM latest_specialist_score lss
          JOIN ml_score_items s ON s.run_id = lss.specialist_score_run_id
          JOIN ml_score_items g
            ON g.segment_id = s.segment_id
           AND g.run_id = (SELECT general_score_run_id FROM general_score)
        )
        SELECT
          model_kind,
          general_score_run_id,
          specialist_score_run_id,
          COUNT(*) AS compared,
          SUM(CASE WHEN general_action = 'auto_safe' AND specialist_action = 'auto_safe' THEN 1 ELSE 0 END) AS auto_safe_agree,
          SUM(CASE WHEN general_action <> 'auto_safe' AND specialist_action <> 'auto_safe' THEN 1 ELSE 0 END) AS needs_human_agree,
          SUM(CASE WHEN general_action <> 'auto_safe' AND specialist_action = 'auto_safe' THEN 1 ELSE 0 END) AS specialist_new_safe_review,
          SUM(CASE WHEN general_action = 'auto_safe' AND specialist_action <> 'auto_safe' THEN 1 ELSE 0 END) AS specialist_demoted_review,
          ROUND(
            100.0 * SUM(CASE WHEN general_action <> specialist_action THEN 1 ELSE 0 END)
            / NULLIF(COUNT(*), 0),
            2
          ) AS review_required_pct
        FROM joined
        GROUP BY model_kind, general_score_run_id, specialist_score_run_id
        ORDER BY specialist_new_safe_review DESC, specialist_demoted_review DESC
        """,
    )

    selected_kind = "specialist_title_names"
    if not any(row.get("model_kind") == selected_kind for row in auditor_by_specialist) and auditor_by_specialist:
        selected_kind = auditor_by_specialist[0]["model_kind"]

    auditor_queue = []
    if selected_kind:
        auditor_queue = _all(
            con,
            """
            WITH general_score AS (
              SELECT r.id AS general_score_run_id
              FROM ml_score_runs r
              JOIN ml_model_runs m ON m.id = r.model_run_id
              WHERE m.model_kind = 'risk_action_classifier'
                AND r.finished_at IS NOT NULL
              ORDER BY r.id DESC
              LIMIT 1
            ),
            specialist_score AS (
              SELECT r.id AS specialist_score_run_id
              FROM ml_score_runs r
              JOIN ml_model_runs m ON m.id = r.model_run_id
              WHERE m.model_kind = ?
                AND r.finished_at IS NOT NULL
              ORDER BY r.id DESC
              LIMIT 1
            )
            SELECT
              s.segment_id,
              s.relative_path,
              s.source_key,
              s.candidate_text,
              g.final_action AS general_action,
              s.final_action AS specialist_action,
              CASE
                WHEN g.final_action <> 'auto_safe' AND s.final_action = 'auto_safe' THEN 'specialist_new_safe_review'
                WHEN g.final_action = 'auto_safe' AND s.final_action <> 'auto_safe' THEN 'specialist_demoted_review'
                ELSE 'agree'
              END AS auditor_action,
              g.model_safe_probability AS general_probability,
              s.model_safe_probability AS specialist_probability,
              s.token_status,
              s.issue_count,
              s.reasons_json
            FROM ml_score_items s
            JOIN ml_score_items g
              ON g.segment_id = s.segment_id
             AND g.run_id = (SELECT general_score_run_id FROM general_score)
            WHERE s.run_id = (SELECT specialist_score_run_id FROM specialist_score)
              AND (
                (g.final_action <> 'auto_safe' AND s.final_action = 'auto_safe')
                OR
                (g.final_action = 'auto_safe' AND s.final_action <> 'auto_safe')
              )
            ORDER BY
              CASE WHEN g.final_action = 'auto_safe' AND s.final_action <> 'auto_safe' THEN 0 ELSE 1 END,
              s.model_safe_probability DESC,
              s.segment_id
            LIMIT 300
            """,
            (selected_kind,),
        )

    learning_rows = _all(
        con,
        """
        SELECT
          focus_group,
          human_label,
          COUNT(*) AS total,
          SUM(CASE WHEN corrected_text IS NOT NULL AND TRIM(corrected_text) <> '' THEN 1 ELSE 0 END) AS corrected
        FROM local_learning_candidates
        WHERE queue_source = 'ml_specialist_auditor'
          AND human_label <> 'pending'
        GROUP BY focus_group, human_label
        ORDER BY focus_group, human_label
        """,
    )
    learning_by_label: dict[str, dict[str, Any]] = {}
    learning_by_focus: dict[str, dict[str, Any]] = {}
    for row in learning_rows:
        label = row["human_label"]
        focus = row["focus_group"] or "unknown"
        learning_by_label.setdefault(label, {"human_label": label, "total": 0, "corrected": 0})
        learning_by_label[label]["total"] += _int(row.get("total"))
        learning_by_label[label]["corrected"] += _int(row.get("corrected"))
        learning_by_focus.setdefault(focus, {"focus_group": focus, "total": 0, "corrected": 0})
        learning_by_focus[focus]["total"] += _int(row.get("total"))
        learning_by_focus[focus]["corrected"] += _int(row.get("corrected"))

    title_names_evolution = _all(
        con,
        """
        WITH scores AS (
          SELECT model_run_id, MAX(id) AS score_run_id
          FROM ml_score_runs
          WHERE finished_at IS NOT NULL
          GROUP BY model_run_id
        )
        SELECT
          m.id AS model_run_id,
          m.model_version,
          m.dataset_run_id,
          m.safe_threshold,
          m.macro_f1,
          m.safe_precision,
          m.safe_recall,
          m.false_safe_count,
          s.score_run_id,
          sr.scored_count,
          sr.final_auto_safe_count,
          ROUND(100.0 * sr.final_auto_safe_count / NULLIF(sr.scored_count, 0), 2) AS auto_safe_pct,
          m.finished_at
        FROM ml_model_runs m
        LEFT JOIN scores s ON s.model_run_id = m.id
        LEFT JOIN ml_score_runs sr ON sr.id = s.score_run_id
        WHERE m.model_kind = 'specialist_title_names'
          AND m.finished_at IS NOT NULL
        ORDER BY m.id ASC
        """,
    )

    selected_overview = next((row for row in overview if row.get("model_kind") == selected_kind), overview[0] if overview else {})
    selected_auditor = next((row for row in auditor_by_specialist if row.get("model_kind") == selected_kind), {})
    reviewed_total = sum(_int(row.get("total")) for row in learning_rows)
    correct_total = sum(_int(row.get("total")) for row in learning_rows if row.get("human_label") == "correct")
    minor_fix_total = sum(_int(row.get("total")) for row in learning_rows if row.get("human_label") == "minor_fix")
    semantic_error_total = sum(_int(row.get("total")) for row in learning_rows if row.get("human_label") == "semantic_error")
    corrected_total = sum(_int(row.get("corrected")) for row in learning_rows)

    return {
        "summary": {
            "specialists_total": len(overview),
            "specialists_with_score": sum(1 for row in overview if row.get("score_run_id") is not None),
            "specialists_active": 0,
            "specialist_false_safe": sum(_int(row.get("false_safe_count")) for row in overview),
            "selected_model_kind": selected_kind,
            "selected_auto_safe_count": _int(selected_overview.get("final_auto_safe_count")),
            "selected_auto_safe_pct": _num(selected_overview.get("auto_safe_pct")),
            "selected_score_run_id": selected_overview.get("score_run_id"),
            "auditor_review_required": _int(selected_auditor.get("specialist_new_safe_review")) + _int(selected_auditor.get("specialist_demoted_review")),
            "auditor_review_required_pct": _num(selected_auditor.get("review_required_pct")),
            "human_reviewed_total": reviewed_total,
        },
        "overview": overview,
        "coverageBySpecialist": overview,
        "auditorSummary": {
            "general_score_run_id": selected_auditor.get("general_score_run_id"),
            "specialist_score_run_id": selected_auditor.get("specialist_score_run_id"),
            "compared": _int(selected_auditor.get("compared")),
            "auto_safe_agree": _int(selected_auditor.get("auto_safe_agree")),
            "needs_human_agree": _int(selected_auditor.get("needs_human_agree")),
            "specialist_new_safe_review": _int(selected_auditor.get("specialist_new_safe_review")),
            "specialist_demoted_review": _int(selected_auditor.get("specialist_demoted_review")),
            "review_required_pct": _num(selected_auditor.get("review_required_pct")),
        },
        "auditorBySpecialist": auditor_by_specialist,
        "auditorQueue": auditor_queue,
        "learningSummary": {
            "reviewed_total": reviewed_total,
            "correct": correct_total,
            "minor_fix": minor_fix_total,
            "semantic_error": semantic_error_total,
            "acceptance_rate": round(_pct(correct_total + minor_fix_total, reviewed_total), 2),
            "corrected_text_total": corrected_total,
        },
        "learningByLabel": list(learning_by_label.values()),
        "learningByFocus": list(learning_by_focus.values()),
        "titleNamesEvolution": title_names_evolution,
        "specialists": overview,
        "groupComparison": auditor_by_specialist,
        "divergenceMatrix": [],
        "auditQueue": auditor_queue,
        "evolution": title_names_evolution,
    }


def _agents_payload(con: sqlite3.Connection) -> dict[str, Any]:
    required = [
        "ml_agent_registry",
        "ml_agent_routing_runs",
        "ml_agent_recommendations",
    ]
    if not all(_table_exists(con, table) for table in required):
        return {
            "available": False,
            "summary": {
                "agents_total": 0,
                "agents_operational": 0,
                "experimental_subagents": 0,
                "planned_subagents": 0,
                "latest_false_safe": 0,
                "ensemble_net_gain": 0,
                "recommendation_evidence": 0,
            },
            "topologyNodes": [],
            "topologyEdges": [],
            "registry": [],
            "health": [],
            "recommendations": [],
            "routingRuns": [],
            "routedItemsByAgent": [],
            "routingSamples": [],
            "ensembleImpact": [],
            "promotionReadiness": {},
            "experimentalContribution": [],
            "learningByAgent": [],
            "agentTimeline": [],
        }

    has_snapshots = _table_exists(con, "ml_specialist_policy_snapshots")
    summary = _one(
        con,
        """
        WITH latest_policy AS (
          SELECT *
          FROM ml_policy_runs
          WHERE finished_at IS NOT NULL
          ORDER BY id DESC
          LIMIT 1
        ),
        latest_reco_run AS (
          SELECT MAX(run_id) AS run_id
          FROM ml_agent_recommendations
        ),
        latest_agent_model AS (
          SELECT
            a.agent_key,
            a.status,
            a.operational_state,
            a.agent_type,
            COALESCE(m.false_safe_count, 0) AS false_safe_count
          FROM ml_agent_registry a
          LEFT JOIN ml_model_runs m
            ON m.model_kind = a.model_kind
           AND m.finished_at IS NOT NULL
           AND m.id = (
             SELECT MAX(m2.id)
             FROM ml_model_runs m2
             WHERE m2.model_kind = a.model_kind
               AND m2.finished_at IS NOT NULL
           )
        )
        SELECT
          (SELECT COUNT(*) FROM ml_agent_registry) AS agents_total,
          (
            SELECT COUNT(*)
            FROM ml_agent_registry
            WHERE status = 'active'
              AND operational_state IN ('authoritative', 'operational', 'dry_run')
          ) AS agents_operational,
          (
            SELECT COUNT(*)
            FROM ml_agent_registry
            WHERE status = 'active'
              AND operational_state = 'experimental'
              AND agent_type = 'subspecialist'
          ) AS experimental_subagents,
          (
            SELECT COUNT(*)
            FROM ml_agent_registry
            WHERE status = 'planned'
          ) AS planned_subagents,
          COALESCE((SELECT SUM(false_safe_count) FROM latest_agent_model), 0) AS latest_false_safe,
          COALESCE((
            SELECT SUM(false_safe_count)
            FROM latest_agent_model
            WHERE status = 'active'
              AND operational_state IN ('authoritative', 'operational', 'dry_run')
          ), 0) AS operational_false_safe,
          COALESCE((
            SELECT SUM(false_safe_count)
            FROM latest_agent_model
            WHERE status = 'active'
              AND operational_state = 'experimental'
          ), 0) AS experimental_false_safe,
          COALESCE((
            SELECT SUM(false_safe_count)
            FROM latest_agent_model
            WHERE status = 'planned'
          ), 0) AS planned_false_safe,
          COALESCE((SELECT policy_auto_safe_count - active_auto_safe_count FROM latest_policy), 0) AS ensemble_net_gain,
          COALESCE((
            SELECT SUM(evidence_count)
            FROM ml_agent_recommendations
            WHERE run_id = (SELECT run_id FROM latest_reco_run)
          ), 0) AS recommendation_evidence
        """,
    )

    active_gate = {}
    if _table_exists(con, "ml_composite_gate_registry"):
        active_gate = _one(
            con,
            """
            SELECT
              g.gate_key,
              g.coordinator_key,
              g.active_checkpoint_id,
              g.active_guarded_checkpoint_id,
              g.active_overlay_run_id,
              g.active_policy_run_id,
              g.operational_state,
              g.active_promotion_kind,
              g.auto_apply_allowed,
              g.promoted_at,
              g.promoted_by,
              c.promotion_status AS guarded_checkpoint_status,
              c.recommended_action AS guarded_recommended_action,
              c.ready_rule_count AS guarded_ready_rule_count,
              c.guarded_release_count,
              c.invalid_release_count,
              c.apply_allowed_count AS guarded_apply_allowed_count,
              c.blockers_json AS guarded_blockers_json,
              c.warnings_json AS guarded_warnings_json
            FROM ml_composite_gate_registry g
            LEFT JOIN ml_composite_guarded_overlay_checkpoints c
              ON c.id = g.active_guarded_checkpoint_id
            WHERE g.gate_key = 'segment_token_composite_review_gate'
            LIMIT 1
            """,
        )

    snapshot_cte = (
        """
        latest_snapshot AS (
          SELECT
            s.*,
            ROW_NUMBER() OVER (PARTITION BY s.specialist_key ORDER BY s.id DESC) AS rn
          FROM ml_specialist_policy_snapshots s
        ),
        """
        if has_snapshots
        else """
        latest_snapshot AS (
          SELECT
            NULL AS specialist_key,
            NULL AS status,
            NULL AS pending_real_count,
            NULL AS specialist_new_safe_count,
            NULL AS specialist_demoted_count,
            NULL AS rn
          WHERE 0
        ),
        """
    )
    registry = _all(
        con,
        f"""
        WITH latest_model AS (
          SELECT
            m.*,
            ROW_NUMBER() OVER (PARTITION BY m.model_kind ORDER BY m.id DESC) AS rn
          FROM ml_model_runs m
          WHERE m.finished_at IS NOT NULL
        ),
        latest_score AS (
          SELECT
            r.*,
            ROW_NUMBER() OVER (PARTITION BY r.model_run_id ORDER BY r.id DESC) AS rn
          FROM ml_score_runs r
          WHERE r.finished_at IS NOT NULL
        ),
        {snapshot_cte}
        base AS (
          SELECT
            a.agent_key,
            a.agent_type,
            a.parent_agent_key,
            a.status,
            a.operational_state,
            a.decision_role,
            a.model_kind,
            a.scope_group,
            a.scope_description,
            a.default_threshold,
            a.priority,
            a.dashboard_group,
            lm.id AS model_run_id,
            lm.dataset_run_id,
            lm.model_version,
            lm.safe_threshold,
            lm.macro_f1,
            lm.safe_precision,
            lm.safe_recall,
            lm.false_safe_count,
            ls.id AS score_run_id,
            ls.scored_count,
            ls.final_auto_safe_count,
            ROUND(100.0 * ls.final_auto_safe_count / NULLIF(ls.scored_count, 0), 2) AS auto_safe_pct,
            snap.status AS policy_status,
            snap.pending_real_count,
            snap.specialist_new_safe_count,
            snap.specialist_demoted_count
          FROM ml_agent_registry a
          LEFT JOIN latest_model lm ON lm.model_kind = a.model_kind AND lm.rn = 1
          LEFT JOIN latest_score ls ON ls.model_run_id = lm.id AND ls.rn = 1
          LEFT JOIN latest_snapshot snap ON snap.specialist_key = a.agent_key AND snap.rn = 1
        )
        SELECT *
        FROM base
        ORDER BY priority, dashboard_group, agent_key
        """,
    )

    for row in registry:
        state = row.get("operational_state")
        false_safe = _int(row.get("false_safe_count"))
        if state in {"authoritative", "operational", "dry_run"}:
            row["decision_authority"] = "decision_authorized"
        elif state == "experimental":
            row["decision_authority"] = "evidence_only"
        elif row.get("status") == "planned":
            row["decision_authority"] = "planned_only"
        else:
            row["decision_authority"] = "unknown"

        if row.get("model_kind") and not row.get("model_run_id"):
            row["health_status"] = "no_model_yet"
        elif false_safe > 0 and state == "experimental":
            row["health_status"] = "experimental_false_safe_watch"
        elif false_safe > 0:
            row["health_status"] = "blocked_false_safe"
        elif state == "experimental":
            row["health_status"] = "experimental_watch"
        else:
            row["health_status"] = "healthy"

    topology_nodes = [
        {
            "id": row.get("agent_key"),
            "parent": row.get("parent_agent_key"),
            "agent_type": row.get("agent_type"),
            "status": row.get("status"),
            "operational_state": row.get("operational_state"),
            "decision_role": row.get("decision_role"),
            "dashboard_group": row.get("dashboard_group"),
            "scope_group": row.get("scope_group"),
            "model_kind": row.get("model_kind"),
            "model_run_id": row.get("model_run_id"),
            "score_run_id": row.get("score_run_id"),
            "false_safe_count": _int(row.get("false_safe_count")),
            "auto_safe_pct": _num(row.get("auto_safe_pct")),
            "decision_authority": row.get("decision_authority"),
            "health_status": row.get("health_status"),
        }
        for row in registry
    ]
    topology_edges = [
        {"source": row.get("parent_agent_key"), "target": row.get("agent_key")}
        for row in registry
        if row.get("parent_agent_key")
    ]

    latest_reco_run = _one(con, "SELECT MAX(run_id) AS run_id FROM ml_agent_recommendations")
    recommendations = _all(
        con,
        """
        WITH latest_run AS (
          SELECT MAX(run_id) AS run_id
          FROM ml_agent_recommendations
        )
        SELECT
          proposed_agent_key,
          parent_agent_key,
          recommendation_type,
          status,
          reason,
          evidence_count,
          positive_count,
          negative_count,
          corrected_count,
          sample_segments_json,
          created_at
        FROM ml_agent_recommendations
        WHERE run_id = (SELECT run_id FROM latest_run)
        ORDER BY
          CASE WHEN negative_count > 0 THEN 0 ELSE 1 END,
          corrected_count DESC,
          evidence_count DESC,
          proposed_agent_key
        """,
    )
    for row in recommendations:
        try:
            samples = json.loads(row.get("sample_segments_json") or "[]")
        except json.JSONDecodeError:
            samples = []
        row["sample_count"] = len(samples) if isinstance(samples, list) else 0
        row["sample_preview"] = samples[:3] if isinstance(samples, list) else []

    ensemble_impact = []
    if _table_exists(con, "ml_policy_runs") and _table_exists(con, "ml_policy_items"):
        ensemble_impact = _all(
            con,
            """
            WITH latest_policy AS (
              SELECT id
              FROM ml_policy_runs
              WHERE finished_at IS NOT NULL
              ORDER BY id DESC
              LIMIT 1
            )
            SELECT
              policy_group,
              COUNT(*) AS rows_count,
              SUM(CASE WHEN policy_action = 'auto_safe' THEN 1 ELSE 0 END) AS policy_auto_safe,
              SUM(new_safe) AS new_safe,
              SUM(demoted_safe) AS demoted_safe,
              SUM(CASE WHEN learned_positive = 1 THEN 1 ELSE 0 END) AS learned_positive,
              SUM(CASE WHEN learned_negative = 1 THEN 1 ELSE 0 END) AS learned_negative
            FROM ml_policy_items
            WHERE run_id = (SELECT id FROM latest_policy)
            GROUP BY policy_group
            ORDER BY new_safe DESC, rows_count DESC
            LIMIT 20
            """,
        )

    routing_runs = _all(
        con,
        """
        SELECT
          id,
          rule_version,
          general_score_run_id,
          policy_run_id,
          coordinator_key,
          agents_considered_count,
          segments_scanned_count,
          routed_count,
          active_agent_covered_count,
          planned_agent_covered_count,
          recommendation_count,
          started_at,
          finished_at
        FROM ml_agent_routing_runs
        ORDER BY id ASC
        """,
    )

    promotion_readiness = _one(
        con,
        """
        WITH active_model AS (
          SELECT *
          FROM ml_model_registry
          WHERE model_kind = 'risk_action_classifier'
        ),
        active_score AS (
          SELECT sr.*
          FROM ml_score_runs sr
          JOIN active_model am ON am.active_model_run_id = sr.model_run_id
          WHERE sr.finished_at IS NOT NULL
          ORDER BY sr.id DESC
          LIMIT 1
        ),
        candidate_macro AS (
          SELECT m.*
          FROM ml_model_runs m
          WHERE m.model_kind = 'risk_action_classifier'
            AND m.finished_at IS NOT NULL
          ORDER BY m.id DESC
          LIMIT 1
        ),
        candidate_score AS (
          SELECT sr.*
          FROM ml_score_runs sr
          JOIN candidate_macro cm ON cm.id = sr.model_run_id
          WHERE sr.finished_at IS NOT NULL
          ORDER BY sr.id DESC
          LIMIT 1
        ),
        latest_ensemble AS (
          SELECT *
          FROM ml_policy_runs
          WHERE rule_version = 'ml_specialist_ensemble_policy_v1'
            AND finished_at IS NOT NULL
          ORDER BY id DESC
          LIMIT 1
        ),
        agent_summary AS (
          SELECT
            SUM(CASE WHEN status = 'active' AND operational_state = 'experimental' THEN 1 ELSE 0 END) AS experimental_agents,
            SUM(CASE WHEN status = 'active' AND operational_state IN ('operational', 'authoritative', 'dry_run') THEN 1 ELSE 0 END) AS operational_agents
          FROM ml_agent_registry
        )
        SELECT
          am.active_model_run_id,
          am.active_model_version,
          active_score.id AS active_score_run_id,
          active_score.scored_count AS active_scored_count,
          active_score.final_auto_safe_count AS active_auto_safe_count,
          ROUND(100.0 * active_score.final_auto_safe_count / NULLIF(active_score.scored_count, 0), 2) AS active_auto_safe_pct,
          cm.id AS candidate_model_run_id,
          cm.model_version AS candidate_model_version,
          cm.macro_f1 AS candidate_macro_f1,
          cm.false_safe_count AS candidate_false_safe_count,
          cm.safe_precision AS candidate_safe_precision,
          cm.safe_recall AS candidate_safe_recall,
          candidate_score.id AS candidate_score_run_id,
          candidate_score.final_auto_safe_count AS candidate_auto_safe_count,
          latest_ensemble.id AS ensemble_policy_run_id,
          latest_ensemble.policy_auto_safe_count AS ensemble_auto_safe_count,
          latest_ensemble.new_safe_count AS ensemble_new_safe_count,
          latest_ensemble.demoted_safe_count AS ensemble_demoted_safe_count,
          latest_ensemble.policy_auto_safe_count - latest_ensemble.active_auto_safe_count AS ensemble_gain_vs_candidate_macro,
          latest_ensemble.policy_auto_safe_count - active_score.final_auto_safe_count AS ensemble_gap_vs_active,
          ROUND(
            100.0 * (latest_ensemble.policy_auto_safe_count - active_score.final_auto_safe_count)
            / NULLIF(active_score.scored_count, 0),
            2
          ) AS ensemble_gap_vs_active_pct_points,
          agent_summary.operational_agents,
          agent_summary.experimental_agents,
          CASE
            WHEN COALESCE(cm.false_safe_count, 0) > 0 THEN 'not_ready_false_safe'
            WHEN latest_ensemble.policy_auto_safe_count < active_score.final_auto_safe_count THEN 'not_ready_coverage_gap'
            WHEN agent_summary.experimental_agents > 0 THEN 'watch_experimental_agents'
            ELSE 'ready_for_review'
          END AS promotion_readiness
        FROM active_model am
        LEFT JOIN active_score ON 1 = 1
        LEFT JOIN candidate_macro cm ON 1 = 1
        LEFT JOIN candidate_score ON 1 = 1
        LEFT JOIN latest_ensemble ON 1 = 1
        LEFT JOIN agent_summary ON 1 = 1
        """,
    )

    routed_items_by_agent = []
    routing_samples = []
    experimental_contribution = []
    if _table_exists(con, "ml_agent_routing_items"):
        routed_items_by_agent = _all(
            con,
            """
            WITH latest_run AS (
              SELECT MAX(id) AS run_id
              FROM ml_agent_routing_runs
            )
            SELECT
              route_agent_key,
              route_agent_type,
              route_status,
              COUNT(*) AS rows_count,
              SUM(CASE WHEN general_action = 'auto_safe' THEN 1 ELSE 0 END) AS general_auto_safe,
              SUM(CASE WHEN policy_action = 'auto_safe' THEN 1 ELSE 0 END) AS policy_auto_safe,
              SUM(CASE WHEN specialist_action = 'auto_safe' THEN 1 ELSE 0 END) AS specialist_auto_safe,
              SUM(CASE WHEN recommendation_key IS NOT NULL THEN 1 ELSE 0 END) AS recommendation_rows
            FROM ml_agent_routing_items
            WHERE run_id = (SELECT run_id FROM latest_run)
            GROUP BY route_agent_key, route_agent_type, route_status
            ORDER BY route_status, route_agent_key
            """,
        )
        experimental_contribution = _all(
            con,
            """
            WITH latest_run AS (
              SELECT MAX(id) AS run_id
              FROM ml_agent_routing_runs
            )
            SELECT
              route_agent_key AS agent_key,
              route_status,
              COUNT(*) AS sampled_rows,
              SUM(CASE WHEN specialist_action = 'auto_safe' THEN 1 ELSE 0 END) AS specialist_auto_safe,
              SUM(CASE WHEN general_action = 'auto_safe' THEN 1 ELSE 0 END) AS general_auto_safe,
              SUM(CASE WHEN specialist_action = 'auto_safe' AND COALESCE(general_action, '') <> 'auto_safe' THEN 1 ELSE 0 END) AS potential_new_safe,
              SUM(CASE WHEN COALESCE(specialist_action, '') <> 'auto_safe' AND general_action = 'auto_safe' THEN 1 ELSE 0 END) AS potential_demotions,
              SUM(CASE WHEN recommendation_key IS NOT NULL THEN 1 ELSE 0 END) AS recommendation_rows
            FROM ml_agent_routing_items
            WHERE run_id = (SELECT run_id FROM latest_run)
              AND route_status = 'experimental'
            GROUP BY route_agent_key, route_status
            ORDER BY potential_new_safe DESC, sampled_rows DESC
            """,
        )
        routing_samples = _all(
            con,
            """
            WITH latest_run AS (
              SELECT MAX(id) AS run_id
              FROM ml_agent_routing_runs
            )
            SELECT
              segment_id,
              relative_path,
              source_key,
              route_agent_key,
              route_status,
              ROUND(route_confidence, 4) AS route_confidence,
              general_action,
              policy_action,
              specialist_action,
              recommendation_key
            FROM ml_agent_routing_items
            WHERE run_id = (SELECT run_id FROM latest_run)
            ORDER BY route_status, route_agent_key, relative_path, source_key
            LIMIT 200
            """,
        )

    learning_by_agent = _all(
        con,
        """
        SELECT
          focus_group AS agent_key,
          queue_source,
          COUNT(*) AS reviewed_count,
          SUM(CASE WHEN human_label IN ('correct', 'contextual_exception') THEN 1 ELSE 0 END) AS positive_count,
          SUM(CASE WHEN human_label IN ('minor_fix', 'major_fix', 'semantic_error', 'residual_spanish', 'structure_error', 'token_mismatch', 'rejected', 'rejected_suggestion') THEN 1 ELSE 0 END) AS negative_or_fix_count,
          SUM(CASE WHEN corrected_text IS NOT NULL AND TRIM(corrected_text) <> '' THEN 1 ELSE 0 END) AS corrected_count,
          MAX(reviewed_at) AS latest_reviewed_at
        FROM local_learning_candidates
        WHERE local_status = 'reviewed_human'
          AND queue_source IN ('ml_specialist_auditor', 'ml_specialist_scope_review')
          AND focus_group IS NOT NULL
        GROUP BY focus_group, queue_source
        ORDER BY reviewed_count DESC, agent_key
        LIMIT 20
        """,
    )

    agent_timeline = [
        {
            "id": row.get("id"),
            "rule_version": row.get("rule_version"),
            "routed_count": _int(row.get("routed_count")),
            "active_agent_covered_count": _int(row.get("active_agent_covered_count")),
            "planned_agent_covered_count": _int(row.get("planned_agent_covered_count")),
            "recommendation_count": _int(row.get("recommendation_count")),
            "finished_at": row.get("finished_at"),
        }
        for row in routing_runs
    ]

    return {
        "available": True,
        "summary": {
            "agents_total": _int(summary.get("agents_total")),
            "agents_operational": _int(summary.get("agents_operational")),
            "experimental_subagents": _int(summary.get("experimental_subagents")),
            "planned_subagents": _int(summary.get("planned_subagents")),
            "latest_false_safe": _int(summary.get("latest_false_safe")),
            "latest_false_safe_total": _int(summary.get("latest_false_safe")),
            "operational_false_safe": _int(summary.get("operational_false_safe")),
            "experimental_false_safe": _int(summary.get("experimental_false_safe")),
            "planned_false_safe": _int(summary.get("planned_false_safe")),
            "ensemble_net_gain": _int(summary.get("ensemble_net_gain")),
            "recommendation_evidence": _int(summary.get("recommendation_evidence")),
            "latest_recommendation_run_id": latest_reco_run.get("run_id"),
            "active_gate_overlay_run_id": active_gate.get("active_overlay_run_id"),
            "active_guarded_checkpoint_id": active_gate.get("active_guarded_checkpoint_id"),
            "active_gate_auto_apply_allowed": _int(active_gate.get("auto_apply_allowed")),
            "active_gate_guarded_releases": _int(active_gate.get("guarded_release_count")),
            "active_gate_invalid_releases": _int(active_gate.get("invalid_release_count")),
            "active_gate_apply_allowed_count": _int(active_gate.get("guarded_apply_allowed_count")),
        },
        "activeGate": active_gate,
        "topologyNodes": topology_nodes,
        "topologyEdges": topology_edges,
        "registry": registry,
        "health": registry,
        "recommendations": recommendations,
        "routingRuns": routing_runs,
        "routedItemsByAgent": routed_items_by_agent,
        "routingSamples": routing_samples,
        "ensembleImpact": ensemble_impact,
        "promotionReadiness": promotion_readiness,
        "experimentalContribution": experimental_contribution,
        "learningByAgent": learning_by_agent,
        "agentTimeline": agent_timeline,
    }


def _neural_registry_summary_payload(con: sqlite3.Connection) -> dict[str, Any]:
    def latest_inventory_diagnostic() -> dict[str, Any]:
        try:
            reports = sorted((ROOT / "reports").glob("*agent_inventory_diagnostic.json"))
            if not reports:
                return {}
            with reports[-1].open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def latest_json_report(pattern: str) -> dict[str, Any]:
        try:
            reports = sorted((ROOT / "reports").glob(pattern))
            if not reports:
                return {}
            with reports[-1].open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    if not _table_exists(con, "ml_agent_registry"):
        return {
            "available": False,
            "registered_agents": 0,
            "active_agents": 0,
            "experimental_agents": 0,
            "planned_agents": 0,
            "operational_agents": 0,
            "active_subspecialists": 0,
            "operational_false_safe": 0,
            "status_counts": {},
            "operational_state_counts": {},
            "status_operational_state_counts": {},
            "dashboard_group_counts": {},
            "classification_counts": {},
            "active_by_type": {},
            "experimental_by_type": {},
            "planned_by_type": {},
            "shadow_agents": 0,
            "lab_useful_evidence": 0,
            "requirement_effect_router": {},
            "effect_list_package": {},
            "criterion": "pending_instrumentation",
        }

    counts = _one(
        con,
        """
        SELECT
          COUNT(*) AS registered_agents,
          SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active_agents,
          SUM(CASE WHEN status = 'experimental' THEN 1 ELSE 0 END) AS experimental_agents,
          SUM(CASE WHEN status = 'planned' THEN 1 ELSE 0 END) AS planned_agents,
          SUM(CASE WHEN status = 'active' AND operational_state IN ('authoritative', 'operational', 'dry_run') THEN 1 ELSE 0 END) AS operational_agents,
          SUM(CASE WHEN status = 'active' AND agent_type = 'subspecialist' THEN 1 ELSE 0 END) AS active_subspecialists
        FROM ml_agent_registry
        """,
    )

    def by_type(status: str) -> dict[str, int]:
        return {
            row.get("agent_type") or "unknown": _int(row.get("total"))
            for row in _all(
                con,
                """
                SELECT COALESCE(agent_type, 'unknown') AS agent_type, COUNT(*) AS total
                FROM ml_agent_registry
                WHERE status = ?
                GROUP BY COALESCE(agent_type, 'unknown')
                ORDER BY total DESC, agent_type
                """,
                (status,),
            )
        }

    status_counts = {
        row.get("status") or "unknown": _int(row.get("total"))
        for row in _all(
            con,
            """
            SELECT COALESCE(status, 'unknown') AS status, COUNT(*) AS total
            FROM ml_agent_registry
            GROUP BY COALESCE(status, 'unknown')
            """,
        )
    }
    operational_state_counts = {
        row.get("operational_state") or "unknown": _int(row.get("total"))
        for row in _all(
            con,
            """
            SELECT COALESCE(operational_state, 'unknown') AS operational_state, COUNT(*) AS total
            FROM ml_agent_registry
            GROUP BY COALESCE(operational_state, 'unknown')
            """,
        )
    }
    status_operational_state_counts = {
        f"{row.get('status') or 'unknown'}/{row.get('operational_state') or 'unknown'}": _int(row.get("total"))
        for row in _all(
            con,
            """
            SELECT
              COALESCE(status, 'unknown') AS status,
              COALESCE(operational_state, 'unknown') AS operational_state,
              COUNT(*) AS total
            FROM ml_agent_registry
            GROUP BY COALESCE(status, 'unknown'), COALESCE(operational_state, 'unknown')
            """,
        )
    }
    dashboard_group_counts = {
        row.get("dashboard_group") or "unknown": _int(row.get("total"))
        for row in _all(
            con,
            """
            SELECT COALESCE(dashboard_group, 'unknown') AS dashboard_group, COUNT(*) AS total
            FROM ml_agent_registry
            GROUP BY COALESCE(dashboard_group, 'unknown')
            """,
        )
    }
    diagnostic = latest_inventory_diagnostic()
    post_architecture = latest_json_report("*global_post_not_requirement_effect_router_diagnostic_inventory.json") or latest_json_report("*global_post_architecture_diagnostic_inventory.json")
    post_summary = post_architecture.get("summary") or {}
    effect_list_inventory = latest_json_report("*effect_list_policy_catalog_inventory.json")
    effect_list_summary = effect_list_inventory.get("summary") or {}
    classification_counts = diagnostic.get("classification_counts") or {}
    router = _one(
        con,
        """
        SELECT
          agent_key,
          agent_type,
          parent_agent_key,
          status,
          operational_state,
          decision_role,
          scope_group,
          scope_description,
          dashboard_group,
          updated_at
        FROM ml_agent_registry
        WHERE agent_key = 'requirement_effect_router_readonly'
        LIMIT 1
        """,
    )
    router_children = _all(
        con,
        """
        SELECT
          agent_key,
          agent_type,
          status,
          operational_state,
          decision_role,
          dashboard_group
        FROM ml_agent_registry
        WHERE parent_agent_key = 'requirement_effect_router_readonly'
        ORDER BY decision_role, agent_key
        """,
    )
    router_terminal_count = sum(1 for row in router_children if row.get("decision_role") == "terminal_guard")
    router_splitter_count = sum(1 for row in router_children if row.get("decision_role") == "route_and_split")
    requirement_effect_router = {
        "registered": bool(router),
        **router,
        "children_count": len(router_children),
        "terminal_policies": router_terminal_count,
        "splitters": router_splitter_count,
        "requirement_effect_agents": _int(post_summary.get("requirement_effect_agents")),
        "effect_list_package_registered": bool(effect_list_summary),
        "read_only": True,
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
        "children": router_children,
    }
    effect_list_agents = _all(
        con,
        """
        WITH RECURSIVE effect_list_tree(agent_key) AS (
          SELECT agent_key
          FROM ml_agent_registry
          WHERE agent_key LIKE '%effect_list%'
             OR parent_agent_key LIKE '%effect_list%'
             OR scope_group LIKE '%effect_list%'
          UNION
          SELECT child.agent_key
          FROM ml_agent_registry child
          JOIN effect_list_tree parent
            ON child.parent_agent_key = parent.agent_key
        )
        SELECT
          a.agent_key,
          a.agent_type,
          a.parent_agent_key,
          a.status,
          a.operational_state,
          a.decision_role,
          a.scope_group,
          a.scope_description,
          a.dashboard_group
        FROM ml_agent_registry a
        JOIN effect_list_tree t ON t.agent_key = a.agent_key
        ORDER BY a.decision_role, a.agent_key
        """,
    )
    effect_list_top_blockers = [
        {
            "route": row.get("route"),
            "segments": _int(row.get("segments")),
        }
        for row in (post_architecture.get("top_routes_without_spec") or effect_list_inventory.get("large_routes_without_spec") or [])[:8]
        if row.get("route")
    ]
    effect_list_subroutes = [
        {
            "route": row.get("subroute"),
            "segments": _int(row.get("segments")),
            "registered": bool(row.get("registered")),
            "terminal_policy": bool(row.get("terminal_policy")),
        }
        for row in (effect_list_inventory.get("effect_list_subroute_counts") or [])[:10]
        if row.get("subroute")
    ]
    effect_list_package = {
        "registered": bool(effect_list_summary),
        "agents": effect_list_agents,
        "agents_count": _int(post_summary.get("effect_list_agents") or len(effect_list_agents)),
        "registered_rows": len(effect_list_agents),
        "specs": _int(effect_list_summary.get("specs_effect_list_loaded")),
        "terminal_policies": _int(effect_list_summary.get("terminal_effect_list_policies")),
        "splitters": _int(effect_list_summary.get("splitter_effect_list_policies")),
        "registry_created": _int(effect_list_summary.get("spec_only_to_create")),
        "registry_updated": _int(effect_list_summary.get("registered_effect_list_specs")),
        "coverage_before": _int(post_summary.get("segments_with_spec_associated_before_effect_list") or effect_list_summary.get("segments_with_spec_associated_before")),
        "coverage_after": _int(post_summary.get("segments_with_spec_associated_after_effect_list") or effect_list_summary.get("segments_with_spec_associated_after")),
        "coverage_gain": _int(post_summary.get("architecture_material_gain_segments")),
        "route_count": _int(post_summary.get("segments_with_effect_list_route") or effect_list_summary.get("segments_with_effect_list_route")),
        "with_spec": _int(post_summary.get("segments_with_effect_list_spec_associated") or effect_list_summary.get("segments_with_spec_effect_list_associated")),
        "terminal_registered": _int(post_summary.get("segments_with_effect_list_registered_terminal_policy") or effect_list_summary.get("segments_with_registered_terminal_effect_list_policy")),
        "segments_without_useful_spec": _int(post_summary.get("segments_without_useful_spec")),
        "top_blockers": effect_list_top_blockers,
        "subroutes": effect_list_subroutes,
        "read_only": True,
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
    }
    not_requirement_effect_agents = _all(
        con,
        """
        SELECT
          agent_key,
          agent_type,
          parent_agent_key,
          status,
          operational_state,
          decision_role,
          scope_group,
          scope_description,
          dashboard_group
        FROM ml_agent_registry
        WHERE agent_key LIKE '%not_requirement_effect%'
           OR parent_agent_key LIKE '%not_requirement_effect%'
           OR scope_group LIKE '%not_requirement_effect%'
        ORDER BY decision_role, agent_key
        """,
    )
    not_requirement_effect_package = {
        "registered": bool(not_requirement_effect_agents),
        "agents": not_requirement_effect_agents,
        "agents_count": _int(post_summary.get("not_requirement_effect_agents") or len(not_requirement_effect_agents)),
        "universe": _int(post_summary.get("not_requirement_effect_universe")),
        "review_total": _int(post_summary.get("not_requirement_effect_review_total")),
        "reuse_existing_policies": _int(post_summary.get("not_requirement_effect_reuse_existing_policies")),
        "new_router_needed": _int(post_summary.get("not_requirement_effect_new_router_needed")),
        "reuse_semantic_review_router": _int(post_summary.get("not_req_reuse_semantic_review_router")),
        "reuse_gender_local_player_policy": _int(post_summary.get("not_req_reuse_gender_local_player_policy")),
        "needs_culture_religion_router": _int(post_summary.get("needs_not_req_culture_religion_router")),
        "reuse_short_label_style_policy": _int(post_summary.get("not_req_reuse_short_label_style_policy")),
        "reuse_autofix_unknown_router": _int(post_summary.get("not_req_reuse_autofix_unknown_router")),
        "unrouted_or_preserved": _int(post_summary.get("not_requirement_effect_unrouted_or_preserved")),
        "chain_complete": bool(post_summary.get("chain_complete")),
        "validated_chain": post_architecture.get("validated_chain") or [
            "not_requirement_effect_global_router",
            "not_requirement_effect_culture_religion_router",
            "not_requirement_effect_culture_policy",
            "not_requirement_effect_culture_tradition_heritage_policy",
        ],
        "read_only": True,
        "auto_apply_allowed": 0,
        "production_release_allowed": 0,
        "lifecycle_allowed": 0,
    }
    registry_agents = _all(
        con,
        """
        SELECT
          agent_key,
          agent_type,
          parent_agent_key,
          status,
          operational_state,
          decision_role,
          scope_group,
          scope_description,
          dashboard_group
        FROM ml_agent_registry
        ORDER BY dashboard_group, priority, agent_key
        """,
    )

    operational_false_safe = 0
    if _table_exists(con, "ml_model_runs"):
        row = _one(
            con,
            """
            WITH latest_agent_model AS (
              SELECT
                a.agent_key,
                a.status,
                a.operational_state,
                COALESCE(m.false_safe_count, 0) AS false_safe_count
              FROM ml_agent_registry a
              LEFT JOIN ml_model_runs m
                ON m.model_kind = a.model_kind
               AND m.finished_at IS NOT NULL
               AND m.id = (
                 SELECT MAX(m2.id)
                 FROM ml_model_runs m2
                 WHERE m2.model_kind = a.model_kind
                   AND m2.finished_at IS NOT NULL
               )
            )
            SELECT COALESCE(SUM(false_safe_count), 0) AS operational_false_safe
            FROM latest_agent_model
            WHERE status = 'active'
              AND operational_state IN ('authoritative', 'operational', 'dry_run')
            """,
        )
        operational_false_safe = _int(row.get("operational_false_safe"))

    return {
        "available": True,
        "registered_agents": _int(counts.get("registered_agents")),
        "active_agents": _int(counts.get("active_agents")),
        "experimental_agents": _int(counts.get("experimental_agents")),
        "planned_agents": _int(counts.get("planned_agents")),
        "operational_agents": _int(post_summary.get("operational_agents") or counts.get("operational_agents")),
        "operational_agents_live_sql": _int(counts.get("operational_agents")),
        "active_subspecialists": _int(counts.get("active_subspecialists")),
        "operational_false_safe": operational_false_safe,
        "status_counts": status_counts,
        "operational_state_counts": operational_state_counts,
        "status_operational_state_counts": status_operational_state_counts,
        "dashboard_group_counts": dashboard_group_counts,
        "classification_counts": classification_counts,
        "active_by_type": by_type("active"),
        "experimental_by_type": by_type("experimental"),
        "planned_by_type": by_type("planned"),
        "shadow_agents": _int(post_summary.get("shadow_agents") or operational_state_counts.get("shadow")),
        "lab_useful_evidence": _int(classification_counts.get("lab_useful_evidence")),
        "planned_backlog": _int(classification_counts.get("planned_backlog")),
        "dry_run_agents": _int(post_summary.get("dry_run_agents") or operational_state_counts.get("dry_run")),
        "terminal_guard_agents": _int(post_summary.get("terminal_guard_agents")),
        "splitter_agents": _int(post_summary.get("splitter_agents")),
        "requirement_effect_agents": _int(post_summary.get("requirement_effect_agents")),
        "not_requirement_effect_agents": _int(post_summary.get("not_requirement_effect_agents") or not_requirement_effect_package.get("agents_count")),
        "effect_list_agents": _int(post_summary.get("effect_list_agents") or effect_list_package.get("agents_count")),
        "artifact_activity_agents": _int(post_summary.get("artifact_activity_agents")),
        "building_modifier_agents": _int(post_summary.get("building_modifier_agents")),
        "event_context_agents": _int(post_summary.get("event_context_agents")),
        "residual_agents": _int(post_summary.get("residual_agents")),
        "accolade_trait_agents": _int(post_summary.get("accolade_trait_agents")),
        "script_value_agents": _int(post_summary.get("script_value_agents")),
        "holy_site_agents": _int(post_summary.get("holy_site_agents")),
        "culture_religion_agents": _int(post_summary.get("culture_religion_agents")),
        "spec_associated_after_effect_list": _int(post_summary.get("segments_with_spec_associated_after_effect_list")),
        "spec_associated_after_requirement_effect_maturation": _int(post_summary.get("segments_with_spec_associated_after_requirement_effect_maturation")),
        "spec_associated_after_not_requirement_effect": _int(post_summary.get("segments_with_spec_associated_after_not_requirement_effect")),
        "registered_terminal_policy_segments": _int(post_summary.get("segments_with_registered_terminal_policy")),
        "shadow_splitter_policy_segments": _int(post_summary.get("segments_with_shadow_splitter_policy")),
        "coverage_gain_since_effect_list": _int(post_summary.get("coverage_gain_since_effect_list")),
        "coverage_gain_since_post_holy_site": _int(post_summary.get("coverage_gain_since_post_holy_site")),
        "segments_without_useful_spec": _int(post_summary.get("segments_without_useful_spec")),
        "remaining_gaps": {
            "domain_context_after_requirement_effect": _int(post_summary.get("domain_context_after_requirement_effect")),
            "blocked_uncertain": _int(post_summary.get("blocked_uncertain")),
            "script_value_effect_residual_repair_or_preserved_sublane": _int(post_summary.get("script_value_effect_residual_repair_or_preserved_sublane")),
            "accolade_trait_residual_repair_or_preserved_sublane": _int(post_summary.get("accolade_trait_residual_repair_or_preserved_sublane")),
            "residual_culture_or_name_policy": _int(post_summary.get("residual_culture_or_name_policy")),
            "not_requirement_effect_unrouted_or_preserved": _int(post_summary.get("not_requirement_effect_unrouted_or_preserved")),
            "macro_lane_router_missing_parent": _int(post_summary.get("macro_lane_router_missing_parent")),
        },
        "requirement_effect_router": requirement_effect_router,
        "effect_list_package": effect_list_package,
        "not_requirement_effect_package": not_requirement_effect_package,
        "registry_agents": registry_agents,
        "diagnostic_generated_at": diagnostic.get("generated_at"),
        "post_architecture_generated_at": post_architecture.get("generated_at") or post_summary.get("generated_at"),
        "post_architecture_summary": post_summary,
        "post_architecture_source": post_architecture.get("source"),
        "diagnostic_totals": diagnostic.get("totals") or {},
        "criterion": "status='active' AND operational_state IN ('authoritative','operational','dry_run')",
    }


def _lifecycle_payload(con: sqlite3.Connection) -> dict[str, Any]:
    def output_apply_payload() -> dict[str, Any]:
        if not (_table_exists(con, "segment_output_apply_runs") and _table_exists(con, "segment_output_apply_items")):
            return {
                "available": False,
                "summary": {},
                "runs": [],
                "evolution": [],
                "packageItems": [],
                "tokenBlocks": [],
            }

        summary = _one(
            con,
            """
            WITH latest_applied AS (
              SELECT id, applied_count, files_touched_count, backup_root, report_path, started_at
              FROM segment_output_apply_runs
              WHERE apply = 1
              ORDER BY started_at DESC, id DESC
              LIMIT 1
            )
            SELECT
              COALESCE(SUM(CASE WHEN r.apply = 1 THEN r.applied_count ELSE 0 END), 0) AS total_applied,
              COALESCE((SELECT applied_count FROM latest_applied), 0) AS latest_applied_count,
              COALESCE(SUM(r.token_mismatch_count), 0) AS token_mismatch_count,
              COALESCE(SUM(CASE WHEN r.apply = 1 THEN r.files_touched_count ELSE 0 END), 0) AS files_touched_count,
              COALESCE(SUM(CASE WHEN r.apply = 0 THEN 1 ELSE 0 END), 0) AS dry_run_count,
              (SELECT id FROM latest_applied) AS latest_apply_run_id,
              (SELECT backup_root FROM latest_applied) AS latest_backup_root,
              (SELECT report_path FROM latest_applied) AS latest_report_path,
              (SELECT started_at FROM latest_applied) AS latest_started_at
            FROM segment_output_apply_runs r
            """,
        )
        runs = _all(
            con,
            """
            SELECT
              id AS apply_run_id,
              state_run_id,
              apply,
              rule_version,
              limit_count,
              path_filter,
              review_states,
              candidates_inspected,
              ready_count,
              applied_count,
              skipped_count,
              token_mismatch_count,
              files_touched_count,
              backup_root,
              report_path,
              started_at,
              finished_at
            FROM segment_output_apply_runs
            ORDER BY started_at DESC, id DESC
            LIMIT 20
            """,
        )
        evolution = _all(
            con,
            """
            WITH state_runs AS (
              SELECT
                id,
                finished_at,
                output_apply_pending_count,
                pending_count,
                closed_count,
                total_segments
              FROM segment_state_runs
              WHERE total_segments > 1000
            ),
            apply_by_state AS (
              SELECT
                state_run_id,
                SUM(CASE WHEN apply = 1 THEN applied_count ELSE 0 END) AS applied_count,
                SUM(token_mismatch_count) AS token_mismatch_count
              FROM segment_output_apply_runs
              GROUP BY state_run_id
            )
            SELECT
              s.id AS state_run_id,
              s.finished_at,
              s.output_apply_pending_count,
              s.pending_count,
              s.closed_count,
              ROUND(100.0 * s.closed_count / NULLIF(s.total_segments, 0), 2) AS closed_pct,
              COALESCE(a.applied_count, 0) AS applied_from_this_state,
              COALESCE(a.token_mismatch_count, 0) AS token_mismatch_from_this_state
            FROM state_runs s
            LEFT JOIN apply_by_state a ON a.state_run_id = s.id
            ORDER BY s.finished_at ASC, s.id ASC
            """,
        )
        package_items = _all(
            con,
            """
            SELECT
              r.id AS apply_run_id,
              substr(i.relative_path, 1, instr(i.relative_path || '/', '/') - 1) AS package_name,
              i.review_state,
              COUNT(*) AS inspected_count,
              SUM(CASE WHEN i.applied = 1 THEN 1 ELSE 0 END) AS applied_count,
              SUM(CASE WHEN i.token_mismatch = 1 THEN 1 ELSE 0 END) AS token_mismatch_count
            FROM segment_output_apply_items i
            JOIN segment_output_apply_runs r ON r.id = i.run_id
            GROUP BY r.id, package_name, i.review_state
            ORDER BY r.id DESC, applied_count DESC, token_mismatch_count DESC
            LIMIT 200
            """,
        )
        token_blocks = _all(
            con,
            """
            SELECT
              i.run_id AS apply_run_id,
              i.segment_id,
              i.relative_path,
              i.source_line_number,
              i.source_key,
              i.final_state,
              i.review_state,
              i.result_status,
              i.reasons_json,
              r.report_path,
              i.created_at
            FROM segment_output_apply_items i
            JOIN segment_output_apply_runs r ON r.id = i.run_id
            WHERE i.token_mismatch = 1
            ORDER BY i.created_at DESC, i.relative_path, i.source_line_number
            LIMIT 300
            """,
        )
        return {
            "available": True,
            "summary": summary,
            "runs": runs,
            "evolution": evolution,
            "packageItems": package_items,
            "tokenBlocks": token_blocks,
        }

    def token_policy_payload() -> dict[str, Any]:
        def checkpoint_payload() -> dict[str, Any]:
            if not _table_exists(con, "ml_composite_checkpoints"):
                return {
                    "available": False,
                    "summary": {},
                    "runs": [],
                    "trend": [],
                    "statusDistribution": [],
                    "registry": {},
                    "promotions": [],
                    "activeQueue": {"summary": {}, "runs": [], "routes": [], "fullSummary": {}, "fullRoutes": []},
                    "reviewProgress": {"summary": {}, "routes": [], "trend": []},
                    "subpolicyDiagnostic": {"summary": {}, "items": [], "statusDistribution": [], "routeMatrix": [], "trend": []},
                }
            registry = _one(
                con,
                """
                SELECT
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
                FROM ml_composite_gate_registry
                WHERE gate_key = 'segment_token_composite_review_gate'
                LIMIT 1
                """,
            ) if _table_exists(con, "ml_composite_gate_registry") else {}
            promotions = _all(
                con,
                """
                SELECT
                  id AS promotion_id,
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
                FROM ml_composite_gate_promotions
                ORDER BY created_at DESC, id DESC
                LIMIT 20
                """,
            ) if _table_exists(con, "ml_composite_gate_promotions") else []
            active_queue_runs = _all(
                con,
                """
                SELECT
                  id AS queue_run_id,
                  rule_version,
                  gate_key,
                  checkpoint_id,
                  overlay_run_id,
                  source_policy_run_id,
                  source_mode,
                  route_filter_csv,
                  risk_filter_csv,
                  critical_only,
                  limit_count,
                  total_rows,
                  critical_rows,
                  high_rows,
                  medium_rows,
                  low_rows,
                  route_counts_json,
                  bucket_counts_json,
                  report_path,
                  csv_path,
                  jsonl_path,
                  decisions_template_path,
                  started_at,
                  finished_at
                FROM ml_composite_gate_queue_runs
                ORDER BY started_at DESC, id DESC
                LIMIT 20
                """,
            ) if _table_exists(con, "ml_composite_gate_queue_runs") else []
            active_queue_summary = active_queue_runs[0] if active_queue_runs else {}
            active_queue_routes = _all(
                con,
                """
                SELECT
                  suggested_route,
                  overlay_policy_bucket,
                  overlay_risk_level,
                  total
                FROM ml_composite_gate_queue_routes
                WHERE queue_run_id = ?
                ORDER BY total DESC, suggested_route
                """,
                (active_queue_summary["queue_run_id"],),
            ) if active_queue_summary and _table_exists(con, "ml_composite_gate_queue_routes") else []
            active_queue_full_summary = _one(
                con,
                """
                SELECT
                  id AS queue_run_id,
                  rule_version,
                  gate_key,
                  checkpoint_id,
                  overlay_run_id,
                  source_policy_run_id,
                  source_mode,
                  route_filter_csv,
                  risk_filter_csv,
                  critical_only,
                  limit_count,
                  total_rows,
                  critical_rows,
                  high_rows,
                  medium_rows,
                  low_rows,
                  route_counts_json,
                  bucket_counts_json,
                  report_path,
                  csv_path,
                  jsonl_path,
                  decisions_template_path,
                  started_at,
                  finished_at
                FROM ml_composite_gate_queue_runs
                WHERE source_mode = 'active_composite_gate'
                  AND critical_only = 0
                  AND route_filter_csv IS NULL
                  AND risk_filter_csv IS NULL
                  AND limit_count IS NULL
                ORDER BY started_at DESC, id DESC
                LIMIT 1
                """,
            ) if _table_exists(con, "ml_composite_gate_queue_runs") else {}
            active_queue_full_routes = _all(
                con,
                """
                SELECT
                  suggested_route,
                  overlay_policy_bucket,
                  overlay_risk_level,
                  total
                FROM ml_composite_gate_queue_routes
                WHERE queue_run_id = ?
                ORDER BY total DESC, suggested_route
                """,
                (active_queue_full_summary["queue_run_id"],),
            ) if active_queue_full_summary and _table_exists(con, "ml_composite_gate_queue_routes") else []
            review_progress_summary = _one(
                con,
                """
                SELECT
                  id AS snapshot_id,
                  rule_version,
                  gate_key,
                  checkpoint_id,
                  overlay_run_id,
                  source_policy_run_id,
                  total_items,
                  queued_items,
                  unqueued_items,
                  reviewed_items,
                  pending_items,
                  approved_for_apply_count,
                  rejected_count,
                  fix_count,
                  needs_subpolicy_count,
                  manual_exception_count,
                  review_coverage_pct,
                  queue_coverage_pct,
                  report_path,
                  started_at,
                  finished_at
                FROM ml_composite_gate_review_snapshots
                ORDER BY started_at DESC, id DESC
                LIMIT 1
                """,
            ) if _table_exists(con, "ml_composite_gate_review_snapshots") else {}
            review_progress_routes = _all(
                con,
                """
                SELECT
                  suggested_route,
                  overlay_policy_bucket,
                  overlay_risk_level,
                  total_items,
                  queued_items,
                  unqueued_items,
                  reviewed_items,
                  pending_items,
                  approved_for_apply_count,
                  rejected_count,
                  fix_count,
                  needs_subpolicy_count,
                  manual_exception_count,
                  review_coverage_pct,
                  queue_coverage_pct,
                  latest_queue_run_id,
                  latest_queue_total_rows
                FROM ml_composite_gate_review_route_status
                WHERE snapshot_id = ?
                ORDER BY review_coverage_pct ASC, total_items DESC, suggested_route
                """,
                (review_progress_summary["snapshot_id"],),
            ) if review_progress_summary and _table_exists(con, "ml_composite_gate_review_route_status") else []
            review_progress_trend = _all(
                con,
                """
                SELECT
                  id AS snapshot_id,
                  total_items,
                  queued_items,
                  unqueued_items,
                  reviewed_items,
                  pending_items,
                  approved_for_apply_count,
                  rejected_count,
                  fix_count,
                  needs_subpolicy_count,
                  manual_exception_count,
                  review_coverage_pct,
                  queue_coverage_pct,
                  started_at
                FROM ml_composite_gate_review_snapshots
                ORDER BY started_at ASC, id ASC
                LIMIT 80
                """,
            ) if _table_exists(con, "ml_composite_gate_review_snapshots") else []
            subpolicy_diagnostic_summary = _one(
                con,
                """
                SELECT
                  id AS diagnostic_run_id,
                  rule_version,
                  gate_key,
                  checkpoint_id,
                  overlay_run_id,
                  source_policy_run_id,
                  total_items,
                  reviewed_items,
                  pending_items,
                  grouped_subpolicies,
                  design_candidate_count,
                  policy_candidate_count,
                  needs_more_review_count,
                  queue_review_candidate_count,
                  report_path,
                  csv_path,
                  json_path,
                  started_at,
                  finished_at
                FROM ml_composite_subpolicy_diagnostic_runs
                ORDER BY started_at DESC, id DESC
                LIMIT 1
                """,
            ) if _table_exists(con, "ml_composite_subpolicy_diagnostic_runs") else {}
            subpolicy_diagnostic_items = _all(
                con,
                """
                SELECT
                  suggested_route,
                  token_subtype,
                  overlay_policy_bucket,
                  overlay_risk_level,
                  total_items,
                  queued_items,
                  unqueued_items,
                  reviewed_items,
                  pending_items,
                  approved_for_apply_count,
                  accept_count,
                  keep_manual_exception_count,
                  reject_count,
                  needs_subpolicy_count,
                  fix_count,
                  review_coverage_pct,
                  queue_coverage_pct,
                  maturity_status,
                  confidence_band,
                  recommended_action,
                  sample_policy_item_ids_json,
                  sample_paths_json,
                  token_families_json
                FROM ml_composite_subpolicy_diagnostic_items
                WHERE run_id = ?
                ORDER BY
                  CASE maturity_status
                    WHEN 'policy_candidate_review' THEN 0
                    WHEN 'ready_to_design_subpolicy' THEN 1
                    WHEN 'negative_boundary_learned' THEN 2
                    WHEN 'conflicting_evidence' THEN 3
                    WHEN 'queued_waiting_review' THEN 4
                    WHEN 'needs_more_review' THEN 5
                    WHEN 'needs_queue' THEN 6
                    ELSE 9
                  END,
                  reviewed_items DESC,
                  total_items DESC,
                  suggested_route,
                  token_subtype
                LIMIT 120
                """,
                (subpolicy_diagnostic_summary["diagnostic_run_id"],),
            ) if subpolicy_diagnostic_summary and _table_exists(con, "ml_composite_subpolicy_diagnostic_items") else []
            subpolicy_diagnostic_status = _all(
                con,
                """
                SELECT
                  maturity_status,
                  confidence_band,
                  COUNT(*) AS total_groups,
                  SUM(total_items) AS total_items,
                  SUM(reviewed_items) AS reviewed_items,
                  SUM(pending_items) AS pending_items
                FROM ml_composite_subpolicy_diagnostic_items
                WHERE run_id = ?
                GROUP BY maturity_status, confidence_band
                ORDER BY total_groups DESC, maturity_status
                """,
                (subpolicy_diagnostic_summary["diagnostic_run_id"],),
            ) if subpolicy_diagnostic_summary and _table_exists(con, "ml_composite_subpolicy_diagnostic_items") else []
            subpolicy_diagnostic_routes = _all(
                con,
                """
                SELECT
                  suggested_route,
                  maturity_status,
                  COUNT(*) AS total_groups,
                  SUM(total_items) AS total_items,
                  SUM(reviewed_items) AS reviewed_items,
                  SUM(needs_subpolicy_count) AS needs_subpolicy_count
                FROM ml_composite_subpolicy_diagnostic_items
                WHERE run_id = ?
                GROUP BY suggested_route, maturity_status
                ORDER BY suggested_route, total_groups DESC
                """,
                (subpolicy_diagnostic_summary["diagnostic_run_id"],),
            ) if subpolicy_diagnostic_summary and _table_exists(con, "ml_composite_subpolicy_diagnostic_items") else []
            subpolicy_diagnostic_trend = _all(
                con,
                """
                SELECT
                  id AS diagnostic_run_id,
                  grouped_subpolicies,
                  design_candidate_count,
                  policy_candidate_count,
                  needs_more_review_count,
                  queue_review_candidate_count,
                  reviewed_items,
                  pending_items,
                  started_at
                FROM ml_composite_subpolicy_diagnostic_runs
                ORDER BY started_at ASC, id ASC
                LIMIT 80
                """,
            ) if _table_exists(con, "ml_composite_subpolicy_diagnostic_runs") else []
            subpolicy_diagnostic_payload = {
                "summary": subpolicy_diagnostic_summary,
                "items": subpolicy_diagnostic_items,
                "statusDistribution": subpolicy_diagnostic_status,
                "routeMatrix": subpolicy_diagnostic_routes,
                "trend": subpolicy_diagnostic_trend,
            }
            runs = _all(
                con,
                """
                SELECT
                  id AS checkpoint_id,
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
                  finished_at
                FROM ml_composite_checkpoints
                ORDER BY started_at DESC, id DESC
                LIMIT 20
                """,
            )
            if not runs:
                return {
                    "available": True,
                    "summary": {},
                    "runs": [],
                    "trend": [],
                    "statusDistribution": [],
                    "registry": registry,
                    "promotions": promotions,
                    "activeQueue": {
                        "summary": active_queue_summary,
                        "runs": active_queue_runs,
                        "routes": active_queue_routes,
                        "fullSummary": active_queue_full_summary,
                        "fullRoutes": active_queue_full_routes,
                    },
                    "reviewProgress": {
                        "summary": review_progress_summary,
                        "routes": review_progress_routes,
                        "trend": review_progress_trend,
                    },
                    "subpolicyDiagnostic": subpolicy_diagnostic_payload,
                }
            trend = _all(
                con,
                """
                SELECT
                  id AS checkpoint_id,
                  overlay_run_id,
                  source_policy_run_id,
                  base_critical_count,
                  overlay_critical_count,
                  released_critical_count,
                  critical_queue_count,
                  enabled_rule_count,
                  promotion_status,
                  started_at
                FROM ml_composite_checkpoints
                ORDER BY started_at ASC, id ASC
                LIMIT 80
                """,
            )
            status_distribution = _all(
                con,
                """
                SELECT
                  promotion_status,
                  recommended_action,
                  COUNT(*) AS total
                FROM ml_composite_checkpoints
                GROUP BY promotion_status, recommended_action
                ORDER BY total DESC, promotion_status
                """,
            )
            return {
                "available": True,
                "summary": runs[0],
                "runs": runs,
                "trend": trend,
                "statusDistribution": status_distribution,
                "registry": registry,
                "promotions": promotions,
                "activeQueue": {
                    "summary": active_queue_summary,
                    "runs": active_queue_runs,
                    "routes": active_queue_routes,
                    "fullSummary": active_queue_full_summary,
                    "fullRoutes": active_queue_full_routes,
                },
                "reviewProgress": {
                    "summary": review_progress_summary,
                    "routes": review_progress_routes,
                    "trend": review_progress_trend,
                },
                "subpolicyDiagnostic": subpolicy_diagnostic_payload,
            }

        def overlay_payload() -> dict[str, Any]:
            if not (
                _table_exists(con, "segment_token_policy_overlay_runs")
                and _table_exists(con, "segment_token_policy_overlay_items")
            ):
                return {
                    "available": False,
                    "summary": {},
                    "runs": [],
                    "riskComparison": [],
                    "actionDistribution": [],
                    "bucketDistribution": [],
                    "releasedByRule": [],
                    "releasedItems": [],
                    "remainingCritical": [],
                }

            runs = _all(
                con,
                """
                SELECT
                  id AS overlay_run_id,
                  rule_version,
                  source_policy_run_id,
                  source_state_run_id,
                  source_rule_version,
                  overlay_name,
                  min_evidence,
                  total_candidates,
                  original_critical_count,
                  overlay_critical_count,
                  released_critical_count,
                  remaining_blocked_count,
                  enabled_rule_count,
                  apply_allowed_count,
                  ROUND(100.0 * released_critical_count / NULLIF(original_critical_count, 0), 2) AS critical_release_pct,
                  report_path,
                  csv_path,
                  jsonl_path,
                  notes_json,
                  started_at,
                  finished_at
                FROM segment_token_policy_overlay_runs
                WHERE finished_at IS NOT NULL
                ORDER BY finished_at DESC, id DESC
                LIMIT 20
                """,
            )
            if not runs:
                return {
                    "available": True,
                    "summary": {},
                    "runs": [],
                    "riskComparison": [],
                    "actionDistribution": [],
                    "bucketDistribution": [],
                    "releasedByRule": [],
                    "releasedItems": [],
                    "remainingCritical": [],
                }

            overlay_run_id = runs[0]["overlay_run_id"]
            risk_comparison = _all(
                con,
                """
                SELECT
                  original_risk_level,
                  overlay_risk_level,
                  COUNT(*) AS total
                FROM segment_token_policy_overlay_items
                WHERE run_id = ?
                GROUP BY original_risk_level, overlay_risk_level
                ORDER BY
                  CASE original_risk_level
                    WHEN 'critical' THEN 0
                    WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2
                    WHEN 'low' THEN 3
                    ELSE 9
                  END,
                  CASE overlay_risk_level
                    WHEN 'critical' THEN 0
                    WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2
                    WHEN 'low' THEN 3
                    ELSE 9
                  END
                """,
                (overlay_run_id,),
            )
            action_distribution = _all(
                con,
                """
                SELECT
                  overlay_action,
                  overlay_risk_level,
                  overlay_agent_key,
                  COUNT(*) AS total
                FROM segment_token_policy_overlay_items
                WHERE run_id = ?
                GROUP BY overlay_action, overlay_risk_level, overlay_agent_key
                ORDER BY total DESC, overlay_action
                """,
                (overlay_run_id,),
            )
            bucket_distribution = _all(
                con,
                """
                SELECT
                  overlay_policy_bucket,
                  overlay_risk_level,
                  COUNT(*) AS total,
                  SUM(CASE WHEN would_release_critical = 1 THEN 1 ELSE 0 END) AS released_critical_count,
                  SUM(CASE WHEN apply_allowed = 1 THEN 1 ELSE 0 END) AS apply_allowed_count
                FROM segment_token_policy_overlay_items
                WHERE run_id = ?
                GROUP BY overlay_policy_bucket, overlay_risk_level
                ORDER BY
                  CASE overlay_risk_level
                    WHEN 'critical' THEN 0
                    WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2
                    WHEN 'low' THEN 3
                    ELSE 9
                  END,
                  total DESC,
                  overlay_policy_bucket
                """,
                (overlay_run_id,),
            )
            released_by_rule = _all(
                con,
                """
                SELECT
                  COALESCE(NULLIF(rule_key, ''), 'unchanged') AS rule_key,
                  overlay_agent_key,
                  COUNT(*) AS total,
                  SUM(CASE WHEN would_release_critical = 1 THEN 1 ELSE 0 END) AS released_critical_count
                FROM segment_token_policy_overlay_items
                WHERE run_id = ?
                  AND would_release_critical = 1
                GROUP BY COALESCE(NULLIF(rule_key, ''), 'unchanged'), overlay_agent_key
                ORDER BY released_critical_count DESC, rule_key
                """,
                (overlay_run_id,),
            )
            released_items = _all(
                con,
                """
                SELECT
                  source_policy_item_id,
                  segment_id,
                  relative_path,
                  source_line_number,
                  source_key,
                  original_policy_bucket,
                  original_risk_level,
                  overlay_policy_bucket,
                  overlay_risk_level,
                  overlay_action,
                  overlay_agent_key,
                  decision,
                  rule_key,
                  reasons_json
                FROM segment_token_policy_overlay_items
                WHERE run_id = ?
                  AND would_release_critical = 1
                ORDER BY rule_key, relative_path, source_line_number
                LIMIT 200
                """,
                (overlay_run_id,),
            )
            remaining_critical = _all(
                con,
                """
                SELECT
                  source_policy_item_id,
                  segment_id,
                  relative_path,
                  source_line_number,
                  source_key,
                  original_policy_bucket,
                  original_risk_level,
                  overlay_policy_bucket,
                  overlay_risk_level,
                  overlay_action,
                  overlay_agent_key,
                  decision,
                  rule_key,
                  reasons_json
                FROM segment_token_policy_overlay_items
                WHERE run_id = ?
                  AND overlay_risk_level = 'critical'
                ORDER BY overlay_policy_bucket, relative_path, source_line_number
                LIMIT 200
                """,
                (overlay_run_id,),
            )

            return {
                "available": True,
                "summary": runs[0],
                "runs": runs,
                "riskComparison": risk_comparison,
                "actionDistribution": action_distribution,
                "bucketDistribution": bucket_distribution,
                "releasedByRule": released_by_rule,
                "releasedItems": released_items,
                "remainingCritical": remaining_critical,
            }

        def decision_payload() -> dict[str, Any]:
            if not (_table_exists(con, "segment_token_policy_decision_runs") and _table_exists(con, "segment_token_policy_decisions")):
                return {
                    "available": False,
                    "summary": {},
                    "runs": [],
                    "byBucket": [],
                    "coverage": [],
                }

            decision_runs = _all(
                con,
                """
                SELECT
                  id AS decision_run_id,
                  policy_run_id,
                  source_report,
                  decisions_path,
                  total_decisions,
                  approved_count,
                  rejected_count,
                  fix_count,
                  skipped_count,
                  report_path,
                  started_at,
                  finished_at
                FROM segment_token_policy_decision_runs
                WHERE finished_at IS NOT NULL
                ORDER BY finished_at DESC, id DESC
                LIMIT 20
                """,
            )
            latest_policy_id = decision_runs[0]["policy_run_id"] if decision_runs else None
            if latest_policy_id is None:
                return {
                    "available": True,
                    "summary": {},
                    "runs": decision_runs,
                    "byBucket": [],
                    "coverage": [],
                }

            summary = _one(
                con,
                """
                SELECT
                  ? AS policy_run_id,
                  COUNT(*) AS total_decisions,
                  SUM(CASE WHEN approved_for_apply = 1 THEN 1 ELSE 0 END) AS approved_for_apply,
                  SUM(CASE WHEN decision = 'accept_policy_candidate' THEN 1 ELSE 0 END) AS accepted_policy,
                  SUM(CASE WHEN decision = 'keep_manual_exception_only' THEN 1 ELSE 0 END) AS manual_exceptions,
                  SUM(CASE WHEN decision = 'reject_policy_candidate' THEN 1 ELSE 0 END) AS rejected_policy,
                  SUM(CASE WHEN decision IN ('fix_confirmed_text', 'encoding_cleanup_required', 'manual_token_rewrite_required') THEN 1 ELSE 0 END) AS needs_fix,
                  SUM(CASE WHEN decision = 'needs_subpolicy' THEN 1 ELSE 0 END) AS needs_subpolicy,
                  SUM(CASE WHEN approved_for_apply = 1 AND risk_level = 'critical' THEN 1 ELSE 0 END) AS critical_approved_for_apply
                FROM segment_token_policy_decisions
                WHERE policy_run_id = ?
                """,
                (latest_policy_id, latest_policy_id),
            )
            by_bucket = _all(
                con,
                """
                SELECT
                  policy_bucket,
                  risk_level,
                  decision,
                  approved_for_apply,
                  COUNT(*) AS total
                FROM segment_token_policy_decisions
                WHERE policy_run_id = ?
                GROUP BY policy_bucket, risk_level, decision, approved_for_apply
                ORDER BY total DESC, policy_bucket, decision
                """,
                (latest_policy_id,),
            )
            coverage = _all(
                con,
                """
                WITH policy_items AS (
                  SELECT id, policy_bucket, risk_level
                  FROM segment_token_policy_items
                  WHERE run_id = ?
                )
                SELECT
                  p.policy_bucket,
                  p.risk_level,
                  COUNT(*) AS policy_items,
                  COUNT(d.id) AS reviewed_items,
                  SUM(CASE WHEN d.approved_for_apply = 1 THEN 1 ELSE 0 END) AS approved_for_apply,
                  ROUND(100.0 * COUNT(d.id) / NULLIF(COUNT(*), 0), 2) AS review_coverage_pct
                FROM policy_items p
                LEFT JOIN segment_token_policy_decisions d
                  ON d.policy_run_id = ?
                 AND d.policy_item_id = p.id
                GROUP BY p.policy_bucket, p.risk_level
                ORDER BY review_coverage_pct DESC, policy_items DESC
                LIMIT 200
                """,
                (latest_policy_id, latest_policy_id),
            )
            return {
                "available": True,
                "summary": summary,
                "runs": decision_runs,
                "byBucket": by_bucket,
                "coverage": coverage,
            }

        if not (_table_exists(con, "segment_token_policy_runs") and _table_exists(con, "segment_token_policy_items")):
            return {
                "available": False,
                "summary": {},
                "runs": [],
                "bucketDistribution": [],
                "packageBuckets": [],
                "reviewQueue": [],
                "decisions": decision_payload(),
                "overlay": overlay_payload(),
                "checkpoints": checkpoint_payload(),
            }

        runs = _all(
            con,
            """
            SELECT
              id AS token_policy_run_id,
              rule_version,
              state_run_id,
              total_candidates,
              critical_count,
              high_count,
              medium_count,
              low_count,
              manual_review_count,
              policy_candidate_count,
              blocked_count,
              report_path,
              csv_path,
              jsonl_path,
              started_at,
              finished_at
            FROM segment_token_policy_runs
            WHERE finished_at IS NOT NULL
            ORDER BY finished_at DESC, id DESC
            LIMIT 20
            """,
        )
        summary = runs[0] if runs else {}
        bucket_distribution = _all(
            con,
            """
            WITH latest AS (
              SELECT id
              FROM segment_token_policy_runs
              WHERE finished_at IS NOT NULL
                AND total_candidates > 0
              ORDER BY finished_at DESC, id DESC
              LIMIT 1
            )
            SELECT
              policy_bucket,
              risk_level,
              review_state,
              COUNT(*) AS total,
              SUM(CASE WHEN auto_apply_allowed = 1 THEN 1 ELSE 0 END) AS auto_apply_allowed_count,
              SUM(CASE WHEN needs_human_review = 1 THEN 1 ELSE 0 END) AS needs_human_review_count
            FROM segment_token_policy_items
            WHERE run_id = (SELECT id FROM latest)
            GROUP BY policy_bucket, risk_level, review_state
            ORDER BY
              CASE risk_level
                WHEN 'critical' THEN 0
                WHEN 'high' THEN 1
                WHEN 'medium' THEN 2
                WHEN 'low' THEN 3
                ELSE 9
              END,
              total DESC,
              policy_bucket
            """,
        )
        package_buckets = _all(
            con,
            """
            WITH latest AS (
              SELECT id
              FROM segment_token_policy_runs
              WHERE finished_at IS NOT NULL
                AND total_candidates > 0
              ORDER BY finished_at DESC, id DESC
              LIMIT 1
            )
            SELECT
              substr(relative_path, 1, instr(relative_path || '/', '/') - 1) AS package_name,
              policy_bucket,
              risk_level,
              COUNT(*) AS total
            FROM segment_token_policy_items
            WHERE run_id = (SELECT id FROM latest)
            GROUP BY package_name, policy_bucket, risk_level
            ORDER BY total DESC, package_name, policy_bucket
            LIMIT 200
            """,
        )
        review_queue = _all(
            con,
            """
            WITH latest AS (
              SELECT id
              FROM segment_token_policy_runs
              WHERE finished_at IS NOT NULL
                AND total_candidates > 0
              ORDER BY finished_at DESC, id DESC
              LIMIT 1
            ),
            latest_confirmation AS (
              SELECT *
              FROM (
                SELECT
                  sc.*,
                  ROW_NUMBER() OVER (PARTITION BY sc.segment_id ORDER BY sc.updated_at DESC, sc.id DESC) AS rn
                FROM segment_confirmations sc
              )
              WHERE rn = 1
            )
            SELECT
              i.id AS policy_item_id,
              i.segment_id,
              i.relative_path,
              i.source_line_number,
              i.source_key,
              i.review_state,
              i.diff_kind,
              i.policy_bucket,
              i.risk_level,
              i.recommendation,
              i.missing_tokens_json,
              i.extra_tokens_json,
              i.issue_flags_json,
              s.english_text,
              s.spanish_text,
              s.old_text,
              o.portuguese_text AS output_text,
              sc.confirmed_text
            FROM segment_token_policy_items i
            JOIN source_segments s ON s.id = i.segment_id
            LEFT JOIN output_segments o ON o.segment_id = i.segment_id
            LEFT JOIN latest_confirmation sc ON sc.segment_id = i.segment_id
            WHERE i.run_id = (SELECT id FROM latest)
            ORDER BY
              CASE i.risk_level
                WHEN 'critical' THEN 0
                WHEN 'high' THEN 1
                WHEN 'medium' THEN 2
                WHEN 'low' THEN 3
                ELSE 9
              END,
              i.policy_bucket,
              i.relative_path,
              i.source_line_number
            LIMIT 300
            """,
        )
        return {
            "available": True,
            "summary": summary,
            "runs": runs,
            "bucketDistribution": bucket_distribution,
            "packageBuckets": package_buckets,
            "reviewQueue": review_queue,
            "decisions": decision_payload(),
            "overlay": overlay_payload(),
            "checkpoints": checkpoint_payload(),
        }

    if not (_table_exists(con, "segment_state_runs") and _table_exists(con, "segment_state_items")):
        return {
            "available": False,
            "summary": {},
            "stateDistribution": [],
            "outputApplication": [],
            "packageBacklog": [],
            "applyQueue": [],
            "reopenQueue": [],
            "outputApply": output_apply_payload(),
            "tokenPolicy": token_policy_payload(),
        }

    summary = _one(
        con,
        """
        WITH latest AS (
          SELECT id
          FROM segment_state_runs
          WHERE total_segments > 1000
          ORDER BY finished_at DESC, id DESC
          LIMIT 1
        )
        SELECT
          r.id AS run_id,
          r.rule_version,
          r.active_score_run_id,
          r.candidate_score_run_id,
          r.policy_run_id,
          r.total_segments,
          r.closed_count,
          r.pending_count,
          ROUND(100.0 * r.closed_count / NULLIF(r.total_segments, 0), 2) AS closed_pct,
          ROUND(100.0 * r.pending_count / NULLIF(r.total_segments, 0), 2) AS pending_pct,
          r.output_apply_pending_count,
          r.blank_valid_count,
          r.reopen_count,
          r.finished_at
        FROM segment_state_runs r
        WHERE r.id = (SELECT id FROM latest)
        """,
    )
    run_id = summary.get("run_id")
    if not run_id:
        return {
            "available": False,
            "summary": {},
            "stateDistribution": [],
            "outputApplication": [],
            "packageBacklog": [],
            "applyQueue": [],
            "reopenQueue": [],
            "outputApply": output_apply_payload(),
            "tokenPolicy": token_policy_payload(),
        }
    taxonomy = _pending_taxonomy_payload(con, _int(run_id))

    state_distribution = _all(
        con,
        """
        SELECT
          state_group,
          final_state,
          COUNT(*) AS total,
          ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct_total
        FROM segment_state_items
        WHERE run_id = ?
        GROUP BY state_group, final_state
        ORDER BY state_group, total DESC
        """,
        (run_id,),
    )
    output_application = _all(
        con,
        """
        SELECT
          output_state,
          apply_state,
          review_state,
          COUNT(*) AS total
        FROM segment_state_items
        WHERE run_id = ?
        GROUP BY output_state, apply_state, review_state
        ORDER BY total DESC
        """,
        (run_id,),
    )
    package_backlog = _all(
        con,
        """
        SELECT
          substr(relative_path, 1, instr(relative_path || '/', '/') - 1) AS package_name,
          COUNT(*) AS total,
          SUM(CASE WHEN state_group = 'closed' THEN 1 ELSE 0 END) AS closed_count,
          SUM(CASE WHEN state_group = 'pending' THEN 1 ELSE 0 END) AS pending_count,
          SUM(CASE WHEN needs_output_apply = 1 THEN 1 ELSE 0 END) AS needs_apply_count,
          SUM(CASE WHEN needs_reopen = 1 THEN 1 ELSE 0 END) AS reopen_count,
          ROUND(100.0 * SUM(CASE WHEN state_group = 'closed' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 2) AS closed_pct
        FROM segment_state_items
        WHERE run_id = ?
        GROUP BY package_name
        ORDER BY pending_count DESC, needs_apply_count DESC, package_name
        LIMIT 40
        """,
        (run_id,),
    )
    apply_queue = _all(
        con,
        """
        SELECT
          segment_id,
          relative_path,
          source_line_number,
          source_key,
          final_state,
          review_state,
          priority_score,
          reasons_json
        FROM segment_state_items
        WHERE run_id = ?
          AND needs_output_apply = 1
        ORDER BY priority_score DESC, relative_path, source_line_number
        LIMIT 200
        """,
        (run_id,),
    )
    reopen_queue = _all(
        con,
        """
        SELECT
          segment_id,
          relative_path,
          source_line_number,
          source_key,
          final_state,
          review_state,
          active_action,
          candidate_action,
          policy_action,
          priority_score,
          reasons_json
        FROM segment_state_items
        WHERE run_id = ?
          AND needs_reopen = 1
        ORDER BY priority_score DESC, relative_path, source_line_number
        LIMIT 200
        """,
        (run_id,),
    )

    grouped = taxonomy.get("distribution") or [
        {"name": "Consolidado", "value": _int(summary.get("closed_count")), "group": "closed_consolidated", "color": "#10b981"},
        {"name": "Pendente bruto", "value": _int(summary.get("pending_count")), "group": "raw_pending", "color": "#f59e0b"},
    ]
    return {
        "available": True,
        "summary": summary,
        "taxonomy": taxonomy,
        "groupDistribution": grouped,
        "stateDistribution": state_distribution,
        "outputApplication": output_application,
        "packageBacklog": package_backlog,
        "applyQueue": apply_queue,
        "reopenQueue": reopen_queue,
        "outputApply": output_apply_payload(),
        "tokenPolicy": token_policy_payload(),
    }


def _dashboard_payload(db_path: Path) -> dict[str, Any]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        segment_counts = _one(
            con,
            """
            SELECT
              COUNT(*) AS active_segments,
              SUM(CASE WHEN has_english = 1 THEN 1 ELSE 0 END) AS with_english,
              SUM(CASE WHEN has_old = 1 THEN 1 ELSE 0 END) AS with_old
            FROM source_segments
            WHERE is_active = 1
            """,
        )
        output_counts = _one(
            con,
            """
            SELECT
              SUM(CASE WHEN COALESCE(o.portuguese_text, '') <> '' THEN 1 ELSE 0 END) AS with_output,
              SUM(CASE WHEN COALESCE(o.portuguese_text, '') = '' THEN 1 ELSE 0 END) AS without_output
            FROM source_segments s
            LEFT JOIN output_segments o ON o.segment_id = s.id
            WHERE s.is_active = 1
            """,
        )
        confirmation_counts = _one(
            con,
            """
            SELECT
              COUNT(DISTINCT sc.segment_id) AS total,
              COUNT(DISTINCT CASE WHEN sc.locked = 1 THEN sc.segment_id END) AS locked,
              COUNT(DISTINCT CASE WHEN sc.locked = 1 AND sc.confirmation_level IN ('human_confirmed', 'human') THEN sc.segment_id END) AS locked_human,
              COUNT(DISTINCT CASE WHEN sc.confirmation_level = 'auto_confirmed' THEN sc.segment_id END) AS auto_confirmed,
              COUNT(DISTINCT CASE WHEN sc.locked = 0 AND sc.confirmation_level IN ('human_confirmed', 'human') THEN sc.segment_id END) AS human_unlocked
            FROM segment_confirmations sc
            JOIN source_segments s ON s.id = sc.segment_id
            WHERE s.is_active = 1
            """,
        )
        review_counts = _one(
            con,
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN human_label = 'pending' THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN human_label <> 'pending' THEN 1 ELSE 0 END) AS reviewed
            FROM local_learning_candidates
            """,
        )
        issue_counts = _one(
            con,
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN severity IN ('high', 'critical') THEN 1 ELSE 0 END) AS high
            FROM issues
            """,
        )

        latest_score = _latest_score(con, operational=True) or _latest_score(con)
        previous_score = _latest_score(con, 1, operational=True) or _latest_score(con, 1)
        active_model = _active_model(con)
        latest_model = _latest_model(con)
        latest_dataset = _latest_dataset(con)

        scored = _int(latest_score.get("scored_count"))
        auto_safe = _int(latest_score.get("final_auto_safe_count"))
        pending = (
            _int(latest_score.get("needs_human_count"))
            + _int(latest_score.get("needs_autofix_count"))
            + _int(latest_score.get("blocked_structure_count"))
        )
        auto_safe_pct = _pct(auto_safe, scored)
        previous_auto_safe_pct = _pct(
            previous_score.get("final_auto_safe_count"),
            previous_score.get("scored_count"),
        )
        latest_score_id = _int(latest_score.get("id"))

        model_rows = _all(
            con,
            """
            SELECT id, model_version, macro_f1, safe_precision, safe_recall, false_safe_count, predicted_safe_count, started_at
            FROM ml_model_runs
            WHERE model_kind = 'risk_action_classifier'
            ORDER BY id DESC
            LIMIT 12
            """,
        )
        model_rows.reverse()
        ml_trend = [
            {
                "model": _run_axis_label(row["id"]),
                "modelVersion": row.get("model_version"),
                "modelLabel": _short_model_name(row.get("model_version")),
                "runId": row["id"],
                "macroF1": round(_num(row.get("macro_f1")), 4),
                "safePrecision": round(_num(row.get("safe_precision")), 4),
                "safeRecall": round(_num(row.get("safe_recall")), 4),
                "falseSafe": _int(row.get("false_safe_count")),
                "predictedSafe": _int(row.get("predicted_safe_count")),
                "startedAt": row.get("started_at"),
            }
            for row in model_rows
        ]
        latest_by_model: dict[str, dict[str, Any]] = {}
        for row in ml_trend:
            key = row.get("modelLabel") or row.get("modelVersion") or str(row.get("runId"))
            if key not in latest_by_model or _int(row.get("runId")) > _int(latest_by_model[key].get("runId")):
                latest_by_model[key] = row
        ml_trend_by_model = [
            {
                **row,
                "model": row.get("modelLabel") or row.get("model"),
            }
            for row in sorted(latest_by_model.values(), key=lambda item: _int(item.get("runId")))
        ]

        score_rows = _all(
            con,
            """
            SELECT id, model_run_id, scored_count, final_auto_safe_count, needs_human_count, needs_autofix_count, blocked_structure_count, started_at
            FROM ml_score_runs
            WHERE scored_count >= 10000
            ORDER BY id DESC
            LIMIT 10
            """,
        )
        score_rows.reverse()
        score_model_rows = _all(
            con,
            """
            SELECT id, model_version, macro_f1
            FROM ml_model_runs
            WHERE id IN (
              SELECT DISTINCT model_run_id
              FROM ml_score_runs
              WHERE scored_count >= 10000
            )
            """,
        )
        score_model_by_id = {row["id"]: row for row in score_model_rows}
        quality_trend = []
        for row in score_rows:
            score_pending = _int(row.get("needs_human_count")) + _int(row.get("needs_autofix_count")) + _int(row.get("blocked_structure_count"))
            current_model = score_model_by_id.get(_int(row.get("model_run_id")), {})
            quality_index = round(_num(current_model.get("macro_f1")) * 100, 2) if current_model else 0
            quality_trend.append(
                {
                    "run": f"R{row['id']}",
                    "runLabel": _run_axis_label(row["id"]),
                    "modelRunId": row.get("model_run_id"),
                    "modelVersion": current_model.get("model_version"),
                    "qualityIndex": quality_index,
                    "autoSafe": _pct(row.get("final_auto_safe_count"), row.get("scored_count")),
                    "pending": score_pending,
                }
            )
        latest_score_by_model: dict[str, dict[str, Any]] = {}
        for row in quality_trend:
            key = _short_model_name(row.get("modelVersion")) or str(row.get("modelRunId"))
            if key not in latest_score_by_model or _int(row.get("modelRunId")) > _int(latest_score_by_model[key].get("modelRunId")):
                latest_score_by_model[key] = row
        quality_trend_by_model = [
            {
                **row,
                "runLabel": _short_model_name(row.get("modelVersion")),
            }
            for row in sorted(latest_score_by_model.values(), key=lambda item: _int(item.get("modelRunId")))
        ]

        dataset_composition = [
            {"label": "Positivos", "value": _int(latest_dataset.get("positive_count"))},
            {"label": "Negativos", "value": _int(latest_dataset.get("negative_count"))},
            {"label": "Neutros", "value": _int(latest_dataset.get("neutral_count"))},
            {"label": "Strong +", "value": _int(latest_dataset.get("strong_positive_count"))},
            {"label": "Strong -", "value": _int(latest_dataset.get("strong_negative_count"))},
        ]

        segment_distribution = [
            {"name": "Auto-safe", "value": auto_safe, "color": "#10b981"},
            {"name": "Revisao humana", "value": _int(latest_score.get("needs_human_count")), "color": "#f59e0b"},
            {"name": "Autofix", "value": _int(latest_score.get("needs_autofix_count")), "color": "#3b82f6"},
            {"name": "Bloqueio estrutural", "value": _int(latest_score.get("blocked_structure_count")), "color": "#ef4444"},
        ]

        pipeline_status = [
            {"status": "Sem output", "count": _int(output_counts.get("without_output"))},
            {
                "status": "Output nao confirmado",
                "count": max(_int(output_counts.get("with_output")) - _int(confirmation_counts.get("total")), 0),
            },
            {"status": "Confirmado automatico", "count": _int(confirmation_counts.get("auto_confirmed"))},
            {"status": "Confirmado humano", "count": _int(confirmation_counts.get("human_unlocked"))},
            {"status": "Locked humano", "count": _int(confirmation_counts.get("locked_human"))},
        ]

        package_backlog = _all(
            con,
            """
            SELECT
              relative_path AS file,
              SUM(CASE WHEN final_action <> 'auto_safe' THEN 1 ELSE 0 END) AS pending
            FROM ml_score_items
            WHERE run_id = ?
            GROUP BY relative_path
            HAVING pending > 0
            ORDER BY pending DESC
            LIMIT 8
            """,
            (latest_score_id,),
        )

        human_reviews = _all(
            con,
            """
            SELECT
              DATE(COALESCE(reviewed_at, updated_at, created_at)) AS day,
              SUM(CASE WHEN human_label = 'correct' THEN 1 ELSE 0 END) AS correct,
              SUM(CASE WHEN human_label = 'minor_fix' THEN 1 ELSE 0 END) AS minorFix,
              SUM(CASE WHEN human_label = 'semantic_error' THEN 1 ELSE 0 END) AS semanticError,
              SUM(CASE WHEN human_label = 'residual_spanish' THEN 1 ELSE 0 END) AS residualSpanish
            FROM local_learning_candidates
            WHERE human_label <> 'pending'
            GROUP BY DATE(COALESCE(reviewed_at, updated_at, created_at))
            ORDER BY day DESC
            LIMIT 7
            """,
        )
        human_reviews.reverse()

        promotion_rows = _all(
            con,
            """
            SELECT
              p.id,
              p.candidate_model_run_id,
              COALESCE(m.model_version, 'modelo ' || p.candidate_model_run_id) AS model_version,
              p.decision,
              COALESCE(m.false_safe_count, 0) AS false_safe_count,
              COALESCE(m.safe_recall, 0) AS safe_recall,
              COALESCE(m.safe_precision, 0) AS safe_precision
            FROM ml_model_promotions p
            LEFT JOIN ml_model_runs m ON m.id = p.candidate_model_run_id
            ORDER BY p.id DESC
            LIMIT 10
            """,
        )
        promotion_rows.reverse()
        promotion_timeline = [
            {
                "model": _run_axis_label(row["candidate_model_run_id"]),
                "modelVersion": row["model_version"],
                "decision": "Promovido" if row["decision"] == "promote" else "Rejeitado",
                "risk": _int(row["false_safe_count"]),
                "holdoutCoverage": round(_num(row.get("safe_recall")) * 100, 2),
                "safePrecision": round(_num(row.get("safe_precision")) * 100, 2),
            }
            for row in promotion_rows
        ]

        block_reasons = _all(
            con,
            """
            SELECT issue_type AS reason, COUNT(*) AS count
            FROM issues
            GROUP BY issue_type
            ORDER BY count DESC
            LIMIT 8
            """,
        )
        if not block_reasons:
            block_reasons = _all(
                con,
                """
                SELECT reason, SUM(count) AS count
                FROM (
                  SELECT 'Bloqueio estrutural' AS reason, COUNT(*) AS count
                  FROM ml_score_items
                  WHERE run_id = ? AND final_action = 'blocked_structure'
                  UNION ALL
                  SELECT 'Bloqueio determinístico' AS reason, COUNT(*) AS count
                  FROM ml_score_items
                  WHERE run_id = ? AND deterministic_blocked = 1
                  UNION ALL
                  SELECT 'Token diferente de ok' AS reason, COUNT(*) AS count
                  FROM ml_score_items
                  WHERE run_id = ? AND COALESCE(token_status, 'ok') <> 'ok'
                )
                GROUP BY reason
                HAVING count > 0
                ORDER BY count DESC
                """,
                (latest_score_id, latest_score_id, latest_score_id),
            )

        confirmation_sources = _all(
            con,
            """
            SELECT
              COALESCE(confirmation_source, confirmation_level, 'desconhecido') AS name,
              COUNT(*) AS value
            FROM segment_confirmations
            GROUP BY COALESCE(confirmation_source, confirmation_level, 'desconhecido')
            ORDER BY value DESC
            LIMIT 6
            """,
        )
        palette = ["#10b981", "#8b5cf6", "#3b82f6", "#f59e0b", "#14b8a6", "#ef4444"]
        for index, row in enumerate(confirmation_sources):
            row["color"] = palette[index % len(palette)]

        model_total_count = _int(
            _one(
                con,
                "SELECT COUNT(*) AS total FROM ml_model_runs WHERE model_kind = 'risk_action_classifier'",
            ).get("total")
        )
        promoted_count = _int(
            _one(
                con,
                """
                WITH latest_decision AS (
                  SELECT p.*
                  FROM ml_model_promotions p
                  JOIN (
                    SELECT candidate_model_run_id, MAX(id) AS latest_id
                    FROM ml_model_promotions
                    GROUP BY candidate_model_run_id
                  ) latest ON latest.latest_id = p.id
                )
                SELECT COUNT(*) AS total
                FROM latest_decision
                WHERE decision = 'promote'
                """,
            ).get("total")
        )
        rejected_count = _int(
            _one(
                con,
                """
                WITH latest_decision AS (
                  SELECT p.*
                  FROM ml_model_promotions p
                  JOIN (
                    SELECT candidate_model_run_id, MAX(id) AS latest_id
                    FROM ml_model_promotions
                    GROUP BY candidate_model_run_id
                  ) latest ON latest.latest_id = p.id
                )
                SELECT COUNT(*) AS total
                FROM latest_decision
                WHERE decision <> 'promote'
                """,
            ).get("total")
        )
        evaluated_model_count = _int(
            _one(
                con,
                "SELECT COUNT(DISTINCT candidate_model_run_id) AS total FROM ml_model_promotions",
            ).get("total")
        )
        unevaluated_model_count = max(model_total_count - evaluated_model_count, 0)

        active_precision = _num(active_model.get("safe_precision"))
        latest_precision = _num(latest_model.get("safe_precision"))
        candidate_delta = latest_precision - active_precision
        model_comparison = [
            {"metric": "Accuracy", "current": round(_num(active_model.get("accuracy")), 4), "candidate": round(_num(latest_model.get("accuracy")), 4), "format": "percent"},
            {"metric": "Macro F1", "current": round(_num(active_model.get("macro_f1")), 4), "candidate": round(_num(latest_model.get("macro_f1")), 4)},
            {"metric": "Safe Precision", "current": round(active_precision, 4), "candidate": round(latest_precision, 4)},
            {"metric": "Holdout Coverage", "current": round(_num(active_model.get("safe_recall")), 4), "candidate": round(_num(latest_model.get("safe_recall")), 4), "format": "percent"},
            {"metric": "False Safe", "current": _int(active_model.get("false_safe_count")), "candidate": _int(latest_model.get("false_safe_count"))},
            {"metric": "Predicted Safe", "current": _int(active_model.get("predicted_safe_count")), "candidate": _int(latest_model.get("predicted_safe_count"))},
        ]

        return {
            "generatedAt": datetime.now().isoformat(timespec="seconds"),
            "databasePath": str(db_path),
            "cockpit": {
                "kpis": {
                    "activeSegments": _int(segment_counts.get("active_segments")),
                    "outputCoverage": _pct(output_counts.get("with_output"), segment_counts.get("active_segments")),
                    "autoSafeEfficiency": auto_safe_pct,
                    "autoSafeDelta": round(auto_safe_pct - previous_auto_safe_pct, 2),
                    "pendingReview": pending,
                },
                "qualityTrend": quality_trend,
                "qualityTrendByModel": quality_trend_by_model,
                "segmentDistribution": segment_distribution,
                "status": {
                    "activeModel": active_model.get("active_model_version"),
                    "activeModelRunId": active_model.get("active_model_run_id"),
                    "latestScoreRunId": latest_score.get("id"),
                    "latestDatasetRunId": latest_dataset.get("id"),
                },
            },
            "mlPerformance": {
                "kpis": {
                    "activeModelShort": _short_model_name(active_model.get("active_model_version")),
                    "activeModel": active_model.get("active_model_version"),
                    "macroF1": round(_num(active_model.get("macro_f1")), 4),
                    "safePrecision": round(active_precision, 4),
                    "holdoutCoverage": round(_num(active_model.get("safe_recall")), 4),
                    "negativeCoverage": _pct(latest_dataset.get("negative_count"), latest_dataset.get("total_count")),
                },
                "mlTrend": ml_trend,
                "mlTrendByModel": ml_trend_by_model,
                "datasetComposition": dataset_composition,
                "modelComparison": model_comparison,
                "candidateDecision": "promote" if latest_model.get("id") == active_model.get("active_model_run_id") else "review",
                "candidateDeltaSafePrecision": round(candidate_delta, 4),
            },
            "pipeline": {
                "kpis": {
                    "segmentsTotal": _int(segment_counts.get("active_segments")),
                    "withOutput": _int(output_counts.get("with_output")),
                    "withoutOutput": _int(output_counts.get("without_output")),
                    "lockedHuman": _int(confirmation_counts.get("locked_human")),
                    "confirmed": _int(confirmation_counts.get("total")),
                    "pendingReview": _int(review_counts.get("pending")),
                    "structuralIssues": _int(issue_counts.get("high")),
                    "autofix": _int(latest_score.get("needs_autofix_count")),
                },
                "pipelineStatus": pipeline_status,
                "funnelData": [
                    {"step": "Source", "value": _int(segment_counts.get("active_segments"))},
                    {"step": "Output", "value": _int(output_counts.get("with_output"))},
                    {"step": "Analisado", "value": _int(_one(con, "SELECT COUNT(DISTINCT segment_id) AS total FROM segment_analysis").get("total"))},
                    {"step": "Scored ML", "value": scored},
                    {"step": "Confirmado", "value": _int(confirmation_counts.get("total"))},
                    {"step": "Locked", "value": _int(confirmation_counts.get("locked"))},
                ],
                "packageBacklog": package_backlog,
                "humanReviews": human_reviews,
            },
            "governance": {
                "kpis": {
                    "lockedHuman": _int(confirmation_counts.get("locked_human")),
                    "blockedStructure": _int(latest_score.get("blocked_structure_count")),
                    "tokenIssues": _int(_one(con, "SELECT COUNT(*) AS total FROM issues WHERE issue_type LIKE '%token%' OR issue_type LIKE '%placeholder%'").get("total")),
                    "falseSafeHoldout": _int(active_model.get("false_safe_count")),
                    "totalModels": model_total_count,
                    "rejectedModels": rejected_count,
                    "lastPromotion": _short_model_name(active_model.get("active_model_version")),
                },
                "promotionTimeline": promotion_timeline,
                "blockReasons": block_reasons,
                "confirmationSources": confirmation_sources,
                "policy": [
                    {"title": "Threshold seguro atual", "value": f"{round(_num(active_model.get('safe_threshold')) * 100, 1)}%"},
                    {"title": "Promocao de modelo", "value": "False safe precisa ser 0"},
                    {"title": "Locked humano", "value": "Nunca sobrescrever automaticamente"},
                    {"title": "Estrutura e tokens", "value": "Prioridade sobre ML"},
                ],
                "modelSnapshot": {
                    "active": {
                        "label": "Modelo ativo/promovido",
                        "runId": active_model.get("active_model_run_id"),
                        "version": active_model.get("active_model_version"),
                        "accuracy": round(_num(active_model.get("accuracy")), 4),
                        "macroF1": round(_num(active_model.get("macro_f1")), 4),
                        "safePrecision": round(active_precision, 4),
                        "safeRecall": round(_num(active_model.get("safe_recall")), 4),
                        "holdoutCoverage": round(_num(active_model.get("safe_recall")), 4),
                        "negativeCoverage": _dataset_negative_coverage(con, active_model.get("dataset_run_id")),
                    },
                    "latest": {
                        "label": "Último modelo treinado",
                        "runId": latest_model.get("id"),
                        "version": latest_model.get("model_version"),
                        "accuracy": round(_num(latest_model.get("accuracy")), 4),
                        "macroF1": round(_num(latest_model.get("macro_f1")), 4),
                        "safePrecision": round(latest_precision, 4),
                        "safeRecall": round(_num(latest_model.get("safe_recall")), 4),
                        "holdoutCoverage": round(_num(latest_model.get("safe_recall")), 4),
                        "negativeCoverage": _dataset_negative_coverage(con, latest_model.get("dataset_run_id")),
                    },
                },
            },
            "policy": _policy_payload(con),
            "lab": _lab_payload(con),
            "specialists": _specialists_payload(con),
            "agents": _agents_payload(con),
            "lifecycle": _lifecycle_payload(con),
            "learning": _learning_status_payload(con),
            "production": _production_payload(con),
        }
    finally:
        con.close()


class DashboardHandler(BaseHTTPRequestHandler):
    db_path: Path = DEFAULT_DB

    def _send_html(self, status: int, html: str) -> None:
        data = html.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError as exc:
            if _is_client_disconnect(exc):
                return
            raise

    def _send_json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(data)
        except OSError as exc:
            if _is_client_disconnect(exc):
                return
            raise

    def do_OPTIONS(self) -> None:
        self._send_json(204, {})

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("", "/", "/api"):
            self._send_html(
                200,
                """
                <!doctype html>
                <html lang="pt-BR">
                  <head>
                    <meta charset="utf-8" />
                    <title>CK3 PT-BR Dashboard API</title>
                    <style>
                      body { font-family: system-ui, sans-serif; margin: 40px; line-height: 1.5; }
                      code { background: #f1f5f9; padding: 2px 6px; border-radius: 4px; }
                    </style>
                  </head>
                  <body>
                    <h1>CK3 PT-BR Dashboard API</h1>
                    <p>Este servidor é apenas o backend de dados.</p>
                    <p>Abra o dashboard visual em <code>http://127.0.0.1:5173</code> depois de iniciar o frontend.</p>
                    <p>Endpoint JSON: <a href="/api/dashboard">/api/dashboard</a></p>
                    <p>Health check: <a href="/api/health">/api/health</a></p>
                  </body>
                </html>
                """,
            )
            return
        if path == "/api/health":
            self._send_json(200, {"ok": self.db_path.exists(), "databasePath": str(self.db_path)})
            return
        if path == "/api/dashboard":
            if not self.db_path.exists():
                self._send_json(500, {"error": f"SQLite not found: {self.db_path}"})
                return
            try:
                self._send_json(200, _dashboard_payload(self.db_path))
            except Exception as exc:  # pragma: no cover - server diagnostic path
                self._send_json(500, {"error": str(exc)})
            return
        if path == "/api/app-state":
            if not self.db_path.exists():
                self._send_json(500, {"error": f"SQLite not found: {self.db_path}"})
                return
            try:
                self._send_json(200, _app_state_payload(self.db_path))
            except Exception as exc:  # pragma: no cover - server diagnostic path
                self._send_json(500, {"error": str(exc)})
            return
        if path == "/api/neural-visualization":
            if not NEURAL_VISUALIZATION_FILE.exists():
                self._send_json(404, {"error": f"Neural visualization file not found: {NEURAL_VISUALIZATION_FILE}"})
                return
            try:
                registry_summary: dict[str, Any] = {}
                current_segment_state: dict[str, Any] = {}
                if self.db_path.exists():
                    con = sqlite3.connect(self.db_path)
                    con.row_factory = sqlite3.Row
                    try:
                        registry_summary = _neural_registry_summary_payload(con)
                        current_segment_state = _latest_segment_state_summary(con)
                    finally:
                        con.close()
                self._send_json(
                    200,
                    {
                        "network": json.loads(NEURAL_VISUALIZATION_FILE.read_text(encoding="utf-8")),
                        "sourcePath": str(NEURAL_VISUALIZATION_FILE),
                        "registrySummary": registry_summary,
                        "currentSegmentState": current_segment_state,
                    },
                )
            except Exception as exc:  # pragma: no cover - server diagnostic path
                self._send_json(500, {"error": str(exc)})
            return
        if path == "/api/production/status":
            if not self.db_path.exists():
                self._send_json(500, {"error": f"SQLite not found: {self.db_path}"})
                return
            try:
                con = sqlite3.connect(self.db_path)
                con.row_factory = sqlite3.Row
                try:
                    self._send_json(200, {"production": _production_payload(con)})
                finally:
                    con.close()
            except Exception as exc:  # pragma: no cover - server diagnostic path
                self._send_json(500, {"error": str(exc)})
            return
        if path == "/api/production/preflight/disk":
            try:
                self._send_json(200, {"disk_preflight": _production_disk_preflight_payload()})
            except Exception as exc:  # pragma: no cover - server diagnostic path
                self._send_json(500, {"error": str(exc)})
            return
        if path in ("/api/production/runs/latest", "/api/production/run/latest"):
            self._send_json(200, {"run": _read_production_run_status()})
            return
        if path == "/api/learning/status":
            if not self.db_path.exists():
                self._send_json(500, {"error": f"SQLite not found: {self.db_path}"})
                return
            try:
                con = sqlite3.connect(self.db_path)
                con.row_factory = sqlite3.Row
                try:
                    self._send_json(200, {"learning": _learning_status_payload(con)})
                finally:
                    con.close()
            except Exception as exc:  # pragma: no cover - server diagnostic path
                self._send_json(500, {"error": str(exc)})
            return
        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/cache/refresh":
            if not self.db_path.exists():
                self._send_json(500, {"error": f"SQLite not found: {self.db_path}"})
                return
            try:
                segment_state_refresh = _run_diagnostic_segment_state(self.db_path)
                if segment_state_refresh.get("exit_code") != 0:
                    message = "\n".join(segment_state_refresh.get("stderr_tail") or segment_state_refresh.get("stdout_tail") or [])
                    raise RuntimeError(message or "segment-state diagnostic refresh failed")
                cache = _get_dashboard_cache(self.db_path, force=True)
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "cache": cache.get("app_state", {}).get("cache", {}),
                        "app_state": cache.get("app_state", {}),
                        "diagnostic_segment_state": segment_state_refresh,
                    },
                )
            except Exception as exc:  # pragma: no cover - server diagnostic path
                self._send_json(500, {"ok": False, "error": str(exc)})
            return
        if path == "/api/production/start":
            if not self.db_path.exists():
                self._send_json(500, {"error": f"SQLite not found: {self.db_path}"})
                return
            con = sqlite3.connect(self.db_path)
            con.row_factory = sqlite3.Row
            try:
                learning = _learning_status_payload(con)
                lock = learning.get("lock") or _training_lock_payload(con)
                if not learning.get("can_start_production"):
                    self._send_json(
                        423,
                        {
                            "accepted": False,
                            "status": "blocked",
                            "lock": lock,
                            "learning": learning,
                        },
                    )
                    return
                segment_state = _latest_segment_state_summary(con)
                post_release_status = _post_release_feedback_payload(segment_state)
                safety_status = _safe_full_production_payload(post_release_status, segment_state)
                evaluation_gate = safety_status.get("evaluation_gate", {})
                if evaluation_gate.get("can_start_evaluation_full_production_now") is not True:
                    self._send_json(
                        423,
                        {
                            "accepted": False,
                            "status": "blocked",
                            "gate": "evaluation",
                            "evaluation_gate": evaluation_gate,
                            "message": "Production evaluation is blocked by safety preflight.",
                        },
                    )
                    return
                disk_preflight = _production_disk_preflight_payload()
                if disk_preflight.get("ok") is not True:
                    self._send_json(
                        507,
                        {
                            "accepted": False,
                            "status": "blocked",
                            "gate": "disk_preflight",
                            "error": "Insufficient disk space",
                            "message": disk_preflight.get("message"),
                            "disk_preflight": disk_preflight,
                        },
                    )
                    return
                result = _start_production_run()
                self._send_json(202 if result.get("accepted") else 409, result)
            finally:
                con.close()
            return
        if path == "/api/production/publication/preflight":
            if not self.db_path.exists():
                self._send_json(500, {"ok": False, "error": f"SQLite not found: {self.db_path}"})
                return
            try:
                self._send_json(200, _publication_preflight_payload(self.db_path))
            except Exception as exc:  # pragma: no cover - server diagnostic path
                self._send_json(500, {"ok": False, "error": str(exc)})
            return
        if path == "/api/production/publication/apply-confirmed/start":
            if not self.db_path.exists():
                self._send_json(500, {"ok": False, "error": f"SQLite not found: {self.db_path}"})
                return
            con = sqlite3.connect(self.db_path)
            con.row_factory = sqlite3.Row
            try:
                learning = _learning_status_payload(con)
                lock = learning.get("lock") or _training_lock_payload(con)
                if not learning.get("can_start_production"):
                    self._send_json(
                        423,
                        {
                            "accepted": False,
                            "status": "blocked",
                            "lock": lock,
                            "learning": learning,
                        },
                    )
                    return
                preflight_payload = _publication_preflight_payload(self.db_path)
                preflight = preflight_payload.get("publication_preflight") or {}
                if preflight.get("can_apply_pending") is not True:
                    self._send_json(
                        423,
                        {
                            "accepted": False,
                            "status": "blocked",
                            "gate": "publication_apply",
                            "message": "Publication apply is blocked by preflight.",
                            "publication_preflight": preflight,
                        },
                    )
                    return
                result = _start_publication_apply_run()
                result["publication_preflight"] = preflight
                self._send_json(202 if result.get("accepted") else 409, result)
            except Exception as exc:  # pragma: no cover - server diagnostic path
                self._send_json(500, {"ok": False, "error": str(exc)})
            finally:
                con.close()
            return
        self._send_json(404, {"error": "Not found"})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[dashboard] {self.address_string()} - {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="CK3 PT-BR dashboard read-only API.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Path to translation_engine.sqlite")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    global ACTIVE_DB_PATH
    DashboardHandler.db_path = Path(args.db).resolve()
    ACTIVE_DB_PATH = DashboardHandler.db_path
    try:
        if DashboardHandler.db_path.exists():
            _get_dashboard_cache(DashboardHandler.db_path)
    except Exception as exc:
        print(f"Dashboard cache warmup failed: {exc}")
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Dashboard API: http://{args.host}:{args.port}/api/dashboard")
    print(f"SQLite: {DashboardHandler.db_path}")
    server.serve_forever()


if __name__ == "__main__":
    main()
