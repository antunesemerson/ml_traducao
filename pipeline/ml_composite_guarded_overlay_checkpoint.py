from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from segment_token_overlay_review_queue import DEFAULT_ACTIVE_GATE_KEY


RULE_VERSION = "ml_composite_guarded_overlay_checkpoint_v1"
CHECKPOINT_NAME = "composite_pronoun_guarded_overlay_checkpoint"
EXPECTED_OVERLAY_NAME = "composite_pronoun_subpolicy_guarded_overlay"
TARGET_ACTION = "dry_run_release_to_guarded_pronoun_policy"
TARGET_BUCKET = "guarded_pronoun_english_aligned_subpolicy"
TARGET_AGENT = "gender_pronoun_english_aligned_subpolicy"
MIXED_TARGET_ACTION = "dry_run_release_to_guarded_mixed_name_gender_policy"
MIXED_TARGET_BUCKET = "guarded_mixed_name_gender_subpolicy"
MIXED_TARGET_AGENT = "mixed_name_gender_subpolicy"
SELECT_TARGET_ACTION = "dry_run_release_to_guarded_select_cstring_policy"
SELECT_TARGET_BUCKET = "guarded_select_cstring_ui_subpolicy"
SELECT_TARGET_AGENT = "select_cstring_ui_subpolicy"
TOKEN_ADDED_TARGET_ACTION = "dry_run_release_to_guarded_token_added_policy"
TOKEN_ADDED_TARGET_BUCKET = "guarded_token_added_english_reference_subpolicy"
TOKEN_ADDED_TARGET_AGENT = "token_added_english_reference_subpolicy"
GENDER_ARTICLE_TARGET_ACTION = "dry_run_release_to_guarded_gender_article_removal_policy"
GENDER_ARTICLE_TARGET_BUCKET = "guarded_gender_article_removal_subpolicy"
GENDER_ARTICLE_TARGET_AGENT = "gender_article_removal_subpolicy"
GENDER_FORM_SWAP_TARGET_ACTION = "dry_run_release_to_guarded_gender_form_swap_policy"
GENDER_FORM_SWAP_TARGET_BUCKET = "guarded_gender_form_swap_subpolicy"
GENDER_FORM_SWAP_TARGET_AGENT = "gender_form_swap_subpolicy"
PRONOUN_FORM_SWAP_TARGET_ACTION = "dry_run_release_to_guarded_pronoun_form_swap_policy"
PRONOUN_FORM_SWAP_TARGET_BUCKET = "guarded_pronoun_form_swap_subpolicy"
PRONOUN_FORM_SWAP_TARGET_AGENT = "pronoun_form_swap_subpolicy"
NAME_FORM_STYLE_TARGET_ACTION = "dry_run_release_to_guarded_name_form_style_policy"
NAME_FORM_STYLE_TARGET_BUCKET = "guarded_name_form_style_subpolicy"
NAME_FORM_STYLE_TARGET_AGENT = "name_form_style_subpolicy"
TUTORIAL_CONCEPT_TARGET_ACTION = "dry_run_release_to_guarded_tutorial_concept_policy"
TUTORIAL_CONCEPT_TARGET_BUCKET = "guarded_tutorial_concept_exception_subpolicy"
TUTORIAL_CONCEPT_TARGET_AGENT = "tutorial_concept_exception_subpolicy"
TARGET_ACTIONS = {
    TARGET_ACTION,
    MIXED_TARGET_ACTION,
    SELECT_TARGET_ACTION,
    TOKEN_ADDED_TARGET_ACTION,
    GENDER_ARTICLE_TARGET_ACTION,
    GENDER_FORM_SWAP_TARGET_ACTION,
    PRONOUN_FORM_SWAP_TARGET_ACTION,
    NAME_FORM_STYLE_TARGET_ACTION,
    TUTORIAL_CONCEPT_TARGET_ACTION,
}
TARGET_PROFILES = {
    TARGET_ACTION: {
        "original_bucket": "review_gender_token_change",
        "original_risk": "medium",
        "target_bucket": TARGET_BUCKET,
        "target_risk": "low",
        "target_agent": TARGET_AGENT,
        "issue_flags": {"gender_or_pronoun_token_change"},
    },
    MIXED_TARGET_ACTION: {
        "original_bucket": "review_mixed_token_change",
        "original_risk": "high",
        "target_bucket": MIXED_TARGET_BUCKET,
        "target_risk": "low",
        "target_agent": MIXED_TARGET_AGENT,
        "issue_flags": {"mixed_token_change"},
    },
    SELECT_TARGET_ACTION: {
        "original_bucket": "review_select_cstring_change",
        "original_risk": "high",
        "target_bucket": SELECT_TARGET_BUCKET,
        "target_risk": "low",
        "target_agent": SELECT_TARGET_AGENT,
        "issue_flags": {"select_cstring_changed"},
    },
    TOKEN_ADDED_TARGET_ACTION: {
        "original_bucket": "review_token_added",
        "original_risk": "medium",
        "target_bucket": TOKEN_ADDED_TARGET_BUCKET,
        "target_risk": "low",
        "target_agent": TOKEN_ADDED_TARGET_AGENT,
        "issue_flags": {"token_added"},
    },
    GENDER_ARTICLE_TARGET_ACTION: {
        "original_bucket": "review_gender_token_change",
        "original_risk": "medium",
        "target_bucket": GENDER_ARTICLE_TARGET_BUCKET,
        "target_risk": "low",
        "target_agent": GENDER_ARTICLE_TARGET_AGENT,
        "issue_flags": {"gender_or_pronoun_token_change"},
    },
    GENDER_FORM_SWAP_TARGET_ACTION: {
        "original_bucket": "review_gender_token_change",
        "original_risk": "medium",
        "target_bucket": GENDER_FORM_SWAP_TARGET_BUCKET,
        "target_risk": "low",
        "target_agent": GENDER_FORM_SWAP_TARGET_AGENT,
        "issue_flags": {"gender_or_pronoun_token_change"},
    },
    PRONOUN_FORM_SWAP_TARGET_ACTION: {
        "original_bucket": "review_gender_token_change",
        "original_risk": "medium",
        "target_bucket": PRONOUN_FORM_SWAP_TARGET_BUCKET,
        "target_risk": "low",
        "target_agent": PRONOUN_FORM_SWAP_TARGET_AGENT,
        "issue_flags": {"gender_or_pronoun_token_change"},
    },
    NAME_FORM_STYLE_TARGET_ACTION: {
        "original_bucket": "review_mixed_token_change",
        "original_risk": "high",
        "target_bucket": NAME_FORM_STYLE_TARGET_BUCKET,
        "target_risk": "low",
        "target_agent": NAME_FORM_STYLE_TARGET_AGENT,
        "issue_flags": {"mixed_token_change"},
    },
    TUTORIAL_CONCEPT_TARGET_ACTION: {
        "original_bucket": "blocked_variable_or_icon_change",
        "original_risk": "critical",
        "target_bucket": TUTORIAL_CONCEPT_TARGET_BUCKET,
        "target_risk": "high",
        "target_agent": TUTORIAL_CONCEPT_TARGET_AGENT,
        "issue_flags": {"variable_or_icon_changed"},
    },
}
READY_STATUS = "ready_for_guarded_policy_review"
INLINE_QUESTION_RE = re.compile(r"[A-Za-z]\?[A-Za-z]")
ACCENT_QUESTION_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]\?(?=[A-Za-zÀ-ÖØ-öø-ÿ]|-[A-Za-zÀ-ÖØ-öø-ÿ]|\s+[a-zà-öø-ÿ])")
MOJIBAKE_SEQUENCE_RE = re.compile(r"(?:Ã[£©¡³µºª§¨]|Â[^\sA-Za-z0-9]|�)")


def parse_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def parse_json_list(value: str | None) -> list[Any]:
    if not value:
        return []
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return [value]
    return payload if isinstance(payload, list) else [payload]


def latest_guarded_overlay_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_token_policy_overlay_runs
        WHERE overlay_name = ?
          AND finished_at IS NOT NULL
          AND total_candidates > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (EXPECTED_OVERLAY_NAME,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No completed {EXPECTED_OVERLAY_NAME!r} overlay run found.")
    return int(row["id"])


def fetch_overlay_run(conn, overlay_run_id: int) -> dict[str, Any]:
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


def fetch_ready_rules(conn, audit_run_id: int | None) -> list[dict[str, Any]]:
    if audit_run_id is None:
        return []
    rows = conn.execute(
        """
        SELECT *
        FROM ml_composite_subpolicy_promotion_audit_items
        WHERE run_id = ?
          AND promotion_status = ?
        ORDER BY suggested_route, token_subtype, rule_key
        """,
        (audit_run_id, READY_STATUS),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_overlay_rows(conn, overlay_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            oi.*,
            s.english_text,
            s.spanish_text,
            sc.confirmed_text,
            sc.confirmation_level,
            sc.locked,
            i.missing_tokens_json,
            i.extra_tokens_json,
            i.issue_flags_json
        FROM segment_token_policy_overlay_items oi
        JOIN source_segments s ON s.id = oi.segment_id
        JOIN segment_token_policy_items i ON i.id = oi.source_policy_item_id
        LEFT JOIN segment_confirmations sc ON sc.segment_id = oi.segment_id
        WHERE oi.run_id = ?
        ORDER BY
            CASE oi.overlay_risk_level
                WHEN 'critical' THEN 0
                WHEN 'high' THEN 1
                WHEN 'medium' THEN 2
                WHEN 'low' THEN 3
                ELSE 9
            END,
            oi.overlay_policy_bucket,
            oi.relative_path,
            oi.source_line_number
        """,
        (overlay_run_id,),
    ).fetchall()
    enriched: list[dict[str, Any]] = []
    for raw_row in rows:
        row = dict(raw_row)
        row["missing_tokens"] = parse_json_list(row.get("missing_tokens_json"))
        row["extra_tokens"] = parse_json_list(row.get("extra_tokens_json"))
        row["issue_flags"] = parse_json_list(row.get("issue_flags_json"))
        row["reasons"] = parse_json_list(row.get("reasons_json"))
        enriched.append(row)
    return enriched


def fetch_parent_release_policy_item_ids(conn, parent_overlay_run_id: int | None) -> set[int]:
    if parent_overlay_run_id is None:
        return set()
    placeholders = ", ".join("?" for _ in TARGET_ACTIONS)
    rows = conn.execute(
        f"""
        SELECT source_policy_item_id
        FROM segment_token_policy_overlay_items
        WHERE run_id = ?
          AND overlay_action IN ({placeholders})
        """,
        (parent_overlay_run_id, *sorted(TARGET_ACTIONS)),
    ).fetchall()
    return {int(row["source_policy_item_id"]) for row in rows}


def invalid_release_reasons(row: dict[str, Any], ready_rule_keys: set[str]) -> list[str]:
    reasons: list[str] = []
    profile = TARGET_PROFILES.get(row.get("overlay_action"))
    if profile is None:
        return ["unsupported_guarded_release_action"]
    if row["original_policy_bucket"] != profile["original_bucket"]:
        reasons.append("original_policy_bucket_mismatch")
    if row["original_risk_level"] != profile["original_risk"]:
        reasons.append("original_risk_mismatch")
    if row["overlay_policy_bucket"] != profile["target_bucket"]:
        reasons.append("overlay_bucket_mismatch")
    if row["overlay_risk_level"] != profile["target_risk"]:
        reasons.append("overlay_risk_mismatch")
    if row.get("overlay_agent_key") != profile["target_agent"]:
        reasons.append("overlay_agent_mismatch")
    if set(row.get("issue_flags") or []) != profile["issue_flags"]:
        reasons.append("issue_flags_mismatch")
    if int(row.get("apply_allowed") or 0) != 0:
        reasons.append("apply_allowed_must_remain_zero")
    if row.get("decision") != READY_STATUS:
        reasons.append("decision_not_ready_for_guarded_policy_review")
    if row.get("rule_key") not in ready_rule_keys:
        reasons.append("rule_key_not_ready_in_promotion_audit")
    return reasons


def text_hygiene_flags(text: str | None) -> list[str]:
    text = text or ""
    flags: list[str] = []
    if MOJIBAKE_SEQUENCE_RE.search(text):
        flags.append("mojibake_marker")
    if INLINE_QUESTION_RE.search(text):
        flags.append("inline_question_marker")
    if ACCENT_QUESTION_RE.search(text):
        flags.append("accent_question_marker")
    if " ? " in text or "#EMP ?#!" in text:
        flags.append("standalone_question_marker")
    return flags


def build_metrics(
    *,
    overlay: dict[str, Any],
    active_gate: dict[str, Any],
    ready_rules: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    parent_overlay_run_id: int | None,
    parent_release_policy_item_ids: set[int],
) -> dict[str, Any]:
    ready_rule_keys = {row["rule_key"] for row in ready_rules}
    release_rows = [row for row in rows if row["overlay_action"] in TARGET_ACTIONS]
    new_release_rows = [
        row
        for row in release_rows
        if int(row["source_policy_item_id"]) not in parent_release_policy_item_ids
    ]
    inherited_release_rows = [
        row
        for row in release_rows
        if int(row["source_policy_item_id"]) in parent_release_policy_item_ids
    ]
    invalid_releases = [
        {
            "source_policy_item_id": row["source_policy_item_id"],
            "segment_id": row["segment_id"],
            "relative_path": row["relative_path"],
            "source_line_number": row["source_line_number"],
            "source_key": row["source_key"],
            "rule_key": row.get("rule_key"),
            "invalid_reasons": invalid_release_reasons(row, ready_rule_keys),
        }
        for row in new_release_rows
        if invalid_release_reasons(row, ready_rule_keys)
    ]
    action_distribution = Counter(
        (
            row["overlay_action"],
            row.get("overlay_agent_key") or "",
            row["overlay_risk_level"],
        )
        for row in rows
    )
    risk_transitions = Counter(
        (row["original_risk_level"], row["overlay_risk_level"]) for row in rows
    )
    release_by_rule = Counter(row.get("rule_key") or "missing_rule_key" for row in release_rows)
    release_by_path = Counter(row["relative_path"] for row in release_rows)
    ready_pending = sum(int(row.get("pending_items") or 0) for row in ready_rules)
    text_hygiene_warnings = []
    for row in release_rows:
        flags = text_hygiene_flags(row.get("confirmed_text"))
        if not flags:
            continue
        text_hygiene_warnings.append(
            {
                "source_policy_item_id": row["source_policy_item_id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_line_number": row["source_line_number"],
                "source_key": row["source_key"],
                "rule_key": row.get("rule_key"),
                "flags": flags,
            }
        )

    release_rate = (len(release_rows) / len(rows) * 100) if rows else 0.0
    active_gate_unchanged = (
        int(active_gate["active_overlay_run_id"]) == int(parent_overlay_run_id or -1)
        and int(active_gate["active_overlay_run_id"]) != int(overlay["id"])
        and int(active_gate["active_policy_run_id"]) == int(overlay["source_policy_run_id"])
    )
    return {
        "total_candidates": len(rows),
        "guarded_release_count": len(release_rows),
        "new_guarded_release_count": len(new_release_rows),
        "inherited_guarded_release_count": len(inherited_release_rows),
        "release_rate_pct": round(release_rate, 4),
        "medium_to_low_count": sum(
            1
            for row in new_release_rows
            if row["original_risk_level"] == "medium" and row["overlay_risk_level"] == "low"
        ),
        "invalid_releases": invalid_releases,
        "invalid_release_count": len(invalid_releases),
        "apply_allowed_count": sum(int(row.get("apply_allowed") or 0) for row in rows),
        "active_gate_unchanged": 1 if active_gate_unchanged else 0,
        "ready_pending_items": ready_pending,
        "release_text_hygiene_warning_count": len(text_hygiene_warnings),
        "release_text_hygiene_warnings": text_hygiene_warnings[:50],
        "action_distribution": [
            {
                "overlay_action": action,
                "overlay_agent_key": agent,
                "overlay_risk_level": risk,
                "total": total,
            }
            for (action, agent, risk), total in action_distribution.most_common()
        ],
        "risk_transitions": [
            {"original_risk_level": original, "overlay_risk_level": overlay_risk, "total": total}
            for (original, overlay_risk), total in risk_transitions.most_common()
        ],
        "release_by_rule": [
            {"rule_key": key, "total": total}
            for key, total in release_by_rule.most_common()
        ],
        "release_by_path": [
            {"relative_path": key, "total": total}
            for key, total in release_by_path.most_common(20)
        ],
        "release_sample": [
            {
                "source_policy_item_id": row["source_policy_item_id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_line_number": row["source_line_number"],
                "source_key": row["source_key"],
                "rule_key": row.get("rule_key"),
                "confirmed_text": row.get("confirmed_text"),
                "english_text": row.get("english_text"),
            }
            for row in new_release_rows[:40]
        ],
    }


def evaluate_checkpoint(
    *,
    overlay: dict[str, Any],
    active_gate: dict[str, Any],
    notes: dict[str, Any],
    ready_rules: list[dict[str, Any]],
    metrics: dict[str, Any],
    min_releases: int,
    min_rules: int,
) -> tuple[str, str, list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    parent_overlay_run_id = notes.get("parent_overlay_run_id")
    promotion_audit_run_id = notes.get("promotion_audit_run_id")

    if overlay["overlay_name"] != EXPECTED_OVERLAY_NAME:
        blockers.append("overlay_name_mismatch")
    if overlay["finished_at"] is None:
        blockers.append("overlay_run_is_not_finished")
    if int(overlay["source_policy_run_id"]) != int(active_gate["active_policy_run_id"]):
        blockers.append("overlay_source_policy_is_not_active_gate_policy")
    if int(active_gate.get("auto_apply_allowed") or 0) != 0:
        blockers.append("active_gate_auto_apply_must_remain_disabled")
    if int(active_gate["active_overlay_run_id"]) == int(overlay["id"]):
        blockers.append("guarded_overlay_was_already_promoted_to_active_gate")
    if parent_overlay_run_id is None:
        blockers.append("missing_parent_overlay_run_id_in_overlay_notes")
    elif int(parent_overlay_run_id) != int(active_gate["active_overlay_run_id"]):
        blockers.append("active_gate_no_longer_matches_guarded_overlay_parent")
    if promotion_audit_run_id is None:
        blockers.append("missing_promotion_audit_run_id_in_overlay_notes")
    if int(metrics["apply_allowed_count"]) != 0:
        blockers.append("guarded_overlay_has_apply_allowed_rows")
    if int(metrics["invalid_release_count"]) > 0:
        blockers.append("guarded_release_rows_failed_invariant_checks")
    if not ready_rules:
        blockers.append("no_ready_rules_found_for_guarded_overlay")
    if len(ready_rules) < min_rules:
        blockers.append("not_enough_ready_rules_for_checkpoint_threshold")
    if int(metrics["guarded_release_count"]) <= 0:
        blockers.append("guarded_overlay_released_no_rows")
    if int(metrics["guarded_release_count"]) < min_releases:
        warnings.append("guarded_release_count_below_target_shadow_sample")
    if int(metrics["ready_pending_items"]) > 0:
        warnings.append("ready_rules_still_have_unreviewed_pending_items")
    if int(metrics["release_text_hygiene_warning_count"]) > 0:
        warnings.append("guarded_releases_have_text_hygiene_markers")
    if float(metrics["release_rate_pct"]) > 20.0:
        warnings.append("guarded_release_rate_above_20_percent_review_before_promotion")
    if int(metrics["active_gate_unchanged"]) != 1:
        blockers.append("active_gate_changed_or_not_linked_to_parent_overlay")

    if blockers:
        return (
            "blocked",
            "fix_guarded_overlay_blockers_before_any_shadow_validation",
            blockers,
            warnings,
        )
    if warnings:
        return (
            "ready_for_shadow_validation",
            "review_shadow_queue_for_guarded_releases_before_active_promotion",
            blockers,
            warnings,
        )
    return (
        "ready_for_guarded_gate_candidate",
        "prepare_active_gate_promotion_dry_run_keep_auto_apply_disabled",
        blockers,
        warnings,
    )


def insert_checkpoint(
    conn,
    *,
    gate_key: str,
    overlay: dict[str, Any],
    active_gate: dict[str, Any],
    notes: dict[str, Any],
    ready_rules: list[dict[str, Any]],
    metrics: dict[str, Any],
    promotion_status: str,
    recommended_action: str,
    blockers: list[str],
    warnings: list[str],
    started_at: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO ml_composite_guarded_overlay_checkpoints (
            rule_version,
            checkpoint_name,
            gate_key,
            overlay_run_id,
            parent_overlay_run_id,
            source_policy_run_id,
            promotion_audit_run_id,
            ready_rule_count,
            total_candidates,
            guarded_release_count,
            release_rate_pct,
            medium_to_low_count,
            invalid_release_count,
            apply_allowed_count,
            active_gate_overlay_run_id,
            active_gate_unchanged,
            promotion_status,
            recommended_action,
            blockers_json,
            warnings_json,
            metrics_json,
            started_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            CHECKPOINT_NAME,
            gate_key,
            overlay["id"],
            notes.get("parent_overlay_run_id"),
            overlay["source_policy_run_id"],
            notes.get("promotion_audit_run_id"),
            len(ready_rules),
            metrics["total_candidates"],
            metrics["guarded_release_count"],
            metrics["release_rate_pct"],
            metrics["medium_to_low_count"],
            metrics["invalid_release_count"],
            metrics["apply_allowed_count"],
            active_gate["active_overlay_run_id"],
            metrics["active_gate_unchanged"],
            promotion_status,
            recommended_action,
            json.dumps(blockers, ensure_ascii=False),
            json.dumps(warnings, ensure_ascii=False),
            json.dumps(metrics, ensure_ascii=False),
            started_at,
            started_at,
        ),
    )
    return int(cursor.lastrowid)


def insert_checkpoint_rules(
    conn,
    *,
    checkpoint_id: int,
    ready_rules: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    created_at: str,
) -> None:
    releases_by_rule: dict[str, list[int]] = {}
    for row in rows:
        if row["overlay_action"] not in TARGET_ACTIONS:
            continue
        rule_key = row.get("rule_key") or "missing_rule_key"
        releases_by_rule.setdefault(rule_key, []).append(int(row["source_policy_item_id"]))

    conn.executemany(
        """
        INSERT INTO ml_composite_guarded_overlay_checkpoint_rules (
            checkpoint_id,
            suggested_route,
            token_subtype,
            rule_key,
            release_count,
            reviewed_items,
            pending_items,
            accept_count,
            keep_manual_exception_count,
            reject_count,
            needs_subpolicy_count,
            promotion_status,
            sample_policy_item_ids_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                checkpoint_id,
                rule["suggested_route"],
                rule["token_subtype"],
                rule["rule_key"],
                len(releases_by_rule.get(rule["rule_key"], [])),
                rule["reviewed_items"],
                rule["pending_items"],
                rule["accept_count"],
                rule["keep_manual_exception_count"],
                rule["reject_count"],
                rule["needs_subpolicy_count"],
                rule["promotion_status"],
                json.dumps(releases_by_rule.get(rule["rule_key"], [])[:30], ensure_ascii=False),
                created_at,
            )
            for rule in ready_rules
        ],
    )


def write_report(
    settings: dict[str, Any],
    *,
    checkpoint_id: int,
    gate_key: str,
    overlay: dict[str, Any],
    active_gate: dict[str, Any],
    notes: dict[str, Any],
    ready_rules: list[dict[str, Any]],
    metrics: dict[str, Any],
    promotion_status: str,
    recommended_action: str,
    blockers: list[str],
    warnings: list[str],
    started_at: datetime,
) -> Path:
    blocker_lines = [f"- {item}" for item in blockers] or ["- none"]
    warning_lines = [f"- {item}" for item in warnings] or ["- none"]
    risk_lines = [
        f"- {row['original_risk_level']} -> {row['overlay_risk_level']}: {row['total']}"
        for row in metrics["risk_transitions"]
    ] or ["- none"]
    action_lines = [
        f"- {row['overlay_action']} | {row['overlay_agent_key']} | {row['overlay_risk_level']}: {row['total']}"
        for row in metrics["action_distribution"]
    ] or ["- none"]
    rule_lines = [
        f"- {row['rule_key']}: {row['total']}"
        for row in metrics["release_by_rule"]
    ] or ["- none"]
    path_lines = [
        f"- {row['relative_path']}: {row['total']}"
        for row in metrics["release_by_path"]
    ] or ["- none"]
    ready_rule_lines = [
        (
            f"- {rule['suggested_route']} / {rule['token_subtype']} / {rule['rule_key']}: "
            f"release={next((item['total'] for item in metrics['release_by_rule'] if item['rule_key'] == rule['rule_key']), 0)}, "
            f"accept={rule['accept_count']}, reviewed={rule['reviewed_items']}, pending={rule['pending_items']}"
        )
        for rule in ready_rules
    ] or ["- none"]
    invalid_lines = [
        (
            f"- item {row['source_policy_item_id']} | segment {row['segment_id']} | "
            f"{row['relative_path']}:{row['source_line_number']} | {row['rule_key']} | "
            f"{','.join(row['invalid_reasons'])}"
        )
        for row in metrics["invalid_releases"][:30]
    ] or ["- none"]
    hygiene_lines = [
        (
            f"- item {row['source_policy_item_id']} | segment {row['segment_id']} | "
            f"{row['relative_path']}:{row['source_line_number']} | {row['rule_key']} | "
            f"{','.join(row['flags'])}"
        )
        for row in metrics["release_text_hygiene_warnings"][:30]
    ] or ["- none"]
    sample_lines: list[str] = []
    for row in metrics["release_sample"][:20]:
        sample_lines.extend(
            [
                (
                    f"- item {row['source_policy_item_id']} | segment {row['segment_id']} | "
                    f"{row['relative_path']}:{row['source_line_number']} | {row['source_key']} | "
                    f"{row['rule_key']}"
                ),
                f"  PT: {row.get('confirmed_text')}",
                f"  EN: {row.get('english_text')}",
            ]
        )
    if not sample_lines:
        sample_lines = ["- none"]

    lines = [
        "ML composite guarded overlay checkpoint",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint id: {checkpoint_id}",
        f"Checkpoint name: {CHECKPOINT_NAME}",
        f"Gate key: {gate_key}",
        "",
        "Decision:",
        f"- Promotion status: {promotion_status}",
        f"- Recommended action: {recommended_action}",
        f"- Blockers: {len(blockers)}",
        f"- Warnings: {len(warnings)}",
        "",
        "Core metrics:",
        f"- Overlay run id: {overlay['id']}",
        f"- Overlay name: {overlay['overlay_name']}",
        f"- Parent overlay run id: {notes.get('parent_overlay_run_id')}",
        f"- Active gate overlay run id: {active_gate['active_overlay_run_id']}",
        f"- Source policy run id: {overlay['source_policy_run_id']}",
        f"- Active gate policy run id: {active_gate['active_policy_run_id']}",
        f"- Promotion audit run id: {notes.get('promotion_audit_run_id')}",
        f"- Ready rules: {len(ready_rules)}",
        f"- Total candidates: {metrics['total_candidates']}",
        f"- Guarded releases: {metrics['guarded_release_count']}",
        f"- New guarded releases: {metrics['new_guarded_release_count']}",
        f"- Inherited guarded releases: {metrics['inherited_guarded_release_count']}",
        f"- Release rate pct: {metrics['release_rate_pct']:.2f}",
        f"- Medium -> low: {metrics['medium_to_low_count']}",
        f"- Invalid releases: {metrics['invalid_release_count']}",
        f"- Apply allowed: {metrics['apply_allowed_count']}",
        f"- Active gate unchanged: {metrics['active_gate_unchanged']}",
        f"- Ready-rule pending items: {metrics['ready_pending_items']}",
        f"- Release text hygiene warnings: {metrics['release_text_hygiene_warning_count']}",
        "",
        "Blockers:",
        *blocker_lines,
        "",
        "Warnings:",
        *warning_lines,
        "",
        "Ready guarded rules:",
        *ready_rule_lines,
        "",
        "Risk transitions:",
        *risk_lines,
        "",
        "Action distribution:",
        *action_lines,
        "",
        "Guarded releases by rule:",
        *rule_lines,
        "",
        "Top release paths:",
        *path_lines,
        "",
        "Invalid release sample:",
        *invalid_lines,
        "",
        "Release text hygiene warning sample:",
        *hygiene_lines,
        "",
        "Guarded release sample:",
        *sample_lines,
    ]
    return db.write_report(settings, "ml_composite_guarded_overlay_checkpoint", lines)


def update_checkpoint_report_path(conn, *, checkpoint_id: int, report_path: Path, finished_at: str) -> None:
    conn.execute(
        """
        UPDATE ml_composite_guarded_overlay_checkpoints
        SET report_path = ?, finished_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (str(report_path), finished_at, finished_at, checkpoint_id),
    )


def main(
    overlay_run_id: int | None = None,
    gate_key: str = DEFAULT_ACTIVE_GATE_KEY,
    min_releases: int = 50,
    min_rules: int = 3,
) -> None:
    settings = db.load_settings()
    started_at_dt = datetime.now()
    started_at = started_at_dt.isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_overlay_run_id = overlay_run_id or latest_guarded_overlay_run_id(conn)
        overlay = fetch_overlay_run(conn, selected_overlay_run_id)
        active_gate = fetch_active_gate(conn, gate_key)
        notes = parse_json_dict(overlay.get("notes_json"))
        ready_rules = fetch_ready_rules(conn, notes.get("promotion_audit_run_id"))
        rows = fetch_overlay_rows(conn, selected_overlay_run_id)
        parent_release_policy_item_ids = fetch_parent_release_policy_item_ids(
            conn,
            notes.get("parent_overlay_run_id"),
        )
        metrics = build_metrics(
            overlay=overlay,
            active_gate=active_gate,
            ready_rules=ready_rules,
            rows=rows,
            parent_overlay_run_id=notes.get("parent_overlay_run_id"),
            parent_release_policy_item_ids=parent_release_policy_item_ids,
        )
        promotion_status, recommended_action, blockers, warnings = evaluate_checkpoint(
            overlay=overlay,
            active_gate=active_gate,
            notes=notes,
            ready_rules=ready_rules,
            metrics=metrics,
            min_releases=min_releases,
            min_rules=min_rules,
        )
        checkpoint_id = insert_checkpoint(
            conn,
            gate_key=gate_key,
            overlay=overlay,
            active_gate=active_gate,
            notes=notes,
            ready_rules=ready_rules,
            metrics=metrics,
            promotion_status=promotion_status,
            recommended_action=recommended_action,
            blockers=blockers,
            warnings=warnings,
            started_at=started_at,
        )
        insert_checkpoint_rules(
            conn,
            checkpoint_id=checkpoint_id,
            ready_rules=ready_rules,
            rows=rows,
            created_at=started_at,
        )
        report_path = write_report(
            settings,
            checkpoint_id=checkpoint_id,
            gate_key=gate_key,
            overlay=overlay,
            active_gate=active_gate,
            notes=notes,
            ready_rules=ready_rules,
            metrics=metrics,
            promotion_status=promotion_status,
            recommended_action=recommended_action,
            blockers=blockers,
            warnings=warnings,
            started_at=started_at_dt,
        )
        finished_at = datetime.now().isoformat(timespec="seconds")
        update_checkpoint_report_path(
            conn,
            checkpoint_id=checkpoint_id,
            report_path=report_path,
            finished_at=finished_at,
        )
        conn.commit()

    print("[ml_composite_guarded_overlay_checkpoint] Checkpoint completed")
    print(f"[ml_composite_guarded_overlay_checkpoint] Checkpoint id: {checkpoint_id}")
    print(f"[ml_composite_guarded_overlay_checkpoint] Overlay run id: {selected_overlay_run_id}")
    print(f"[ml_composite_guarded_overlay_checkpoint] Promotion status: {promotion_status}")
    print(f"[ml_composite_guarded_overlay_checkpoint] Recommended action: {recommended_action}")
    print(f"[ml_composite_guarded_overlay_checkpoint] Guarded releases: {metrics['guarded_release_count']}")
    print(f"[ml_composite_guarded_overlay_checkpoint] Blockers: {len(blockers)}")
    print(f"[ml_composite_guarded_overlay_checkpoint] Warnings: {len(warnings)}")
    print(f"[ml_composite_guarded_overlay_checkpoint] Report: {report_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate a guarded composite overlay checkpoint.")
    parser.add_argument("--overlay-run-id", type=int, default=None)
    parser.add_argument("--gate-key", default=DEFAULT_ACTIVE_GATE_KEY)
    parser.add_argument("--min-releases", type=int, default=50)
    parser.add_argument("--min-rules", type=int, default=3)
    parsed = parser.parse_args()
    main(
        overlay_run_id=parsed.overlay_run_id,
        gate_key=parsed.gate_key,
        min_releases=parsed.min_releases,
        min_rules=parsed.min_rules,
    )
