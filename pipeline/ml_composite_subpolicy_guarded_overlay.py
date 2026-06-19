from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short
from ml_composite_subpolicy_diagnostic import classify_token_subtype
from ml_composite_subpolicy_promotion_audit import (
    NAME_METHODS,
    all_glossary_tokens,
    glossary_key_counter,
    only_methods,
    rule_key_for,
    same_tokens_after_style_strip,
    token_scopes,
)
from segment_token_overlay_review_queue import (
    DEFAULT_ACTIVE_GATE_KEY,
    active_gate_overlay_run_id,
    enrich,
    fetch_rows,
    parse_csv_filter,
)


RULE_VERSION = "ml_composite_subpolicy_guarded_overlay_v1"
OVERLAY_NAME = "composite_pronoun_subpolicy_guarded_overlay"
AGENT_KEY = "gender_pronoun_english_aligned_subpolicy"
TARGET_BUCKET = "guarded_pronoun_english_aligned_subpolicy"
TARGET_ACTION = "dry_run_release_to_guarded_pronoun_policy"
MIXED_AGENT_KEY = "mixed_name_gender_subpolicy"
MIXED_TARGET_BUCKET = "guarded_mixed_name_gender_subpolicy"
MIXED_TARGET_ACTION = "dry_run_release_to_guarded_mixed_name_gender_policy"
GLOSSARY_LABEL_AGENT_KEY = "glossary_label_translation_subpolicy"
GLOSSARY_LABEL_TARGET_BUCKET = "guarded_glossary_label_translation_subpolicy"
GLOSSARY_LABEL_TARGET_ACTION = "dry_run_release_to_guarded_glossary_label_policy"
SELECT_AGENT_KEY = "select_cstring_ui_subpolicy"
SELECT_TARGET_BUCKET = "guarded_select_cstring_ui_subpolicy"
SELECT_TARGET_ACTION = "dry_run_release_to_guarded_select_cstring_policy"
TOKEN_ADDED_AGENT_KEY = "token_added_english_reference_subpolicy"
TOKEN_ADDED_TARGET_BUCKET = "guarded_token_added_english_reference_subpolicy"
TOKEN_ADDED_TARGET_ACTION = "dry_run_release_to_guarded_token_added_policy"
GENDER_ARTICLE_AGENT_KEY = "gender_article_removal_subpolicy"
GENDER_ARTICLE_TARGET_BUCKET = "guarded_gender_article_removal_subpolicy"
GENDER_ARTICLE_TARGET_ACTION = "dry_run_release_to_guarded_gender_article_removal_policy"
GENDER_FORM_SWAP_AGENT_KEY = "gender_form_swap_subpolicy"
GENDER_FORM_SWAP_TARGET_BUCKET = "guarded_gender_form_swap_subpolicy"
GENDER_FORM_SWAP_TARGET_ACTION = "dry_run_release_to_guarded_gender_form_swap_policy"
PRONOUN_FORM_SWAP_AGENT_KEY = "pronoun_form_swap_subpolicy"
PRONOUN_FORM_SWAP_TARGET_BUCKET = "guarded_pronoun_form_swap_subpolicy"
PRONOUN_FORM_SWAP_TARGET_ACTION = "dry_run_release_to_guarded_pronoun_form_swap_policy"
NAME_FORM_STYLE_AGENT_KEY = "name_form_style_subpolicy"
NAME_FORM_STYLE_TARGET_BUCKET = "guarded_name_form_style_subpolicy"
NAME_FORM_STYLE_TARGET_ACTION = "dry_run_release_to_guarded_name_form_style_policy"
TOKEN_STYLE_AGENT_KEY = "token_style_modifier_subpolicy"
TOKEN_STYLE_TARGET_BUCKET = "guarded_token_style_modifier_subpolicy"
TOKEN_STYLE_TARGET_ACTION = "dry_run_release_to_guarded_token_style_modifier_policy"
DYNAMIC_SCOPE_AGENT_KEY = "dynamic_scope_neutralization_subpolicy"
DYNAMIC_SCOPE_TARGET_BUCKET = "guarded_dynamic_scope_neutralization_subpolicy"
DYNAMIC_SCOPE_TARGET_ACTION = "dry_run_release_to_guarded_dynamic_scope_neutralization_policy"
TUTORIAL_CONCEPT_AGENT_KEY = "tutorial_concept_exception_subpolicy"
TUTORIAL_CONCEPT_TARGET_BUCKET = "guarded_tutorial_concept_exception_subpolicy"
TUTORIAL_CONCEPT_TARGET_ACTION = "dry_run_release_to_guarded_tutorial_concept_policy"
DEFAULT_STATUSES = "ready_for_guarded_policy_review"
TOKEN_ADDED_MANUAL_CONTEXT_MARKERS = {
    "CatStory",
    "DogStory",
    "HorseStory",
    "RandomWomanMan",
    "DescribingEducationOutcomeProwess",
    "ES_OA",
    "ES_XA",
    "ES_EA",
    "ES_ElLa",
    "ES_ElElla",
    "ES_DelDela",
    "ES_AlAla",
    "ES_LoLa",
}
PRONOUN_MISSING_TOKEN_RELEASE_RULES = {
    "single_scope_missing_gender_to_object_pronoun_english_aligned": {"GetHerHim"},
    "single_scope_missing_article_to_repeated_subject_pronoun_english_aligned": {"GetSheHe"},
    "single_scope_missing_article_to_subject_pronoun_english_aligned": {"GetSheHe"},
    "single_scope_missing_gender_suffix_to_subject_pronoun_clean_context": {"GetSheHe"},
}
INLINE_QUESTION_RE = re.compile(r"[A-Za-z]\?[A-Za-z]")
ACCENT_QUESTION_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]\?(?=[A-Za-zÀ-ÖØ-öø-ÿ]|-[A-Za-zÀ-ÖØ-öø-ÿ]|\s+[a-zà-öø-ÿ])")
MOJIBAKE_SEQUENCE_RE = re.compile(r"(?:Ãƒ[Â£Â©Â¡Â³ÂµÂºÂªÂ§Â¨]|Ã‚[^\sA-Za-z0-9]|ï¿½)")

RELEASE_PROFILES = {
    "pronoun": {
        "agent_key": AGENT_KEY,
        "target_bucket": TARGET_BUCKET,
        "target_action": TARGET_ACTION,
        "target_risk": "low",
    },
    "mixed_name_gender": {
        "agent_key": MIXED_AGENT_KEY,
        "target_bucket": MIXED_TARGET_BUCKET,
        "target_action": MIXED_TARGET_ACTION,
        "target_risk": "low",
    },
    "glossary_label_translation": {
        "agent_key": GLOSSARY_LABEL_AGENT_KEY,
        "target_bucket": GLOSSARY_LABEL_TARGET_BUCKET,
        "target_action": GLOSSARY_LABEL_TARGET_ACTION,
        "target_risk": "low",
    },
    "select_cstring_ui": {
        "agent_key": SELECT_AGENT_KEY,
        "target_bucket": SELECT_TARGET_BUCKET,
        "target_action": SELECT_TARGET_ACTION,
        "target_risk": "low",
    },
    "token_added": {
        "agent_key": TOKEN_ADDED_AGENT_KEY,
        "target_bucket": TOKEN_ADDED_TARGET_BUCKET,
        "target_action": TOKEN_ADDED_TARGET_ACTION,
        "target_risk": "low",
    },
    "gender_article_removal": {
        "agent_key": GENDER_ARTICLE_AGENT_KEY,
        "target_bucket": GENDER_ARTICLE_TARGET_BUCKET,
        "target_action": GENDER_ARTICLE_TARGET_ACTION,
        "target_risk": "low",
    },
    "gender_form_swap": {
        "agent_key": GENDER_FORM_SWAP_AGENT_KEY,
        "target_bucket": GENDER_FORM_SWAP_TARGET_BUCKET,
        "target_action": GENDER_FORM_SWAP_TARGET_ACTION,
        "target_risk": "low",
    },
    "pronoun_form_swap": {
        "agent_key": PRONOUN_FORM_SWAP_AGENT_KEY,
        "target_bucket": PRONOUN_FORM_SWAP_TARGET_BUCKET,
        "target_action": PRONOUN_FORM_SWAP_TARGET_ACTION,
        "target_risk": "low",
    },
    "name_form_style": {
        "agent_key": NAME_FORM_STYLE_AGENT_KEY,
        "target_bucket": NAME_FORM_STYLE_TARGET_BUCKET,
        "target_action": NAME_FORM_STYLE_TARGET_ACTION,
        "target_risk": "low",
    },
    "token_style_modifier": {
        "agent_key": TOKEN_STYLE_AGENT_KEY,
        "target_bucket": TOKEN_STYLE_TARGET_BUCKET,
        "target_action": TOKEN_STYLE_TARGET_ACTION,
        "target_risk": "low",
    },
    "dynamic_scope_neutralization": {
        "agent_key": DYNAMIC_SCOPE_AGENT_KEY,
        "target_bucket": DYNAMIC_SCOPE_TARGET_BUCKET,
        "target_action": DYNAMIC_SCOPE_TARGET_ACTION,
        "target_risk": "low",
    },
    "tutorial_concept": {
        "agent_key": TUTORIAL_CONCEPT_AGENT_KEY,
        "target_bucket": TUTORIAL_CONCEPT_TARGET_BUCKET,
        "target_action": TUTORIAL_CONCEPT_TARGET_ACTION,
        "target_risk": "high",
    },
}


def token_contains_any(tokens: list[str], markers: set[str]) -> bool:
    return any(marker in token for token in tokens for marker in markers)


def token_scope_method(token: str) -> tuple[str, str]:
    inner = token.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    inner = inner.split("|", 1)[0]
    if "." not in inner:
        return ("", inner)
    scope, method = inner.rsplit(".", 1)
    return scope, method


def has_style_modifier(tokens: list[str]) -> bool:
    return any("|" in token for token in tokens)


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


def latest_audit_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_composite_subpolicy_promotion_audit_runs
        WHERE finished_at IS NOT NULL
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No ml_composite_subpolicy_promotion_audit_runs entry found.")
    return int(row["id"])


def fetch_audit_run(conn, run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_composite_subpolicy_promotion_audit_runs
        WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Promotion audit run {run_id} was not found.")
    return dict(row)


def fetch_source_policy_run(conn, policy_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM segment_token_policy_runs
        WHERE id = ?
        """,
        (policy_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Segment token policy run {policy_run_id} was not found.")
    return dict(row)


def fetch_ready_rules(
    conn,
    *,
    audit_run_id: int,
    statuses: set[str],
    rule_keys: set[str],
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_composite_subpolicy_promotion_audit_items
        WHERE run_id = ?
        ORDER BY suggested_route, token_subtype, rule_key
        """,
        (audit_run_id,),
    ).fetchall()
    ready: list[dict[str, Any]] = []
    for raw_row in rows:
        row = dict(raw_row)
        if statuses and row["promotion_status"] not in statuses:
            continue
        if rule_keys and row["rule_key"] not in rule_keys:
            continue
        ready.append(row)
    return ready


def release_profile_for(row: dict[str, Any], rule: dict[str, Any]) -> dict[str, str] | None:
    if (
        rule["suggested_route"] == "gender_token_subspecialist_review"
        and rule["token_subtype"] == "pronoun_added_for_pt_fluency"
    ) or rule["suggested_route"] == "gender_pronoun_english_aligned_subpolicy":
        return RELEASE_PROFILES["pronoun"]
    if (
        rule["suggested_route"] == "gender_token_subspecialist_review"
        and rule["token_subtype"] == "gender_token_rewrite"
        and rule["rule_key"] == "single_gender_article_removed_before_name_clean_candidate"
    ):
        return RELEASE_PROFILES["gender_article_removal"]
    if (
        rule["suggested_route"] == "gender_token_subspecialist_review"
        and rule["token_subtype"] == "gender_custom_form_swap"
        and rule["rule_key"] == "same_scope_es_oa_to_es_xa_article_candidate"
    ):
        return RELEASE_PROFILES["gender_form_swap"]
    if (
        rule["suggested_route"] == "gender_token_subspecialist_review"
        and rule["token_subtype"] == "pronoun_form_swap"
        and rule["rule_key"] == "same_scope_subject_to_object_pronoun_candidate"
    ):
        return RELEASE_PROFILES["pronoun_form_swap"]
    if (
        rule["suggested_route"] == "mixed_token_change_review"
        and rule["token_subtype"] == "name_and_gender_token_rewrite"
    ):
        return RELEASE_PROFILES["mixed_name_gender"]
    if (
        rule["suggested_route"] == "mixed_token_change_review"
        and rule["token_subtype"] == "name_form_rewrite"
        and rule["rule_key"] == "same_scope_name_form_style_removed_candidate"
    ):
        return RELEASE_PROFILES["name_form_style"]
    if (
        rule["suggested_route"] == "mixed_token_change_review"
        and rule["token_subtype"] == "mixed_unclassified_token_rewrite"
        and rule["rule_key"] == "same_token_style_modifier_english_aligned"
    ):
        return RELEASE_PROFILES["token_style_modifier"]
    if (
        rule["suggested_route"] == "mixed_token_change_review"
        and rule["token_subtype"] == "glossary_label_translation"
        and rule["rule_key"] == "single_glossary_same_key_label_translation_candidate"
    ):
        return RELEASE_PROFILES["glossary_label_translation"]
    if (
        rule["suggested_route"] == "dynamic_scope_token_review"
        and rule["token_subtype"] == "dynamic_scope_rewrite"
        and rule["rule_key"] == "debate_progress_marker_localplayer_scope_rephrase"
    ):
        return RELEASE_PROFILES["dynamic_scope_neutralization"]
    if (
        rule["suggested_route"] == "select_cstring_dynamic_context_review"
        and rule["token_subtype"]
        in {
            "select_cstring_literalized_context",
            "select_cstring_multi_dynamic_rewrite",
            "select_cstring_to_custom_gender_rewrite",
        }
    ):
        return RELEASE_PROFILES["select_cstring_ui"]
    if (
        rule["suggested_route"] == "token_added_review"
        and rule["token_subtype"] == "token_added_contextual_exception"
    ):
        return RELEASE_PROFILES["token_added"]
    if (
        rule["suggested_route"] == "tutorial_concept_exception_subpolicy_review"
        and rule["token_subtype"] == "tutorial_concept_exception_candidate"
        and rule["rule_key"] in {"tutorial_game_concept_addition", "tutorial_concept_placeholder_rewrite"}
    ):
        return RELEASE_PROFILES["tutorial_concept"]
    return None


def can_release(row: dict[str, Any], rule: dict[str, Any] | None) -> tuple[bool, list[str], dict[str, str] | None]:
    reasons: list[str] = []
    if rule is None:
        reasons.append("no_ready_rule")
        return False, reasons, None

    profile = release_profile_for(row, rule)
    if profile is None:
        reasons.append("unsupported_ready_rule_release_profile")
        return False, reasons, None

    if profile["target_action"] == TARGET_ACTION:
        current_allowed_buckets = {"review_gender_token_change", profile["target_bucket"]}
        current_allowed_risks = {"medium", profile["target_risk"]}
        if row.get("base_policy_bucket") != "review_gender_token_change":
            reasons.append("original_bucket_not_gender_token_review")
        if row.get("base_risk_level") != "medium":
            reasons.append("original_risk_not_medium")
        if row["overlay_policy_bucket"] not in current_allowed_buckets:
            reasons.append("current_bucket_not_eligible_for_pronoun_release")
        if row["overlay_risk_level"] not in current_allowed_risks:
            reasons.append("current_risk_not_eligible_for_pronoun_release")
        allowed_missing_methods = PRONOUN_MISSING_TOKEN_RELEASE_RULES.get(rule["rule_key"])
        if row.get("missing_tokens") and allowed_missing_methods is None:
            reasons.append("missing_tokens_not_allowed")
        if allowed_missing_methods is not None:
            extra_tokens = row.get("extra_tokens") or []
            if not row.get("missing_tokens"):
                reasons.append("missing_tokens_required_for_missing_token_pronoun_release")
            if not extra_tokens:
                reasons.append("extra_tokens_required_for_missing_token_pronoun_release")
            if not any(any(method in token for method in allowed_missing_methods) for token in extra_tokens):
                reasons.append("missing_expected_object_pronoun_extra_token")
        if set(row.get("issue_flags") or []) != {"gender_or_pronoun_token_change"}:
            reasons.append("unexpected_issue_flags")
    elif profile["target_action"] == MIXED_TARGET_ACTION:
        current_allowed_buckets = {"review_mixed_token_change", profile["target_bucket"]}
        current_allowed_risks = {"high", profile["target_risk"]}
        if row.get("base_policy_bucket") != "review_mixed_token_change":
            reasons.append("original_bucket_not_mixed_token_review")
        if row.get("base_risk_level") != "high":
            reasons.append("original_risk_not_high")
        if row["overlay_policy_bucket"] not in current_allowed_buckets:
            reasons.append("current_bucket_not_eligible_for_mixed_release")
        if row["overlay_risk_level"] not in current_allowed_risks:
            reasons.append("current_risk_not_eligible_for_mixed_release")
        if not row.get("missing_tokens") or not row.get("extra_tokens"):
            reasons.append("mixed_release_requires_missing_and_extra_tokens")
        if set(row.get("issue_flags") or []) != {"mixed_token_change"}:
            reasons.append("unexpected_mixed_issue_flags")
    elif profile["target_action"] == SELECT_TARGET_ACTION:
        current_allowed_buckets = {"review_select_cstring_change", profile["target_bucket"]}
        current_allowed_risks = {"high", profile["target_risk"]}
        if row.get("base_policy_bucket") != "review_select_cstring_change":
            reasons.append("original_bucket_not_select_cstring_review")
        if row.get("base_risk_level") != "high":
            reasons.append("original_risk_not_high")
        if row["overlay_policy_bucket"] not in current_allowed_buckets:
            reasons.append("current_bucket_not_eligible_for_select_cstring_release")
        if row["overlay_risk_level"] not in current_allowed_risks:
            reasons.append("current_risk_not_eligible_for_select_cstring_release")
        if not row.get("missing_tokens"):
            reasons.append("select_cstring_release_requires_missing_tokens")
        if set(row.get("issue_flags") or []) != {"select_cstring_changed"}:
            reasons.append("unexpected_select_cstring_issue_flags")
    elif profile["target_action"] == TOKEN_ADDED_TARGET_ACTION:
        current_allowed_buckets = {"review_token_added", profile["target_bucket"]}
        current_allowed_risks = {"medium", profile["target_risk"]}
        extra_tokens = row.get("extra_tokens") or []
        if row.get("base_policy_bucket") != "review_token_added":
            reasons.append("original_bucket_not_token_added_review")
        if row.get("base_risk_level") != "medium":
            reasons.append("original_risk_not_medium")
        if row["overlay_policy_bucket"] not in current_allowed_buckets:
            reasons.append("current_bucket_not_eligible_for_token_added_release")
        if row["overlay_risk_level"] not in current_allowed_risks:
            reasons.append("current_risk_not_eligible_for_token_added_release")
        if rule.get("rule_key") != "token_added_english_reference_restored":
            reasons.append("unexpected_token_added_rule_key")
        if row.get("missing_tokens"):
            reasons.append("token_added_release_requires_no_missing_tokens")
        if not extra_tokens:
            reasons.append("token_added_release_requires_extra_tokens")
        if token_contains_any(extra_tokens, TOKEN_ADDED_MANUAL_CONTEXT_MARKERS):
            reasons.append("token_added_release_has_manual_context_marker")
        if set(row.get("issue_flags") or []) != {"token_added"}:
            reasons.append("unexpected_token_added_issue_flags")
    elif profile["target_action"] == GENDER_ARTICLE_TARGET_ACTION:
        current_allowed_buckets = {"review_gender_token_change", profile["target_bucket"]}
        current_allowed_risks = {"medium", profile["target_risk"]}
        missing_tokens = row.get("missing_tokens") or []
        if row.get("base_policy_bucket") != "review_gender_token_change":
            reasons.append("original_bucket_not_gender_token_review")
        if row.get("base_risk_level") != "medium":
            reasons.append("original_risk_not_medium")
        if row["overlay_policy_bucket"] not in current_allowed_buckets:
            reasons.append("current_bucket_not_eligible_for_gender_article_release")
        if row["overlay_risk_level"] not in current_allowed_risks:
            reasons.append("current_risk_not_eligible_for_gender_article_release")
        if rule.get("rule_key") != "single_gender_article_removed_before_name_clean_candidate":
            reasons.append("unexpected_gender_article_rule_key")
        if len(missing_tokens) != 1:
            reasons.append("gender_article_release_requires_one_missing_token")
        if not token_contains_any(missing_tokens, {"ES_DelDela"}):
            reasons.append("gender_article_release_requires_es_deldela_missing_token")
        if row.get("extra_tokens"):
            reasons.append("gender_article_release_requires_no_extra_tokens")
        if set(row.get("issue_flags") or []) != {"gender_or_pronoun_token_change"}:
            reasons.append("unexpected_gender_article_issue_flags")
    elif profile["target_action"] == GENDER_FORM_SWAP_TARGET_ACTION:
        current_allowed_buckets = {"review_gender_token_change", profile["target_bucket"]}
        current_allowed_risks = {"medium", profile["target_risk"]}
        missing_tokens = row.get("missing_tokens") or []
        extra_tokens = row.get("extra_tokens") or []
        if row.get("base_policy_bucket") != "review_gender_token_change":
            reasons.append("original_bucket_not_gender_token_review")
        if row.get("base_risk_level") != "medium":
            reasons.append("original_risk_not_medium")
        if row["overlay_policy_bucket"] not in current_allowed_buckets:
            reasons.append("current_bucket_not_eligible_for_gender_form_swap_release")
        if row["overlay_risk_level"] not in current_allowed_risks:
            reasons.append("current_risk_not_eligible_for_gender_form_swap_release")
        if rule.get("rule_key") != "same_scope_es_oa_to_es_xa_article_candidate":
            reasons.append("unexpected_gender_form_swap_rule_key")
        if len(missing_tokens) != 1:
            reasons.append("gender_form_swap_release_requires_one_missing_token")
        if len(extra_tokens) != 1:
            reasons.append("gender_form_swap_release_requires_one_extra_token")
        if missing_tokens and extra_tokens:
            missing_scope, missing_method = token_scope_method(missing_tokens[0])
            extra_scope, extra_method = token_scope_method(extra_tokens[0])
            if not missing_method.startswith("Custom"):
                reasons.append("gender_form_swap_missing_token_must_be_custom")
            if not extra_method.startswith("Custom"):
                reasons.append("gender_form_swap_extra_token_must_be_custom")
            if not missing_scope or missing_scope != extra_scope:
                reasons.append("gender_form_swap_requires_same_non_empty_scope")
        if not token_contains_any(missing_tokens, {"ES_OA"}):
            reasons.append("gender_form_swap_requires_es_oa_missing_token")
        if not token_contains_any(extra_tokens, {"ES_XA"}):
            reasons.append("gender_form_swap_requires_es_xa_extra_token")
        if has_style_modifier(missing_tokens + extra_tokens):
            reasons.append("gender_form_swap_release_requires_no_style_modifier")
        disallowed_markers = {
            "Select_CString(",
            "ES_EA",
            "ES_ElLa",
            "ES_ElElla",
            "ES_DelDela",
            "ES_AlAla",
            "ES_LoLa",
        }
        if token_contains_any(missing_tokens + extra_tokens, disallowed_markers):
            reasons.append("gender_form_swap_release_has_disallowed_gender_marker")
        if set(row.get("issue_flags") or []) != {"gender_or_pronoun_token_change"}:
            reasons.append("unexpected_gender_form_swap_issue_flags")
    elif profile["target_action"] == PRONOUN_FORM_SWAP_TARGET_ACTION:
        current_allowed_buckets = {"review_gender_token_change", profile["target_bucket"]}
        current_allowed_risks = {"medium", profile["target_risk"]}
        missing_tokens = row.get("missing_tokens") or []
        extra_tokens = row.get("extra_tokens") or []
        if row.get("base_policy_bucket") != "review_gender_token_change":
            reasons.append("original_bucket_not_gender_token_review")
        if row.get("base_risk_level") != "medium":
            reasons.append("original_risk_not_medium")
        if row["overlay_policy_bucket"] not in current_allowed_buckets:
            reasons.append("current_bucket_not_eligible_for_pronoun_form_swap_release")
        if row["overlay_risk_level"] not in current_allowed_risks:
            reasons.append("current_risk_not_eligible_for_pronoun_form_swap_release")
        if rule.get("rule_key") != "same_scope_subject_to_object_pronoun_candidate":
            reasons.append("unexpected_pronoun_form_swap_rule_key")
        if len(missing_tokens) != 1:
            reasons.append("pronoun_form_swap_release_requires_one_missing_token")
        if len(extra_tokens) != 1:
            reasons.append("pronoun_form_swap_release_requires_one_extra_token")
        if missing_tokens and extra_tokens:
            missing_scope, missing_method = token_scope_method(missing_tokens[0])
            extra_scope, extra_method = token_scope_method(extra_tokens[0])
            if missing_method != "GetSheHe":
                reasons.append("pronoun_form_swap_missing_token_must_be_subject_pronoun")
            if extra_method != "GetHerHim":
                reasons.append("pronoun_form_swap_extra_token_must_be_object_pronoun")
            if not missing_scope or missing_scope != extra_scope:
                reasons.append("pronoun_form_swap_requires_same_non_empty_scope")
        if has_style_modifier(missing_tokens + extra_tokens):
            reasons.append("pronoun_form_swap_release_requires_no_style_modifier")
        if token_contains_any(missing_tokens + extra_tokens, {"Custom(", "Select_CString(", "ES_"}):
            reasons.append("pronoun_form_swap_release_has_manual_context_marker")
        if set(row.get("issue_flags") or []) != {"gender_or_pronoun_token_change"}:
            reasons.append("unexpected_pronoun_form_swap_issue_flags")
    elif profile["target_action"] == NAME_FORM_STYLE_TARGET_ACTION:
        current_allowed_buckets = {"review_mixed_token_change", profile["target_bucket"]}
        current_allowed_risks = {"high", profile["target_risk"]}
        missing_tokens = row.get("missing_tokens") or []
        extra_tokens = row.get("extra_tokens") or []
        if row.get("base_policy_bucket") != "review_mixed_token_change":
            reasons.append("original_bucket_not_mixed_token_review")
        if row.get("base_risk_level") != "high":
            reasons.append("original_risk_not_high")
        if row["overlay_policy_bucket"] not in current_allowed_buckets:
            reasons.append("current_bucket_not_eligible_for_name_form_style_release")
        if row["overlay_risk_level"] not in current_allowed_risks:
            reasons.append("current_risk_not_eligible_for_name_form_style_release")
        if rule.get("rule_key") != "same_scope_name_form_style_removed_candidate":
            reasons.append("unexpected_name_form_style_rule_key")
        if not missing_tokens or not extra_tokens:
            reasons.append("name_form_style_release_requires_missing_and_extra_tokens")
        if len(missing_tokens) != len(extra_tokens):
            reasons.append("name_form_style_release_requires_equal_token_counts")
        if not only_methods(missing_tokens + extra_tokens, NAME_METHODS):
            reasons.append("name_form_style_release_requires_only_name_methods")
        if token_scopes(missing_tokens) != token_scopes(extra_tokens):
            reasons.append("name_form_style_release_requires_same_scopes")
        if not same_tokens_after_style_strip(missing_tokens, extra_tokens):
            reasons.append("name_form_style_release_requires_same_tokens_after_style_strip")
        if not has_style_modifier(missing_tokens):
            reasons.append("name_form_style_release_requires_missing_style_modifier")
        if has_style_modifier(extra_tokens):
            reasons.append("name_form_style_release_requires_extra_without_style_modifier")
        if token_contains_any(missing_tokens + extra_tokens, {"Custom(", "Select_CString(", "Glossary(", "ES_"}):
            reasons.append("name_form_style_release_has_manual_context_marker")
        hygiene_flags = text_hygiene_flags(row.get("confirmed_text"))
        if hygiene_flags:
            reasons.append("name_form_style_release_blocked_by_text_hygiene:" + ",".join(hygiene_flags))
        if set(row.get("issue_flags") or []) != {"mixed_token_change"}:
            reasons.append("unexpected_name_form_style_issue_flags")
    elif profile["target_action"] == TOKEN_STYLE_TARGET_ACTION:
        current_allowed_buckets = {"review_mixed_token_change", profile["target_bucket"]}
        current_allowed_risks = {"high", profile["target_risk"]}
        missing_tokens = row.get("missing_tokens") or []
        extra_tokens = row.get("extra_tokens") or []
        if row.get("base_policy_bucket") != "review_mixed_token_change":
            reasons.append("original_bucket_not_mixed_token_review")
        if row.get("base_risk_level") != "high":
            reasons.append("original_risk_not_high")
        if row["overlay_policy_bucket"] not in current_allowed_buckets:
            reasons.append("current_bucket_not_eligible_for_token_style_release")
        if row["overlay_risk_level"] not in current_allowed_risks:
            reasons.append("current_risk_not_eligible_for_token_style_release")
        if rule.get("rule_key") != "same_token_style_modifier_english_aligned":
            reasons.append("unexpected_token_style_rule_key")
        if not missing_tokens or not extra_tokens:
            reasons.append("token_style_release_requires_missing_and_extra_tokens")
        if len(missing_tokens) != len(extra_tokens):
            reasons.append("token_style_release_requires_equal_token_counts")
        if token_scopes(missing_tokens) != token_scopes(extra_tokens):
            reasons.append("token_style_release_requires_same_scopes")
        if not same_tokens_after_style_strip(missing_tokens, extra_tokens):
            reasons.append("token_style_release_requires_same_tokens_after_style_strip")
        if has_style_modifier(missing_tokens):
            reasons.append("token_style_release_requires_missing_without_style_modifier")
        if not has_style_modifier(extra_tokens):
            reasons.append("token_style_release_requires_extra_style_modifier")
        if token_contains_any(missing_tokens + extra_tokens, {"Custom(", "Select_CString(", "Glossary(", "ES_"}):
            reasons.append("token_style_release_has_manual_context_marker")
        hygiene_flags = text_hygiene_flags(row.get("confirmed_text"))
        if hygiene_flags:
            reasons.append("token_style_release_blocked_by_text_hygiene:" + ",".join(hygiene_flags))
        if set(row.get("issue_flags") or []) != {"mixed_token_change"}:
            reasons.append("unexpected_token_style_issue_flags")
    elif profile["target_action"] == GLOSSARY_LABEL_TARGET_ACTION:
        current_allowed_buckets = {"review_mixed_token_change", profile["target_bucket"]}
        current_allowed_risks = {"high", profile["target_risk"]}
        missing_tokens = row.get("missing_tokens") or []
        extra_tokens = row.get("extra_tokens") or []
        if row.get("base_policy_bucket") != "review_mixed_token_change":
            reasons.append("original_bucket_not_mixed_token_review")
        if row.get("base_risk_level") != "high":
            reasons.append("original_risk_not_high")
        if row["overlay_policy_bucket"] not in current_allowed_buckets:
            reasons.append("current_bucket_not_eligible_for_glossary_label_release")
        if row["overlay_risk_level"] not in current_allowed_risks:
            reasons.append("current_risk_not_eligible_for_glossary_label_release")
        if rule.get("rule_key") != "single_glossary_same_key_label_translation_candidate":
            reasons.append("unexpected_glossary_label_rule_key")
        if len(missing_tokens) != 1 or len(extra_tokens) != 1:
            reasons.append("glossary_label_release_requires_one_missing_and_one_extra")
        if not all_glossary_tokens(missing_tokens + extra_tokens):
            reasons.append("glossary_label_release_requires_only_glossary_tokens")
        if glossary_key_counter(missing_tokens) != glossary_key_counter(extra_tokens):
            reasons.append("glossary_label_release_requires_same_glossary_key")
        if any(key.endswith("_REBELLION_GLOSS") for key in glossary_key_counter(missing_tokens)):
            reasons.append("glossary_label_release_blocks_historical_rebellion_boundary")
        hygiene_flags = text_hygiene_flags(row.get("confirmed_text"))
        if hygiene_flags:
            reasons.append("glossary_label_release_blocked_by_text_hygiene:" + ",".join(hygiene_flags))
        if set(row.get("issue_flags") or []) != {"mixed_token_change"}:
            reasons.append("unexpected_glossary_label_issue_flags")
    elif profile["target_action"] == DYNAMIC_SCOPE_TARGET_ACTION:
        current_allowed_buckets = {"review_dynamic_scope_change", profile["target_bucket"]}
        current_allowed_risks = {"high", profile["target_risk"]}
        missing_tokens = row.get("missing_tokens") or []
        extra_tokens = row.get("extra_tokens") or []
        if row.get("base_policy_bucket") != "review_dynamic_scope_change":
            reasons.append("original_bucket_not_dynamic_scope_review")
        if row.get("base_risk_level") != "high":
            reasons.append("original_risk_not_high")
        if row["overlay_policy_bucket"] not in current_allowed_buckets:
            reasons.append("current_bucket_not_eligible_for_dynamic_scope_release")
        if row["overlay_risk_level"] not in current_allowed_risks:
            reasons.append("current_risk_not_eligible_for_dynamic_scope_release")
        if rule.get("rule_key") != "debate_progress_marker_localplayer_scope_rephrase":
            reasons.append("unexpected_dynamic_scope_rule_key")
        if str(row.get("relative_path") or "") != "activities/debate_activity_l_spanish.yml":
            reasons.append("dynamic_scope_release_requires_debate_activity_path")
        if not str(row.get("source_key") or "").startswith("DEBATE_PROGRESS_MARKER_"):
            reasons.append("dynamic_scope_release_requires_debate_progress_marker_key")
        if len(missing_tokens) != 1:
            reasons.append("dynamic_scope_release_requires_one_missing_token")
        if not token_contains_any(missing_tokens, {"LocalPlayerString("}):
            reasons.append("dynamic_scope_release_requires_localplayerstring_missing_token")
        if extra_tokens:
            reasons.append("dynamic_scope_release_requires_no_extra_tokens")
        confirmed_text = row.get("confirmed_text") or ""
        if " tem " not in f" {confirmed_text} ":
            reasons.append("dynamic_scope_release_requires_neutral_tem_wording")
        hygiene_flags = text_hygiene_flags(confirmed_text)
        if hygiene_flags:
            reasons.append("dynamic_scope_release_blocked_by_text_hygiene:" + ",".join(hygiene_flags))
        if set(row.get("issue_flags") or []) != {"dynamic_scope_or_name_token_changed"}:
            reasons.append("unexpected_dynamic_scope_issue_flags")
    elif profile["target_action"] == TUTORIAL_CONCEPT_TARGET_ACTION:
        current_allowed_buckets = {
            "blocked_variable_or_icon_change",
            "candidate_tutorial_concept_exception",
            profile["target_bucket"],
        }
        current_allowed_risks = {"critical", "high", profile["target_risk"]}
        missing_tokens = row.get("missing_tokens") or []
        extra_tokens = row.get("extra_tokens") or []
        if row.get("base_policy_bucket") != "blocked_variable_or_icon_change":
            reasons.append("original_bucket_not_variable_or_icon_block")
        if row.get("base_risk_level") != "critical":
            reasons.append("original_risk_not_critical")
        if row["overlay_policy_bucket"] not in current_allowed_buckets:
            reasons.append("current_bucket_not_eligible_for_tutorial_concept_release")
        if row["overlay_risk_level"] not in current_allowed_risks:
            reasons.append("current_risk_not_eligible_for_tutorial_concept_release")
        if rule.get("rule_key") not in {"tutorial_game_concept_addition", "tutorial_concept_placeholder_rewrite"}:
            reasons.append("unexpected_tutorial_concept_rule_key")
        if not str(row.get("relative_path") or "").startswith("tutorial/"):
            reasons.append("tutorial_concept_release_requires_tutorial_path")
        if not extra_tokens or not token_contains_any(extra_tokens, {"$game_concept_"}):
            reasons.append("tutorial_concept_release_requires_game_concept_extra_token")
        if token_contains_any(missing_tokens + extra_tokens, {"@"}):
            reasons.append("tutorial_concept_release_disallows_icon_tokens")
        if rule.get("rule_key") == "tutorial_game_concept_addition" and missing_tokens:
            reasons.append("tutorial_game_concept_addition_requires_no_missing_tokens")
        if rule.get("rule_key") == "tutorial_concept_placeholder_rewrite" and not token_contains_any(missing_tokens, {"Concept("}):
            reasons.append("tutorial_placeholder_rewrite_requires_missing_concept_placeholder")
        if set(row.get("issue_flags") or []) != {"variable_or_icon_changed"}:
            reasons.append("unexpected_tutorial_concept_issue_flags")
    if not row.get("confirmed_text"):
        reasons.append("missing_confirmed_text")
    if reasons:
        return False, reasons, profile
    reasons.extend(
        [
            f"ready_rule:{rule['rule_key']}",
            f"audit_status:{rule['promotion_status']}",
            "all_guard_conditions_passed",
        ]
    )
    return True, reasons, profile


def build_overlay_rows(
    *,
    active_rows: list[dict[str, Any]],
    ready_rules: list[dict[str, Any]],
    audit_run: dict[str, Any],
    active_gate: dict[str, Any],
) -> list[dict[str, Any]]:
    ready_by_key = {
        (row["suggested_route"], row["token_subtype"], row["rule_key"]): row
        for row in ready_rules
    }
    rows: list[dict[str, Any]] = []
    for active in active_rows:
        subtype, families = classify_token_subtype(active)
        active["token_subtype"] = subtype
        active["token_families"] = sorted(families)
        rule_key, signature = rule_key_for(active)
        ready_rule = ready_by_key.get((active["suggested_route"], subtype, rule_key))
        release, release_reasons, release_profile = can_release(active, ready_rule)

        if release:
            overlay_policy_bucket = release_profile["target_bucket"]
            overlay_risk_level = release_profile["target_risk"]
            overlay_action = release_profile["target_action"]
            overlay_agent_key = release_profile["agent_key"]
            decision = "ready_for_guarded_policy_review"
            final_rule_key = rule_key
        else:
            overlay_policy_bucket = active["overlay_policy_bucket"]
            overlay_risk_level = active["overlay_risk_level"]
            overlay_action = active["overlay_action"]
            overlay_agent_key = active.get("overlay_agent_key") or "active_gate_parent_overlay"
            decision = active.get("decision") or ""
            final_rule_key = active.get("rule_key") or ""

        reasons = [
            f"rule:{RULE_VERSION}",
            f"parent_overlay_run:{active_gate['active_overlay_run_id']}",
            f"promotion_audit_run:{audit_run['id']}",
            f"candidate_rule_key:{rule_key}",
            f"candidate_token_subtype:{subtype}",
            f"candidate_route:{active['suggested_route']}",
            *release_reasons,
            "apply_allowed:0",
        ]
        rows.append(
            {
                **active,
                "source_policy_item_id": int(active["policy_item_id"]),
                "original_policy_bucket": active.get("base_policy_bucket") or active["overlay_policy_bucket"],
                "original_risk_level": active.get("base_risk_level") or active["overlay_risk_level"],
                "overlay_policy_bucket": overlay_policy_bucket,
                "overlay_risk_level": overlay_risk_level,
                "overlay_action": overlay_action,
                "overlay_agent_key": overlay_agent_key,
                "would_release_critical": 0,
                "apply_allowed": 0,
                "decision": decision,
                "rule_key": final_rule_key,
                "guarded_candidate_rule_key": rule_key,
                "guarded_token_subtype": subtype,
                "guarded_token_signature": signature,
                "guarded_release": 1 if release else 0,
                "reasons": reasons,
            }
        )
    return rows


def insert_overlay_run(
    conn,
    *,
    source_policy_run: dict[str, Any],
    active_gate: dict[str, Any],
    audit_run: dict[str, Any],
    ready_rules: list[dict[str, Any]],
    started_at: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO segment_token_policy_overlay_runs (
            rule_version,
            source_policy_run_id,
            source_state_run_id,
            source_rule_version,
            overlay_name,
            min_evidence,
            notes_json,
            started_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            source_policy_run["id"],
            source_policy_run.get("state_run_id"),
            source_policy_run.get("rule_version"),
            OVERLAY_NAME,
            0,
            json.dumps(
                {
                    "parent_gate_key": active_gate["gate_key"],
                    "parent_checkpoint_id": active_gate["active_checkpoint_id"],
                    "parent_overlay_run_id": active_gate["active_overlay_run_id"],
                    "promotion_audit_run_id": audit_run["id"],
                    "ready_rule_count": len(ready_rules),
                    "ready_rule_keys": [row["rule_key"] for row in ready_rules],
                    "dry_run_only": True,
                    "apply_allowed": 0,
                },
                ensure_ascii=False,
            ),
            started_at,
            started_at,
        ),
    )
    return int(cursor.lastrowid)


def insert_overlay_items(
    conn,
    *,
    overlay_run_id: int,
    source_policy_run_id: int,
    rows: list[dict[str, Any]],
    created_at: str,
) -> None:
    conn.executemany(
        """
        INSERT OR IGNORE INTO segment_token_policy_overlay_items (
            run_id,
            source_policy_run_id,
            source_policy_item_id,
            state_run_id,
            segment_id,
            relative_path,
            source_key,
            source_line_number,
            original_policy_bucket,
            original_risk_level,
            overlay_policy_bucket,
            overlay_risk_level,
            overlay_action,
            overlay_agent_key,
            would_release_critical,
            apply_allowed,
            decision,
            rule_key,
            reasons_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                overlay_run_id,
                source_policy_run_id,
                row["source_policy_item_id"],
                row["state_run_id"],
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row["source_line_number"],
                row["original_policy_bucket"],
                row["original_risk_level"],
                row["overlay_policy_bucket"],
                row["overlay_risk_level"],
                row["overlay_action"],
                row["overlay_agent_key"],
                row["would_release_critical"],
                row["apply_allowed"],
                row["decision"],
                row["rule_key"],
                json.dumps(row["reasons"], ensure_ascii=False),
                created_at,
            )
            for row in rows
        ],
    )


def update_overlay_run(
    conn,
    *,
    overlay_run_id: int,
    rows: list[dict[str, Any]],
    ready_rules: list[dict[str, Any]],
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    finished_at: str,
) -> None:
    conn.execute(
        """
        UPDATE segment_token_policy_overlay_runs
        SET
            total_candidates = ?,
            original_critical_count = ?,
            overlay_critical_count = ?,
            released_critical_count = ?,
            remaining_blocked_count = ?,
            enabled_rule_count = ?,
            apply_allowed_count = ?,
            report_path = ?,
            csv_path = ?,
            jsonl_path = ?,
            finished_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            len(rows),
            sum(1 for row in rows if row["original_risk_level"] == "critical"),
            sum(1 for row in rows if row["overlay_risk_level"] == "critical"),
            sum(1 for row in rows if row["original_risk_level"] == "critical" and row["guarded_release"]),
            sum(1 for row in rows if row["overlay_policy_bucket"].startswith("blocked")),
            len(ready_rules),
            sum(1 for row in rows if row["apply_allowed"]),
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            finished_at,
            finished_at,
            overlay_run_id,
        ),
    )


def write_outputs(
    settings: dict[str, Any],
    *,
    overlay_run_id: int,
    source_policy_run: dict[str, Any],
    active_gate: dict[str, Any],
    audit_run: dict[str, Any],
    ready_rules: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    started_at: datetime,
) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = started_at.strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{timestamp}_ml_composite_subpolicy_guarded_overlay"
    txt_path = base.with_suffix(".txt")
    csv_path = base.with_suffix(".csv")
    jsonl_path = base.with_suffix(".jsonl")

    selected_rows = sorted(
        rows,
        key=lambda row: (
            -int(row["guarded_release"]),
            row["guarded_candidate_rule_key"],
            row["relative_path"],
            row["source_line_number"] or 0,
        ),
    )
    fieldnames = [
        "source_policy_item_id",
        "segment_id",
        "relative_path",
        "source_line_number",
        "source_key",
        "suggested_route",
        "guarded_token_subtype",
        "guarded_candidate_rule_key",
        "guarded_release",
        "original_policy_bucket",
        "original_risk_level",
        "overlay_policy_bucket",
        "overlay_risk_level",
        "overlay_action",
        "overlay_agent_key",
        "apply_allowed",
        "missing_tokens",
        "extra_tokens",
        "issue_flags",
        "reasons_json",
        "confirmed_text",
        "english_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in selected_rows:
            writer.writerow(
                {
                    key: json.dumps(row[key], ensure_ascii=False)
                    if key in {"missing_tokens", "extra_tokens", "issue_flags"}
                    else json.dumps(row["reasons"], ensure_ascii=False)
                    if key == "reasons_json"
                    else row.get(key)
                    for key in fieldnames
                }
            )

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in selected_rows:
            payload = {
                "overlay_run_id": overlay_run_id,
                "source_policy_run_id": source_policy_run["id"],
                "source_policy_item_id": row["source_policy_item_id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_line_number": row["source_line_number"],
                "source_key": row["source_key"],
                "suggested_route": row["suggested_route"],
                "guarded_token_subtype": row["guarded_token_subtype"],
                "guarded_candidate_rule_key": row["guarded_candidate_rule_key"],
                "guarded_release": row["guarded_release"],
                "original_policy_bucket": row["original_policy_bucket"],
                "original_risk_level": row["original_risk_level"],
                "overlay_policy_bucket": row["overlay_policy_bucket"],
                "overlay_risk_level": row["overlay_risk_level"],
                "overlay_action": row["overlay_action"],
                "overlay_agent_key": row["overlay_agent_key"],
                "apply_allowed": row["apply_allowed"],
                "missing_tokens": row.get("missing_tokens") or [],
                "extra_tokens": row.get("extra_tokens") or [],
                "issue_flags": row.get("issue_flags") or [],
                "reasons": row["reasons"],
                "confirmed_text": row.get("confirmed_text"),
                "english_text": row.get("english_text"),
            }
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    original_risk_counts = Counter(row["original_risk_level"] for row in rows)
    overlay_risk_counts = Counter(row["overlay_risk_level"] for row in rows)
    action_counts = Counter(row["overlay_action"] for row in rows)
    release_rows = [row for row in selected_rows if row["guarded_release"]]
    release_by_rule = Counter(row["guarded_candidate_rule_key"] for row in release_rows)
    release_by_path = Counter(row["relative_path"] for row in release_rows)
    ready_rule_lines = [
        "- {route} / {subtype} / {rule}: accept={accept}, total={total}, pending={pending}".format(
            route=row["suggested_route"],
            subtype=row["token_subtype"],
            rule=row["rule_key"],
            accept=row["accept_count"],
            total=row["total_items"],
            pending=row["pending_items"],
        )
        for row in ready_rules
    ] or ["- none"]

    lines = [
        "ML composite guarded subpolicy overlay",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Overlay name: {OVERLAY_NAME}",
        f"Overlay run id: {overlay_run_id}",
        f"Source policy run id: {source_policy_run['id']}",
        f"Parent active gate: {active_gate['gate_key']}",
        f"Parent checkpoint id: {active_gate['active_checkpoint_id']}",
        f"Parent overlay run id: {active_gate['active_overlay_run_id']}",
        f"Promotion audit run id: {audit_run['id']}",
        "",
        "Summary:",
        f"- Rows evaluated: {len(rows)}",
        f"- Ready guarded rules: {len(ready_rules)}",
        f"- Guarded releases: {len(release_rows)}",
        "- Apply allowed: 0",
        "",
        "Ready rules:",
        *ready_rule_lines,
        "",
        "Original risk:",
        *[f"- {key}: {value}" for key, value in original_risk_counts.most_common()],
        "",
        "Overlay risk:",
        *[f"- {key}: {value}" for key, value in overlay_risk_counts.most_common()],
        "",
        "Overlay actions:",
        *[f"- {key}: {value}" for key, value in action_counts.most_common()],
        "",
        "Guarded releases by rule:",
        *[f"- {key}: {value}" for key, value in release_by_rule.most_common()],
        "",
        "Top paths released:",
        *[f"- {key}: {value}" for key, value in release_by_path.most_common(12)],
        "",
        "Guarded release sample:",
    ]
    for row in release_rows[:60]:
        lines.extend(
            [
                (
                    f"- item {row['source_policy_item_id']} | segment {row['segment_id']} | "
                    f"{row['relative_path']}:{row['source_line_number']} | {row['source_key']} | "
                    f"{row['guarded_candidate_rule_key']}"
                ),
                f"  RISK: {row['original_risk_level']} -> {row['overlay_risk_level']}",
                f"  EXTRA: {json.dumps(row.get('extra_tokens') or [], ensure_ascii=False)}",
                f"  CONFIRMED: {short(row.get('confirmed_text'), 220)}",
            ]
        )
    lines.extend(
        [
            "",
            "Interpretation:",
            "- This overlay is a dry-run policy layer; it does not update output files, confirmations, ML model weights, or the active gate registry.",
            "- Guarded releases are moved from medium review risk to low guarded-policy risk only inside this experimental overlay.",
            "- apply_allowed stays 0 for every row; output application remains a separate explicit phase.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path


def main(
    *,
    audit_run_id: int | None = None,
    statuses_value: str | None = DEFAULT_STATUSES,
    rule_keys_value: str | None = None,
    gate_key: str = DEFAULT_ACTIVE_GATE_KEY,
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at_dt = datetime.now()
    started_at = started_at_dt.isoformat(timespec="seconds")
    statuses = parse_csv_filter(statuses_value)
    rule_keys = parse_csv_filter(rule_keys_value)

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_audit_run_id = audit_run_id or latest_audit_run_id(conn)
        audit_run = fetch_audit_run(conn, selected_audit_run_id)
        active_overlay_run_id, active_gate = active_gate_overlay_run_id(conn, gate_key=gate_key)
        if int(audit_run["overlay_run_id"]) != active_overlay_run_id:
            raise RuntimeError(
                f"Promotion audit overlay {audit_run['overlay_run_id']} does not match active gate overlay {active_overlay_run_id}."
            )
        source_policy_run = fetch_source_policy_run(conn, int(active_gate["active_policy_run_id"]))
        ready_rules = fetch_ready_rules(
            conn,
            audit_run_id=selected_audit_run_id,
            statuses=statuses,
            rule_keys=rule_keys,
        )
        active_rows = [enrich(row) for row in fetch_rows(conn, overlay_run_id=active_overlay_run_id, critical_only=False)]
        overlay_rows = build_overlay_rows(
            active_rows=active_rows,
            ready_rules=ready_rules,
            audit_run=audit_run,
            active_gate=active_gate,
        )
        overlay_run_id = insert_overlay_run(
            conn,
            source_policy_run=source_policy_run,
            active_gate=active_gate,
            audit_run=audit_run,
            ready_rules=ready_rules,
            started_at=started_at,
        )
        insert_overlay_items(
            conn,
            overlay_run_id=overlay_run_id,
            source_policy_run_id=int(source_policy_run["id"]),
            rows=overlay_rows,
            created_at=started_at,
        )
        conn.commit()

        txt_path, csv_path, jsonl_path = write_outputs(
            settings,
            overlay_run_id=overlay_run_id,
            source_policy_run=source_policy_run,
            active_gate=active_gate,
            audit_run=audit_run,
            ready_rules=ready_rules,
            rows=overlay_rows,
            started_at=started_at_dt,
        )
        finished_at = datetime.now().isoformat(timespec="seconds")
        update_overlay_run(
            conn,
            overlay_run_id=overlay_run_id,
            rows=overlay_rows,
            ready_rules=ready_rules,
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            finished_at=finished_at,
        )
        conn.commit()

    guarded_releases = sum(1 for row in overlay_rows if row["guarded_release"])
    action_counts = Counter(row["overlay_action"] for row in overlay_rows)
    print("[ml_composite_subpolicy_guarded_overlay] Guarded overlay generated")
    print(f"[ml_composite_subpolicy_guarded_overlay] Rule version: {RULE_VERSION}")
    print(f"[ml_composite_subpolicy_guarded_overlay] Overlay run id: {overlay_run_id}")
    print(f"[ml_composite_subpolicy_guarded_overlay] Parent active overlay run id: {active_gate['active_overlay_run_id']}")
    print(f"[ml_composite_subpolicy_guarded_overlay] Promotion audit run id: {selected_audit_run_id}")
    print(f"[ml_composite_subpolicy_guarded_overlay] Ready guarded rules: {len(ready_rules)}")
    print(f"[ml_composite_subpolicy_guarded_overlay] Rows evaluated: {len(overlay_rows)}")
    print(f"[ml_composite_subpolicy_guarded_overlay] Guarded releases: {guarded_releases}")
    for key, value in action_counts.most_common():
        print(f"[ml_composite_subpolicy_guarded_overlay] action {key}: {value}")
    print("[ml_composite_subpolicy_guarded_overlay] Apply allowed: 0")
    print(f"[ml_composite_subpolicy_guarded_overlay] Report: {txt_path}")
    print(f"[ml_composite_subpolicy_guarded_overlay] CSV: {csv_path}")
    print(f"[ml_composite_subpolicy_guarded_overlay] JSONL: {jsonl_path}")
    return {
        "overlay_run_id": overlay_run_id,
        "parent_overlay_run_id": active_gate["active_overlay_run_id"],
        "audit_run_id": selected_audit_run_id,
        "ready_rule_count": len(ready_rules),
        "rows_evaluated": len(overlay_rows),
        "guarded_releases": guarded_releases,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a dry-run guarded overlay from promoted composite subpolicy rules.")
    parser.add_argument("--audit-run-id", type=int, default=None)
    parser.add_argument("--statuses", default=DEFAULT_STATUSES)
    parser.add_argument("--rule-keys", default=None)
    parser.add_argument("--gate-key", default=DEFAULT_ACTIVE_GATE_KEY)
    args = parser.parse_args()
    main(
        audit_run_id=args.audit_run_id,
        statuses_value=args.statuses,
        rule_keys_value=args.rule_keys,
        gate_key=args.gate_key,
    )
