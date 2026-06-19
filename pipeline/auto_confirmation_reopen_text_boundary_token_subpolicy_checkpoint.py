from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short
from auto_confirmation_reopen_text_boundary_token_subpolicy_shadow import (
    DYNAMIC_SCOPE_HELPER_SUBPOLICY,
    ES_DELDELA_LITERAL_DE_SUBPOLICY,
    ES_HELPER_NARRATIVE_BUCKETS,
    ES_HELPER_NARRATIVE_SUBPOLICY,
    GLOSSARY_LABEL_SUBPOLICY,
    HOSTAGE_CONTEXT_BUCKETS,
    HOSTAGE_CONTEXT_SUBPOLICY,
    HUNT_ACTIVITY_SELECT_CSTRING_SUBPOLICY,
    LOCALPLAYERSTRING_WAR_JOIN_SUBPOLICY,
    SHORT_DYNAMIC_VERB_SUBPOLICY,
    CORONATION_TITLE_ES_HELPER_SUBPOLICY,
    NICKNAME_WHISPERER_SELECT_CSTRING_SUBPOLICY,
    SINGLE_COMBAT_VICTOR_NAME_SUBPOLICY,
    TOUR_TITLE_POSSESSIVE_SUBPOLICY,
    EP3_TRAVEL_TITLE_ADJECTIVE_SUBPOLICY,
    SELECT_CSTRING_DIRECT_NAME_SUBPOLICY,
    SELECT_CSTRING_SUBPOLICY,
)


RULE_VERSION = "auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint_v1"
CHECKPOINT_NAMES = {
    SELECT_CSTRING_SUBPOLICY: "select_cstring_invariant_ptbr_shadow_checkpoint_v1",
    GLOSSARY_LABEL_SUBPOLICY: "glossary_visible_label_ptbr_shadow_checkpoint_v1",
    ES_DELDELA_LITERAL_DE_SUBPOLICY: "es_deldela_literal_de_ptbr_shadow_checkpoint_v1",
    DYNAMIC_SCOPE_HELPER_SUBPOLICY: "dynamic_scope_ptbr_helper_neutralization_shadow_checkpoint_v1",
    SELECT_CSTRING_DIRECT_NAME_SUBPOLICY: "select_cstring_direct_name_reference_shadow_checkpoint_v1",
    LOCALPLAYERSTRING_WAR_JOIN_SUBPOLICY: "localplayerstring_war_join_neutralization_shadow_checkpoint_v1",
    ES_HELPER_NARRATIVE_SUBPOLICY: "es_helper_narrative_neutralization_shadow_checkpoint_v1",
    HOSTAGE_CONTEXT_SUBPOLICY: "hostage_context_neutralization_shadow_checkpoint_v1",
    SHORT_DYNAMIC_VERB_SUBPOLICY: "short_dynamic_spanish_verb_neutralization_shadow_checkpoint_v1",
    HUNT_ACTIVITY_SELECT_CSTRING_SUBPOLICY: "hunt_activity_select_cstring_neutralization_shadow_checkpoint_v1",
    CORONATION_TITLE_ES_HELPER_SUBPOLICY: "coronation_title_es_helper_neutralization_shadow_checkpoint_v1",
    NICKNAME_WHISPERER_SELECT_CSTRING_SUBPOLICY: "nickname_whisperer_select_cstring_neutralization_shadow_checkpoint_v1",
    SINGLE_COMBAT_VICTOR_NAME_SUBPOLICY: "single_combat_victor_name_pronoun_neutralization_shadow_checkpoint_v1",
    TOUR_TITLE_POSSESSIVE_SUBPOLICY: "tour_title_gendered_possessive_neutralization_shadow_checkpoint_v1",
    EP3_TRAVEL_TITLE_ADJECTIVE_SUBPOLICY: "ep3_travel_title_adjective_alignment_shadow_checkpoint_v1",
}
CHECKPOINT_ACTIONS = {
    SELECT_CSTRING_SUBPOLICY: "stage_select_cstring_invariant_ptbr_shadow",
    GLOSSARY_LABEL_SUBPOLICY: "stage_glossary_visible_label_ptbr_shadow",
    ES_DELDELA_LITERAL_DE_SUBPOLICY: "stage_es_deldela_literal_de_ptbr_shadow",
    DYNAMIC_SCOPE_HELPER_SUBPOLICY: "stage_dynamic_scope_ptbr_helper_neutralization_shadow",
    SELECT_CSTRING_DIRECT_NAME_SUBPOLICY: "stage_select_cstring_direct_name_reference_shadow",
    LOCALPLAYERSTRING_WAR_JOIN_SUBPOLICY: "stage_localplayerstring_war_join_neutralization_shadow",
    ES_HELPER_NARRATIVE_SUBPOLICY: "stage_es_helper_narrative_neutralization_shadow",
    HOSTAGE_CONTEXT_SUBPOLICY: "stage_hostage_context_neutralization_shadow",
    SHORT_DYNAMIC_VERB_SUBPOLICY: "stage_short_dynamic_spanish_verb_neutralization_shadow",
    HUNT_ACTIVITY_SELECT_CSTRING_SUBPOLICY: "stage_hunt_activity_select_cstring_neutralization_shadow",
    CORONATION_TITLE_ES_HELPER_SUBPOLICY: "stage_coronation_title_es_helper_neutralization_shadow",
    NICKNAME_WHISPERER_SELECT_CSTRING_SUBPOLICY: "stage_nickname_whisperer_select_cstring_neutralization_shadow",
    SINGLE_COMBAT_VICTOR_NAME_SUBPOLICY: "stage_single_combat_victor_name_pronoun_neutralization_shadow",
    TOUR_TITLE_POSSESSIVE_SUBPOLICY: "stage_tour_title_gendered_possessive_neutralization_shadow",
    EP3_TRAVEL_TITLE_ADJECTIVE_SUBPOLICY: "stage_ep3_travel_title_adjective_alignment_shadow",
}
EXPECTED_ACTIONS = {
    SELECT_CSTRING_SUBPOLICY: "would_accept_select_cstring_invariant_ptbr_shadow",
    GLOSSARY_LABEL_SUBPOLICY: "would_accept_glossary_visible_label_ptbr_shadow",
    ES_DELDELA_LITERAL_DE_SUBPOLICY: "would_accept_es_deldela_literal_de_ptbr_shadow",
    DYNAMIC_SCOPE_HELPER_SUBPOLICY: "would_accept_dynamic_scope_ptbr_helper_neutralization_shadow",
    SELECT_CSTRING_DIRECT_NAME_SUBPOLICY: "would_accept_select_cstring_direct_name_reference_shadow",
    LOCALPLAYERSTRING_WAR_JOIN_SUBPOLICY: "would_accept_localplayerstring_war_join_neutralization_shadow",
    ES_HELPER_NARRATIVE_SUBPOLICY: "would_accept_es_helper_narrative_neutralization_shadow",
    HOSTAGE_CONTEXT_SUBPOLICY: "would_accept_hostage_context_neutralization_shadow",
    SHORT_DYNAMIC_VERB_SUBPOLICY: "would_accept_short_dynamic_spanish_verb_neutralization_shadow",
    HUNT_ACTIVITY_SELECT_CSTRING_SUBPOLICY: "would_accept_hunt_activity_select_cstring_neutralization_shadow",
    CORONATION_TITLE_ES_HELPER_SUBPOLICY: "would_accept_coronation_title_es_helper_neutralization_shadow",
    NICKNAME_WHISPERER_SELECT_CSTRING_SUBPOLICY: "would_accept_nickname_whisperer_select_cstring_neutralization_shadow",
    SINGLE_COMBAT_VICTOR_NAME_SUBPOLICY: "would_accept_single_combat_victor_name_pronoun_neutralization_shadow",
    TOUR_TITLE_POSSESSIVE_SUBPOLICY: "would_accept_tour_title_gendered_possessive_neutralization_shadow",
    EP3_TRAVEL_TITLE_ADJECTIVE_SUBPOLICY: "would_accept_ep3_travel_title_adjective_alignment_shadow",
}
EXPECTED_BUCKETS = {
    SELECT_CSTRING_SUBPOLICY: "review_select_cstring_change",
    GLOSSARY_LABEL_SUBPOLICY: "review_mixed_token_change",
    ES_DELDELA_LITERAL_DE_SUBPOLICY: "review_gender_token_change",
    DYNAMIC_SCOPE_HELPER_SUBPOLICY: "review_dynamic_scope_change",
    SELECT_CSTRING_DIRECT_NAME_SUBPOLICY: "review_select_cstring_change",
    LOCALPLAYERSTRING_WAR_JOIN_SUBPOLICY: "review_dynamic_scope_change",
    ES_HELPER_NARRATIVE_SUBPOLICY: ES_HELPER_NARRATIVE_BUCKETS,
    HOSTAGE_CONTEXT_SUBPOLICY: HOSTAGE_CONTEXT_BUCKETS,
    SHORT_DYNAMIC_VERB_SUBPOLICY: "review_select_cstring_change",
    HUNT_ACTIVITY_SELECT_CSTRING_SUBPOLICY: "review_select_cstring_change",
    CORONATION_TITLE_ES_HELPER_SUBPOLICY: "review_gender_token_change",
    NICKNAME_WHISPERER_SELECT_CSTRING_SUBPOLICY: "review_select_cstring_change",
    SINGLE_COMBAT_VICTOR_NAME_SUBPOLICY: "review_dynamic_scope_change",
    TOUR_TITLE_POSSESSIVE_SUBPOLICY: "review_select_cstring_change",
    EP3_TRAVEL_TITLE_ADJECTIVE_SUBPOLICY: "review_dynamic_scope_change",
}


def latest_subpolicy_shadow_run_id(conn, *, subpolicy_name: str) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM auto_confirmation_reopen_text_boundary_token_subpolicy_shadow_runs
        WHERE finished_at IS NOT NULL
          AND subpolicy_name = ?
          AND shadow_ready_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (subpolicy_name,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No complete token subpolicy shadow run found for {subpolicy_name!r}.")
    return int(row["id"])


def report_paths(settings: dict[str, Any], *, subpolicy_name: str) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint_{subpolicy_name}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def parse_json_obj(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {"raw": value}
    return payload if isinstance(payload, dict) else {"value": payload}


def fetch_shadow_run(conn, *, subpolicy_shadow_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM auto_confirmation_reopen_text_boundary_token_subpolicy_shadow_runs
        WHERE id = ?
        """,
        (subpolicy_shadow_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Token subpolicy shadow run not found: {subpolicy_shadow_run_id}")
    return dict(row)


def fetch_rows(conn, *, subpolicy_shadow_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.*,
            decision.corrected_text,
            confirmation.confirmed_text
        FROM auto_confirmation_reopen_text_boundary_token_subpolicy_shadow_items item
        JOIN auto_confirmation_reopen_text_review_decisions decision
          ON decision.id = item.review_decision_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.id = (
              SELECT c.id
              FROM segment_confirmations c
              WHERE c.segment_id = item.segment_id
              ORDER BY c.updated_at DESC, c.id DESC
              LIMIT 1
          )
        WHERE item.run_id = ?
        ORDER BY item.relative_path, item.source_line_number, item.source_key
        """,
        (subpolicy_shadow_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def evaluate_global(
    shadow_run: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    subpolicy_name: str,
    min_ready_required: int,
    max_blocked_allowed: int,
) -> list[str]:
    reasons: list[str] = []
    if shadow_run.get("subpolicy_name") != subpolicy_name:
        reasons.append("wrong_subpolicy_name")
    if shadow_run.get("subpolicy_status") != "shadow":
        reasons.append("subpolicy_run_not_shadow")
    if int(shadow_run.get("total_candidates") or 0) != len(rows):
        reasons.append("subpolicy_item_count_mismatch")
    if int(shadow_run.get("shadow_ready_count") or 0) < min_ready_required:
        reasons.append("min_ready_not_met")
    if int(shadow_run.get("blocked_count") or 0) > max_blocked_allowed:
        reasons.append("too_many_shadow_blocks")
    if int(shadow_run.get("validation_issue_count") or 0) != 0:
        reasons.append("validation_issues_present")
    return reasons


def row_block_reason(row: dict[str, Any], *, subpolicy_name: str) -> str:
    expected_action = EXPECTED_ACTIONS.get(subpolicy_name)
    expected_bucket = EXPECTED_BUCKETS.get(subpolicy_name)
    if row.get("subpolicy_status") != "shadow_ready":
        return row.get("block_reason") or row.get("subpolicy_status") or "subpolicy_not_ready"
    if expected_action and row.get("subpolicy_action") != expected_action:
        return "wrong_subpolicy_action"
    if expected_bucket and isinstance(expected_bucket, str) and row.get("policy_bucket") != expected_bucket:
        return "wrong_policy_bucket"
    if expected_bucket and not isinstance(expected_bucket, str) and row.get("policy_bucket") not in expected_bucket:
        return "wrong_policy_bucket"
    if int(row.get("validation_issue_count") or 0) != 0:
        return "validation_issue_present"
    if not row.get("current_confirmed_text_hash") or not row.get("corrected_text_hash"):
        return "missing_text_hash"
    if row.get("current_confirmed_text_hash") == row.get("corrected_text_hash"):
        return "no_text_delta"
    evidence = parse_json_obj(row.get("evidence_json"))
    if subpolicy_name == SELECT_CSTRING_SUBPOLICY:
        if not evidence.get("matched_ptbr_verbs"):
            return "missing_matched_ptbr_verb_evidence"
        if not evidence.get("select_cstring_literal_pairs"):
            return "missing_select_cstring_evidence"
        if evidence.get("style_equivalent_non_select_tokens") is not True:
            return "non_select_tokens_not_style_equivalent"
    elif subpolicy_name == GLOSSARY_LABEL_SUBPOLICY:
        if not evidence.get("glossary_visible_label_pairs"):
            return "missing_glossary_label_evidence"
        if evidence.get("same_glossary_ids") is not True:
            return "glossary_id_not_preserved"
        if evidence.get("labels_changed") is not True:
            return "glossary_label_not_changed"
    elif subpolicy_name == ES_DELDELA_LITERAL_DE_SUBPOLICY:
        if evidence.get("helper") != "ES_DelDela":
            return "missing_es_deldela_evidence"
        if not evidence.get("helper_scope"):
            return "missing_helper_scope_evidence"
        if evidence.get("expected_helper_present") is not True:
            return "helper_not_confirmed_in_source_text"
        if evidence.get("corrected_has_helper") is not False:
            return "corrected_still_has_es_deldela"
        if evidence.get("corrected_has_dynamic_scope") is not True:
            return "corrected_missing_dynamic_scope"
        if evidence.get("literal_de_before_scope") is not True:
            return "missing_literal_de_before_scope"
    elif subpolicy_name == DYNAMIC_SCOPE_HELPER_SUBPOLICY:
        if evidence.get("helper_mode") not in {
            "es_article_helper_plain_name",
            "localplayerstring_invariant_literal",
        }:
            return "unsupported_dynamic_scope_helper_mode"
        if not evidence.get("helper_scope"):
            return "missing_helper_scope_evidence"
        if evidence.get("corrected_has_spanish_helper") is not False:
            return "corrected_still_has_spanish_dynamic_helper"
        if evidence.get("dynamic_scope_preserved") is not True:
            return "dynamic_scope_not_preserved"
        if (
            evidence.get("helper_mode") == "localplayerstring_invariant_literal"
            and evidence.get("helper_literal") != "é"
        ):
            return "non_ptbr_invariant_localplayer_literal"
    elif subpolicy_name == SELECT_CSTRING_DIRECT_NAME_SUBPOLICY:
        if evidence.get("select_cstring_direct_name_reference") is not True:
            return "missing_select_cstring_direct_name_evidence"
        if not evidence.get("select_scope") or not evidence.get("direct_name_scope"):
            return "missing_direct_name_scope_evidence"
        if evidence.get("select_scope") != evidence.get("direct_name_scope"):
            return "select_scope_direct_name_scope_mismatch"
        if evidence.get("corrected_has_direct_name") is not True:
            return "corrected_missing_direct_name_reference"
        if evidence.get("english_has_direct_name") is not True:
            return "english_missing_direct_name_reference"
        if evidence.get("corrected_has_select_cstring") is not False:
            return "corrected_still_has_select_cstring"
    elif subpolicy_name == LOCALPLAYERSTRING_WAR_JOIN_SUBPOLICY:
        if evidence.get("helper_mode") != "localplayerstring_war_join_message":
            return "missing_war_join_helper_mode_evidence"
        if evidence.get("helper_scope") != "Character":
            return "unexpected_war_join_helper_scope"
        if evidence.get("war_join_state") not in {"join", "not_join"}:
            return "missing_war_join_state_evidence"
        if evidence.get("dynamic_scope_removed") is not True:
            return "dynamic_scope_not_removed"
        if evidence.get("corrected_has_character_name") is not True:
            return "corrected_missing_character_name"
        if evidence.get("corrected_phrase_ok") is not True:
            return "corrected_war_join_phrase_not_verified"
        if evidence.get("interaction_token_preserved") is not True:
            return "interaction_token_not_preserved"
    elif subpolicy_name == ES_HELPER_NARRATIVE_SUBPOLICY:
        if not evidence.get("helper_infos"):
            return "missing_es_helper_narrative_evidence"
        if evidence.get("confirmed_contains_missing_helpers") is not True:
            return "helpers_not_confirmed_in_source_text"
        if evidence.get("corrected_removed_missing_helpers") is not True:
            return "helpers_not_removed_from_corrected_text"
        if int(evidence.get("corrected_helper_count") or 0) != 0:
            return "corrected_still_has_es_helper"
        if evidence.get("unsupported_nonhelper_missing"):
            return "unsupported_nonhelper_missing_token"
        if not (
            evidence.get("preserved_helper_scopes")
            or evidence.get("neutralized_helper_scopes")
            or evidence.get("direct_name_extra_scopes")
        ):
            return "missing_scope_strategy_evidence"
    elif subpolicy_name == HOSTAGE_CONTEXT_SUBPOLICY:
        if not evidence.get("helper_infos"):
            return "missing_hostage_helper_evidence"
        if evidence.get("confirmed_contains_missing_helpers") is not True:
            return "helpers_not_confirmed_in_source_text"
        if evidence.get("corrected_removed_missing_helpers") is not True:
            return "helpers_not_removed_from_corrected_text"
        if int(evidence.get("corrected_helper_count") or 0) != 0:
            return "corrected_still_has_es_helper"
        if evidence.get("unsupported_nonhelper_missing"):
            return "unsupported_nonhelper_missing_token"
        if evidence.get("corrected_has_hostage_term") is not True:
            return "corrected_missing_hostage_term"
        if (
            evidence.get("relation_removed") is True
            and evidence.get("relation_preserved_in_corrected") is not True
            and evidence.get("family_neutralization") is not True
        ):
            return "relation_removed_without_neutral_family_phrase"
    elif subpolicy_name == SHORT_DYNAMIC_VERB_SUBPOLICY:
        if evidence.get("helper_mode") != "short_dynamic_spanish_verb_neutralization":
            return "missing_short_dynamic_verb_evidence"
        if evidence.get("allowed_source_key") is not True:
            return "unsupported_short_dynamic_verb_key"
        if evidence.get("confirmed_select_cstring_spanish_literals") is not True:
            return "confirmed_select_cstring_spanish_literals_not_verified"
        if evidence.get("corrected_has_select_cstring") is not False:
            return "corrected_still_has_select_cstring"
        if evidence.get("corrected_removed_spanish_literals") is not True:
            return "spanish_dynamic_literals_not_removed"
        if evidence.get("required_tokens_preserved") is not True:
            return "required_tokens_not_preserved"
        if evidence.get("expected_phrase_present") is not True:
            return "expected_ptbr_phrase_missing"
        if evidence.get("english_alignment_ok") is not True:
            return "english_alignment_not_verified"
    elif subpolicy_name == HUNT_ACTIVITY_SELECT_CSTRING_SUBPOLICY:
        if evidence.get("helper_mode") != "hunt_activity_select_cstring_neutralization":
            return "missing_hunt_activity_select_cstring_evidence"
        if evidence.get("allowed_source_key") is not True:
            return "unsupported_hunt_activity_key"
        if evidence.get("confirmed_select_cstring_redundant_literal") is not True:
            return "confirmed_redundant_select_cstring_not_verified"
        if evidence.get("corrected_has_select_cstring") is not False:
            return "corrected_still_has_select_cstring"
        if evidence.get("corrected_removed_redundant_select") is not True:
            return "redundant_literal_not_preserved_as_text"
        if evidence.get("animal_article_marker_neutralized") is not True:
            return "animal_article_marker_not_neutralized"
        if evidence.get("required_tokens_preserved") is not True:
            return "required_tokens_not_preserved"
        if evidence.get("required_hunt_markers_present") is not True:
            return "required_hunt_markers_missing"
        if evidence.get("english_alignment_ok") is not True:
            return "english_alignment_not_verified"
    elif subpolicy_name == CORONATION_TITLE_ES_HELPER_SUBPOLICY:
        if evidence.get("helper_mode") != "coronation_title_es_helper_neutralization":
            return "missing_coronation_title_es_helper_evidence"
        if evidence.get("allowed_source_key") is not True:
            return "unsupported_coronation_title_key"
        if evidence.get("required_missing_tokens_present") is not True:
            return "required_missing_es_helpers_not_present"
        if evidence.get("confirmed_contains_missing_helpers") is not True:
            return "confirmed_helpers_not_verified"
        if evidence.get("corrected_removed_missing_helpers") is not True:
            return "expected_es_helpers_not_removed"
        if evidence.get("corrected_has_any_es_helper") is not False:
            return "corrected_still_has_es_helper"
        if evidence.get("corrected_tokens_preserved") is not True:
            return "corrected_missing_title_tokens"
        if evidence.get("corrected_markers_present") is not True:
            return "corrected_markers_not_verified"
        if evidence.get("spanish_residue_removed") is not True:
            return "spanish_residue_not_removed"
        if evidence.get("english_alignment_ok") is not True:
            return "english_alignment_not_verified"
    elif subpolicy_name == NICKNAME_WHISPERER_SELECT_CSTRING_SUBPOLICY:
        if evidence.get("helper_mode") != "nickname_whisperer_select_cstring_neutralization":
            return "missing_nickname_whisperer_select_cstring_evidence"
        if evidence.get("allowed_source_key") is not True:
            return "unsupported_nickname_whisperer_key"
        if evidence.get("required_missing_tokens_present") is not True:
            return "required_missing_tokens_not_present"
        if evidence.get("confirmed_contains_missing_tokens") is not True:
            return "confirmed_tokens_not_verified"
        if evidence.get("confirmed_redundant_select_cstring") is not True:
            return "redundant_select_cstring_not_verified"
        if evidence.get("corrected_removed_missing_tokens") is not True:
            return "expected_tokens_not_removed"
        if evidence.get("corrected_has_select_cstring") is not False:
            return "corrected_still_has_select_cstring"
        if evidence.get("corrected_has_any_es_helper") is not False:
            return "corrected_still_has_es_helper"
        if evidence.get("corrected_tokens_preserved") is not True:
            return "corrected_missing_character_name_token"
        if evidence.get("corrected_markers_present") is not True:
            return "corrected_markers_not_verified"
        if evidence.get("english_alignment_ok") is not True:
            return "english_alignment_not_verified"
    elif subpolicy_name == SINGLE_COMBAT_VICTOR_NAME_SUBPOLICY:
        if evidence.get("helper_mode") != "single_combat_victor_name_pronoun_neutralization":
            return "missing_single_combat_victor_name_evidence"
        if evidence.get("allowed_source_key") is not True:
            return "unsupported_single_combat_key"
        if evidence.get("no_missing_tokens") is not True:
            return "unexpected_missing_tokens"
        if evidence.get("expected_extra_tokens") is not True:
            return "unexpected_extra_tokens"
        if evidence.get("corrected_has_required_names") is not True:
            return "corrected_missing_required_sc_victor_names"
        if evidence.get("confirmed_did_not_have_name") is not True:
            return "confirmed_already_had_sc_victor_name"
        if evidence.get("english_pronouns_present") is not True:
            return "english_pronouns_not_verified"
        if evidence.get("corrected_markers_present") is not True:
            return "corrected_markers_not_verified"
        if evidence.get("confirmed_markers_present") is not True:
            return "confirmed_markers_not_verified"
        if evidence.get("quote_markers_preserved") is not True:
            return "quote_markers_not_preserved"
        if evidence.get("english_alignment_ok") is not True:
            return "english_alignment_not_verified"
    elif subpolicy_name == TOUR_TITLE_POSSESSIVE_SUBPOLICY:
        if evidence.get("helper_mode") != "tour_title_gendered_possessive_neutralization":
            return "missing_tour_title_possessive_evidence"
        if evidence.get("allowed_source_key") is not True:
            return "unsupported_tour_title_key"
        if evidence.get("no_missing_tokens") is not True:
            return "unexpected_missing_tokens"
        if evidence.get("expected_extra_tokens") is not True:
            return "unexpected_extra_tokens"
        if evidence.get("title_token_preserved") is not True:
            return "title_token_not_preserved"
        if evidence.get("confirmed_has_plain_possessive") is not True:
            return "confirmed_plain_possessive_not_verified"
        if evidence.get("corrected_has_gendered_possessive") is not True:
            return "corrected_gendered_possessive_not_verified"
        if evidence.get("confirmed_lacked_gendered_possessive") is not True:
            return "confirmed_already_had_gendered_possessive"
        if evidence.get("hunter_title_pair_preserved") is not True:
            return "hunter_title_pair_not_preserved"
        if evidence.get("corrected_starts_with_possessive") is not True:
            return "corrected_possessive_not_at_title_start"
        if evidence.get("english_alignment_ok") is not True:
            return "english_alignment_not_verified"
    elif subpolicy_name == EP3_TRAVEL_TITLE_ADJECTIVE_SUBPOLICY:
        if evidence.get("helper_mode") != "ep3_travel_title_adjective_alignment":
            return "missing_ep3_travel_title_adjective_evidence"
        if evidence.get("allowed_source_key") is not True:
            return "unsupported_ep3_travel_key"
        if evidence.get("required_missing_tokens_present") is not True:
            return "required_missing_tokens_not_matched"
        if evidence.get("expected_extra_tokens") is not True:
            return "unexpected_extra_tokens"
        if evidence.get("confirmed_tokens_present") is not True:
            return "confirmed_tokens_not_verified"
        if evidence.get("corrected_tokens_present") is not True:
            return "corrected_tokens_not_verified"
        if evidence.get("corrected_removed_es_helper") is not True:
            return "corrected_still_has_es_helper"
        if evidence.get("corrected_removed_old_title_name") is not True:
            return "corrected_still_has_old_title_name"
        if evidence.get("corrected_removed_title_style") is not True:
            return "corrected_still_has_title_style_variant"
        if evidence.get("corrected_markers_present") is not True:
            return "corrected_markers_not_verified"
        if evidence.get("english_alignment_ok") is not True:
            return "english_alignment_not_verified"
    else:
        return "unsupported_subpolicy"
    return ""


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    shadow_run: dict[str, Any],
    rows: list[dict[str, Any]],
    started_at: datetime,
    global_reasons: list[str],
    checkpoint_name: str,
    checkpoint_status: str,
    promotion_status: str,
) -> None:
    fieldnames = [
        "checkpoint_item_id",
        "subpolicy_shadow_item_id",
        "bridge_item_id",
        "repair_queue_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "boundary_policy",
        "policy_bucket",
        "risk_level",
        "subpolicy_status",
        "checkpoint_action",
        "checkpoint_allowed",
        "block_reason",
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
                "confirmed_preview": short(row.get("confirmed_text")),
                "corrected_preview": short(row.get("corrected_text")),
                "evidence": parse_json_obj(row.get("evidence_json")),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    counts = Counter("checkpoint_allowed" if row["checkpoint_allowed"] else row["block_reason"] for row in rows)
    policies = Counter(row["boundary_policy"] for row in rows if row["checkpoint_allowed"])
    lines = [
        "Auto-confirmation boundary token subpolicy checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint name: {checkpoint_name}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Checkpoint status: {checkpoint_status}",
        f"Promotion status: {promotion_status}",
        f"Subpolicy name: {shadow_run['subpolicy_name']}",
        f"Subpolicy shadow run id: {shadow_run['id']}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Governance gates:",
        f"- Shadow total: {int(shadow_run['total_candidates'] or 0):,}",
        f"- Shadow ready: {int(shadow_run['shadow_ready_count'] or 0):,}",
        f"- Shadow blocked: {int(shadow_run['blocked_count'] or 0):,}",
        f"- Validation issue rows: {int(shadow_run['validation_issue_count'] or 0):,}",
        "",
        "Checkpoint items:",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        "",
        "Allowed by boundary policy:",
        *[f"- {key}: {value:,}" for key, value in policies.most_common()],
        "",
        "Global blockers:",
        *([f"- {reason}" for reason in global_reasons] or ["- none"]),
        "",
        "Allowed sample:",
    ]
    for row in [item for item in rows if item["checkpoint_allowed"]][:25]:
        lines.extend(
            [
                f"- {row['relative_path']}:{row['source_line_number']}:{row['source_key']} | {row['boundary_policy']}",
                f"  corrected={short(row.get('corrected_text'))}",
            ]
        )
    if not any(item["checkpoint_allowed"] for item in rows):
        lines.append("- none")
    lines.extend(["", "Blocked sample:"])
    blocked = [item for item in rows if not item["checkpoint_allowed"]]
    if blocked:
        for row in blocked[:25]:
            lines.extend(
                [
                    f"- {row['block_reason']} | {row['relative_path']}:{row['source_line_number']}:{row['source_key']}",
                    f"  corrected={short(row.get('corrected_text'))}",
                ]
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Checkpoint only: no output writes, no confirmation updates, no token-policy decision approval.",
            "- This checkpoint only governs shadow-ready subpolicy rows.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    subpolicy_shadow_run_id: int | None = None,
    subpolicy_name: str = SELECT_CSTRING_SUBPOLICY,
    min_ready_required: int = 1,
    max_blocked_allowed: int = 0,
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    checkpoint_name = CHECKPOINT_NAMES.get(subpolicy_name)
    checkpoint_action = CHECKPOINT_ACTIONS.get(subpolicy_name)
    if not checkpoint_name or not checkpoint_action:
        raise ValueError(f"Unsupported token subpolicy checkpoint: {subpolicy_name}")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_shadow_run_id = subpolicy_shadow_run_id or latest_subpolicy_shadow_run_id(conn, subpolicy_name=subpolicy_name)
        shadow_run = fetch_shadow_run(conn, subpolicy_shadow_run_id=selected_shadow_run_id)
        rows = fetch_rows(conn, subpolicy_shadow_run_id=selected_shadow_run_id)
        if not rows:
            raise RuntimeError(f"Token subpolicy shadow run {selected_shadow_run_id} has no items.")

        global_reasons = evaluate_global(
            shadow_run,
            rows,
            subpolicy_name=subpolicy_name,
            min_ready_required=min_ready_required,
            max_blocked_allowed=max_blocked_allowed,
        )
        for row in rows:
            block_reason = row_block_reason(row, subpolicy_name=subpolicy_name)
            if global_reasons and not block_reason:
                block_reason = "global_gate:" + ",".join(global_reasons)
            row["checkpoint_action"] = checkpoint_action
            row["checkpoint_allowed"] = 0 if block_reason else 1
            row["block_reason"] = block_reason

        allowed_count = sum(int(row["checkpoint_allowed"]) for row in rows)
        blocked_count = len(rows) - allowed_count
        checkpoint_status = (
            "ready_for_shadow_lifecycle_policy"
            if allowed_count > 0 and not global_reasons
            else "blocked_by_checkpoint_guard"
        )
        promotion_status = "shadow_candidate" if checkpoint_status == "ready_for_shadow_lifecycle_policy" else "blocked"
        txt_path, csv_path, jsonl_path = report_paths(settings, subpolicy_name=subpolicy_name)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint_runs (
                rule_version,
                subpolicy_shadow_run_id,
                checkpoint_name,
                checkpoint_status,
                subpolicy_name,
                subpolicy_status,
                min_ready_required,
                max_blocked_allowed,
                total_candidates,
                ready_count,
                blocked_count,
                checkpoint_allowed_count,
                checkpoint_blocked_count,
                validation_issue_count,
                promotion_status,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_shadow_run_id,
                checkpoint_name,
                checkpoint_status,
                subpolicy_name,
                "shadow",
                min_ready_required,
                max_blocked_allowed,
                len(rows),
                int(shadow_run.get("shadow_ready_count") or 0),
                int(shadow_run.get("blocked_count") or 0),
                allowed_count,
                blocked_count,
                int(shadow_run.get("validation_issue_count") or 0),
                promotion_status,
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
                INSERT INTO auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint_items (
                    checkpoint_run_id,
                    subpolicy_shadow_run_id,
                    subpolicy_shadow_item_id,
                    bridge_run_id,
                    bridge_item_id,
                    repair_queue_item_id,
                    boundary_policy_item_id,
                    review_decision_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    boundary_agent_key,
                    boundary_policy,
                    policy_bucket,
                    risk_level,
                    subpolicy_status,
                    checkpoint_action,
                    checkpoint_allowed,
                    block_reason,
                    current_confirmed_text_hash,
                    corrected_text_hash,
                    evidence_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_run_id,
                    selected_shadow_run_id,
                    row["id"],
                    row["bridge_run_id"],
                    row["bridge_item_id"],
                    row["repair_queue_item_id"],
                    row["boundary_policy_item_id"],
                    row["review_decision_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    row["boundary_agent_key"],
                    row["boundary_policy"],
                    row["policy_bucket"],
                    row["risk_level"],
                    row["subpolicy_status"],
                    row["checkpoint_action"],
                    int(row["checkpoint_allowed"]),
                    row["block_reason"],
                    row.get("current_confirmed_text_hash"),
                    row.get("corrected_text_hash"),
                    row.get("evidence_json"),
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
            started_at=started_at,
            global_reasons=global_reasons,
            checkpoint_name=checkpoint_name,
            checkpoint_status=checkpoint_status,
            promotion_status=promotion_status,
        )
        conn.commit()

    print("[auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint] Checkpoint generated")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint] Checkpoint run id: {checkpoint_run_id}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint] Subpolicy shadow run id: {selected_shadow_run_id}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint] Status: {checkpoint_status}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint] Allowed: {allowed_count:,}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint] Blocked: {blocked_count:,}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint] Report: {txt_path}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint] CSV: {csv_path}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint] JSONL: {jsonl_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "subpolicy_shadow_run_id": selected_shadow_run_id,
        "checkpoint_status": checkpoint_status,
        "allowed_count": allowed_count,
        "blocked_count": blocked_count,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Checkpoint shadow token subpolicy rows before any lifecycle or apply path.")
    parser.add_argument("--subpolicy-shadow-run-id", type=int, default=None)
    parser.add_argument("--subpolicy-name", default=SELECT_CSTRING_SUBPOLICY)
    parser.add_argument("--min-ready", type=int, default=1)
    parser.add_argument("--max-blocked", type=int, default=0)
    args = parser.parse_args()
    main(
        subpolicy_shadow_run_id=args.subpolicy_shadow_run_id,
        subpolicy_name=args.subpolicy_name,
        min_ready_required=args.min_ready,
        max_blocked_allowed=args.max_blocked,
    )
