from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short
from ml_composite_subpolicy_diagnostic import classify_token_subtype, fetch_decisions
from segment_token_overlay_review_queue import (
    DEFAULT_ACTIVE_GATE_KEY,
    active_gate_overlay_run_id,
    enrich,
    fetch_rows,
)


RULE_VERSION = "ml_composite_subpolicy_promotion_audit_v1"


TOKEN_RE = re.compile(r"\[([^\]]+)\]")
METHOD_RE = re.compile(r"^([A-Za-z0-9_\.]+)\.([A-Za-z0-9_]+)")
GLOSSARY_RE = re.compile(r"Glossary\(\s*'([^']*)'\s*,\s*'([^']*)'\s*\)")
STYLE_SUFFIX_RE = re.compile(r"\|(?:<STYLE>|U|l|L|E|V|T)(?=\])")
PRONOUN_METHODS = {"GetSheHe", "GetHerHim", "GetHerHis", "GetHerselfHimself"}
NAME_METHODS = {
    "GetFirstName",
    "GetFirstNameNoTooltip",
    "GetTitledFirstName",
    "GetTitledFirstNameNoTooltip",
    "GetName",
    "GetNameNoTooltip",
}
GENDER_CUSTOM_MARKERS = {
    "ES_OA",
    "ES_XA",
    "ES_EA",
    "ES_ElLa",
    "ES_ElElla",
    "ES_DelDela",
    "ES_AlAla",
    "ES_LoLa",
}
TOKEN_ADDED_MANUAL_CONTEXT_MARKERS = {
    "CatStory",
    "DogStory",
    "HorseStory",
    "RandomWomanMan",
    "DescribingEducationOutcomeProwess",
}


def parse_csv_filter(value: str | None) -> set[str] | None:
    if not value:
        return None
    parsed = {part.strip() for part in value.split(",") if part.strip()}
    return parsed or None


def json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return [value]
    if isinstance(payload, list):
        return [str(item) for item in payload]
    return [str(payload)]


def token_parts(token: str) -> tuple[str, str]:
    match = TOKEN_RE.search(token)
    inner = match.group(1) if match else token.strip("[]")
    method_match = METHOD_RE.match(inner)
    if not method_match:
        return ("", inner.split("|", 1)[0])
    return (method_match.group(1), method_match.group(2).split("|", 1)[0])


def token_methods(tokens: list[str]) -> set[str]:
    return {token_parts(token)[1] for token in tokens}


def token_scopes(tokens: list[str]) -> set[str]:
    return {scope for scope, _method in (token_parts(token) for token in tokens) if scope}


def has_marker(tokens: list[str], markers: set[str]) -> bool:
    return any(marker in token for token in tokens for marker in markers)


def markers_in(tokens: list[str], markers: set[str]) -> set[str]:
    return {marker for token in tokens for marker in markers if marker in token}


def has_style_modifier(tokens: list[str]) -> bool:
    return any("|" in token for token in tokens)


def strip_style_modifier(token: str) -> str:
    return STYLE_SUFFIX_RE.sub("", token)


def same_tokens_after_style_strip(missing: list[str], extra: list[str]) -> bool:
    return (
        bool(missing)
        and len(missing) == len(extra)
        and Counter(strip_style_modifier(token) for token in missing)
        == Counter(strip_style_modifier(token) for token in extra)
    )


def glossary_entries(tokens: list[str]) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for token in tokens:
        match = GLOSSARY_RE.search(token)
        if not match:
            continue
        entries.append((match.group(1), match.group(2)))
    return entries


def all_glossary_tokens(tokens: list[str]) -> bool:
    return bool(tokens) and len(glossary_entries(tokens)) == len(tokens)


def glossary_key_counter(tokens: list[str]) -> Counter[str]:
    return Counter(key for _label, key in glossary_entries(tokens))


def glossary_labels(tokens: list[str]) -> list[str]:
    return [label for label, _key in glossary_entries(tokens)]


def has_question_marker(texts: list[str]) -> bool:
    return any("?" in text for text in texts)


def has_game_concept_token(tokens: list[str]) -> bool:
    return any("$game_concept_" in token for token in tokens)


def has_concept_placeholder(tokens: list[str]) -> bool:
    return any("Concept(" in token for token in tokens)


def has_any_text(tokens: list[str], markers: set[str]) -> bool:
    return any(marker in token for token in tokens for marker in markers)


def token_variants(token: str) -> set[str]:
    variants = {token}
    if "|<STYLE>" in token:
        for style in ("U", "l", "L", "E", "V", "T"):
            variants.add(token.replace("|<STYLE>", f"|{style}"))
        variants.add(token.replace("|<STYLE>", ""))
    elif token.endswith("]"):
        variants.add(token[:-1] + "|U]")
    return variants


def token_aligned_with_english(token: str, english_text: str | None) -> bool:
    english = english_text or ""
    return any(variant in english for variant in token_variants(token))


def all_tokens_aligned_with_english(tokens: list[str], english_text: str | None) -> bool:
    return bool(tokens) and all(token_aligned_with_english(token, english_text) for token in tokens)


def has_select_cstring(tokens: list[str]) -> bool:
    return any("Select_CString(" in token for token in tokens)


def has_localplayer_select(tokens: list[str]) -> bool:
    return any("Select_CString(" in token and "IsLocalPlayer" in token for token in tokens)


def has_isplayer_select(tokens: list[str]) -> bool:
    return any("Select_CString(" in token and "IsPlayer" in token for token in tokens)


def has_isfemale_select(tokens: list[str]) -> bool:
    return any("Select_CString(" in token and "IsFemale" in token for token in tokens)


def select_cstring_isfemale_scopes(tokens: list[str]) -> set[str]:
    scopes: set[str] = set()
    for token in tokens:
        if "Select_CString(" not in token or "IsFemale" not in token:
            continue
        for match in re.finditer(r"([A-Za-z0-9_.]+)\.IsFemale", token):
            scopes.add(match.group(1))
    return scopes


def all_selects_are_localplayer(tokens: list[str]) -> bool:
    selected = [token for token in tokens if "Select_CString(" in token]
    return bool(selected) and all("IsLocalPlayer" in token for token in selected)


def only_methods(tokens: list[str], methods: set[str]) -> bool:
    parsed = token_methods(tokens)
    return bool(parsed) and parsed <= methods


def same_single_scope(tokens: list[str]) -> bool:
    scopes = token_scopes(tokens)
    return len(scopes) == 1


def has_token_context(tokens: list[str], markers: set[str]) -> bool:
    return any(marker in token for token in tokens for marker in markers)


def rule_key_for(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    missing = row.get("missing_tokens") or []
    extra = row.get("extra_tokens") or []
    all_tokens = missing + extra
    subtype = row["token_subtype"]
    missing_methods = token_methods(missing)
    extra_methods = token_methods(extra)
    signature = {
        "missing_methods": sorted(missing_methods),
        "extra_methods": sorted(extra_methods),
        "missing_scopes": sorted(token_scopes(missing)),
        "extra_scopes": sorted(token_scopes(extra)),
        "missing_count": len(missing),
        "extra_count": len(extra),
    }

    if subtype == "glossary_label_translation":
        missing_glossary_keys = glossary_key_counter(missing)
        extra_glossary_keys = glossary_key_counter(extra)
        signature["missing_glossary_keys"] = sorted(missing_glossary_keys.elements())
        signature["extra_glossary_keys"] = sorted(extra_glossary_keys.elements())
        signature["missing_glossary_labels"] = glossary_labels(missing)
        signature["extra_glossary_labels"] = glossary_labels(extra)
        if not all_glossary_tokens(missing + extra):
            return ("glossary_label_translation_mixed_token_boundary", signature)
        if missing_glossary_keys != extra_glossary_keys:
            return ("glossary_label_translation_key_changed_boundary", signature)
        if has_question_marker(signature["missing_glossary_labels"] + signature["extra_glossary_labels"]):
            return ("same_glossary_key_label_text_hygiene_boundary", signature)
        if len(missing_glossary_keys) == 1 and sum(missing_glossary_keys.values()) == 1:
            return ("single_glossary_same_key_label_translation_candidate", signature)
        return ("multi_glossary_same_key_label_translation_candidate", signature)

    if subtype == "name_form_rewrite":
        signature["missing_has_style"] = has_style_modifier(missing)
        signature["extra_has_style"] = has_style_modifier(extra)
        signature["same_after_style_strip"] = same_tokens_after_style_strip(missing, extra)
        if (
            only_methods(all_tokens, NAME_METHODS)
            and token_scopes(missing) == token_scopes(extra)
            and same_tokens_after_style_strip(missing, extra)
            and has_style_modifier(missing)
            and not has_style_modifier(extra)
        ):
            return ("same_scope_name_form_style_removed_candidate", signature)
        if only_methods(all_tokens, NAME_METHODS) and token_scopes(missing) == token_scopes(extra):
            return ("same_scope_name_form_rewrite_candidate", signature)
        return ("name_form_rewrite_contextual_boundary", signature)

    if subtype == "tutorial_concept_exception_candidate":
        relative_path = str(row.get("relative_path") or "")
        signature["has_game_concept_extra"] = has_game_concept_token(extra)
        signature["has_concept_placeholder_missing"] = has_concept_placeholder(missing)
        if not relative_path.startswith("tutorial/"):
            return ("tutorial_concept_non_tutorial_boundary", signature)
        if not has_game_concept_token(extra):
            return ("tutorial_concept_without_game_concept_boundary", signature)
        if has_concept_placeholder(missing):
            return ("tutorial_concept_placeholder_rewrite", signature)
        if not missing:
            return ("tutorial_game_concept_addition", signature)
        return ("tutorial_concept_mixed_boundary", signature)

    if subtype == "dynamic_name_form_rewrite":
        if has_marker(all_tokens, {"GetFaith."}):
            return ("faith_name_adjective_semantic_rewrite", signature)
        if (
            missing_methods == {"GetFirstName"}
            and extra_methods == {"GetTitledFirstName"}
            and token_scopes(missing) == token_scopes(extra)
            and same_single_scope(all_tokens)
        ):
            return ("same_scope_first_name_to_titled_first_name", signature)
        if (
            missing_methods == {"GetFirstName"}
            and "GetFirstNameNoTooltip" in extra_methods
            and extra_methods <= NAME_METHODS | PRONOUN_METHODS
            and same_single_scope(extra)
        ):
            return ("same_scope_no_tooltip_name_plus_pronoun_alignment", signature)
        if only_methods(all_tokens, NAME_METHODS) and token_scopes(missing) == token_scopes(extra):
            return ("same_scope_name_form_style_simplification", signature)
        if only_methods(all_tokens, NAME_METHODS | PRONOUN_METHODS):
            return ("dynamic_name_plus_pronoun_contextual_rewrite", signature)
        return ("dynamic_name_form_contextual_rewrite", signature)

    if subtype == "dynamic_scope_rewrite":
        source_key = str(row.get("source_key") or "")
        relative_path = str(row.get("relative_path") or "")
        missing_text = " ".join(missing)
        extra_text = " ".join(extra)
        signature["missing_has_style"] = has_style_modifier(missing)
        signature["extra_has_style"] = has_style_modifier(extra)
        if source_key.startswith("DEBATE_PROGRESS_MARKER_"):
            return ("debate_progress_marker_localplayer_scope_rephrase", signature)
        if (
            missing_methods == {"GetShortUIName"}
            and extra_methods == {"GetShortUIName"}
            and token_scopes(missing) == token_scopes(extra)
            and not has_style_modifier(missing)
            and has_style_modifier(extra)
        ):
            return ("same_scope_short_ui_name_style_alignment", signature)
        if "Select_Cstring(" in missing_text and "Select_Cstring(" in extra_text:
            return ("select_cstring_literal_translation_same_structure", signature)
        if source_key == "request_contract_assistance_interaction_condition_tt":
            return ("localplayer_string_removed_without_replacement_boundary", signature)
        if source_key == "agent_events.3001.desc":
            return ("scheme_name_style_removed_text_cleanup_boundary", signature)
        if relative_path == "nicknames_l_spanish.yml":
            return ("nickname_localplayer_string_to_plain_name_manual_exception", signature)
        if "house_feud" in relative_path or source_key.startswith("INTERACTION_FAMILY_FEUD"):
            return ("house_feud_dynamic_scope_manual_exception", signature)
        if source_key == "ep3_contract_event.0082.to_liege":
            return ("task_contract_liege_destination_scope_manual_exception", signature)
        return ("dynamic_scope_rewrite_candidate_rule", signature)

    if subtype == "name_and_gender_token_rewrite":
        english_aligned = all_tokens_aligned_with_english(extra, row.get("english_text"))
        missing_scopes = token_scopes(missing)
        extra_scopes = token_scopes(extra)
        same_scopes = missing_scopes == extra_scopes
        extra_superset = missing_scopes <= extra_scopes
        missing_markers = markers_in(missing, GENDER_CUSTOM_MARKERS)
        extra_markers = markers_in(extra, GENDER_CUSTOM_MARKERS)
        signature["missing_gender_markers"] = sorted(missing_markers)
        signature["extra_gender_markers"] = sorted(extra_markers)
        signature["english_aligned_extra"] = english_aligned
        signature["same_scopes"] = same_scopes
        signature["extra_scope_superset"] = extra_superset
        signature["missing_has_style"] = has_style_modifier(missing)
        signature["extra_has_style"] = has_style_modifier(extra)

        if not same_scopes:
            if extra_superset and extra_methods <= NAME_METHODS | {"Custom"}:
                added_scopes = extra_scopes - missing_scopes
                if has_any_text(extra, {"CatStoryName", "DogStoryName"}):
                    return ("mixed_extra_pet_story_name_with_owner_rewrite", signature)
                if added_scopes == {"ROOT.Char"} and extra_markers <= {"ES_OA"}:
                    return ("mixed_extra_root_gender_self_adjective_context", signature)
                if added_scopes == {"house_feud_victim"}:
                    return ("mixed_house_feud_spouse_victim_possessive_context", signature)
                return ("mixed_name_gender_extra_scope_title_or_pet_name_context", signature)
            if extra_methods & PRONOUN_METHODS:
                source_key = str(row.get("source_key") or "")
                relative_path = str(row.get("relative_path") or "")
                if source_key == "bp1_yearly.3001.desc":
                    return ("friend_financial_help_name_pronoun_clean_candidate", signature)
                if source_key == "dungeon_ongoing.0011.desc":
                    return ("prisoner_imprisoner_cross_scope_negative_boundary", signature)
                if source_key in {
                    "tour_general.4007.desc",
                    "vassal.5050.desc",
                    "trait_specific.6002.desc",
                    "party_baron.3003.desc",
                }:
                    return ("multi_name_gender_pronoun_manual_exception_boundary", signature)
                if relative_path == "dlc/ep3/ep3_frankokratia_events_l_spanish.yml":
                    return ("frankokratia_glossary_name_gender_manual_boundary", signature)
                if source_key in {
                    "nomad_events_oltner.0006.desc",
                    "mpo_decisions_events.0112.desc",
                    "emperor_imperial_examination.200.desc",
                    "scheme_critical_moments.1226.desc_warring",
                    "az_tour.0005.desc",
                }:
                    return ("multi_character_name_pronoun_needs_subpolicy", signature)
                return ("mixed_name_gender_scope_shift_with_pronoun_context", signature)
            return ("mixed_name_gender_scope_shift_contextual_rewrite", signature)

        if missing_methods == {"GetTitledFirstName"} and extra_methods == {"GetHerHis", "GetTitledFirstName"}:
            if has_style_modifier(extra):
                return ("same_scope_name_possessive_style_manual_boundary", signature)
            if english_aligned:
                return ("same_scope_plain_name_plus_possessive_english_aligned", signature)
            return ("same_scope_plain_name_plus_possessive_pt_rephrase", signature)

        if (
            missing_methods <= {"Custom", "GetTitledFirstName", "GetTitledFirstNameNoTooltip", "GetTitledFirstNameRegnal"}
            and extra_methods <= NAME_METHODS
            and missing_markers
            and not extra_markers
        ):
            if len(missing_scopes) > 1 or len(extra_scopes) > 1:
                return ("same_scope_multi_name_article_title_cleanup", signature)
            if missing_markers <= {"ES_DelDela"}:
                return ("same_scope_de_name_title_possessive_context", signature)
            if missing_markers <= {"ES_ElLa", "ES_AlAla"}:
                if english_aligned:
                    return ("same_scope_article_title_to_plain_name_english_aligned", signature)
                return ("same_scope_article_title_to_plain_name_pt_rephrase", signature)
            if missing_markers <= {"ES_OA", "ES_EA", "ES_XA"}:
                return ("same_scope_gender_suffix_title_cleanup_context", signature)
            return ("same_scope_gendered_article_title_cleanup_context", signature)

        if missing_methods <= {"Custom", "GetTitledFirstName"} and extra_methods & PRONOUN_METHODS:
            if english_aligned:
                source_key = str(row.get("source_key") or "")
                relative_path = str(row.get("relative_path") or "")
                if source_key in {
                    "fp1_yearly.2500.desc",
                    "elope.0001.positive.desc",
                    "elope.0001.negative.desc",
                    "learn_language_ongoing.1040.desc",
                }:
                    return ("same_scope_name_plus_pronoun_clean_narrative_candidate", signature)
                if source_key in {"laamp_base_learning_contract_events.4027.desc.success.qualified", "pregnancy.3004.desc"}:
                    return ("same_scope_name_plus_pronoun_manual_exception_boundary", signature)
                if relative_path == "event_localization/activities/feast_events_ewan_l_spanish.yml" or source_key == "agent_events.4011.desc":
                    return ("same_scope_name_plus_pronoun_needs_subpolicy", signature)
                return ("same_scope_name_plus_pronoun_english_aligned", signature)
            return ("same_scope_name_plus_pronoun_pt_rephrase", signature)

        if extra_methods & PRONOUN_METHODS:
            source_key = str(row.get("source_key") or "")
            if source_key.startswith("murder_save."):
                return ("same_scope_murder_save_pet_pronoun_name_rewrite", signature)
            if missing_scopes == {"emperor"} and extra_scopes == {"emperor"}:
                return ("same_scope_liege_title_pronoun_boundary", signature)
            if missing_scopes == {"2001_precarious_entourage_character", "ROOT.Char"}:
                return ("same_scope_entourage_observation_pronoun_alignment", signature)
            if missing_scopes == {"random_knight"} and extra_scopes == {"random_knight"}:
                return ("same_scope_training_opponent_multi_pronoun_rewrite", signature)
            return ("mixed_name_gender_pronoun_contextual_rewrite", signature)

        return ("name_and_gender_token_rewrite_contextual_rule", signature)

    if subtype == "pronoun_form_swap":
        missing_scopes = token_scopes(missing)
        extra_scopes = token_scopes(extra)
        same_scopes = missing_scopes == extra_scopes
        missing_markers = markers_in(missing, GENDER_CUSTOM_MARKERS)
        extra_markers = markers_in(extra, GENDER_CUSTOM_MARKERS)
        signature["missing_gender_markers"] = sorted(missing_markers)
        signature["extra_gender_markers"] = sorted(extra_markers)
        signature["same_scopes"] = same_scopes
        signature["missing_has_style"] = has_style_modifier(missing)
        signature["extra_has_style"] = has_style_modifier(extra)

        if not same_scopes:
            return ("cross_scope_pronoun_form_swap_needs_subpolicy", signature)
        if len(missing_scopes | extra_scopes) > 1:
            return ("multi_scope_pronoun_form_swap_needs_subpolicy", signature)
        if missing_markers or extra_markers:
            return ("article_or_gender_plus_pronoun_form_swap_needs_subpolicy", signature)

        if missing_methods == {"GetSheHe"} and extra_methods == {"GetHerHim"}:
            if len(extra) > len(missing):
                return ("same_scope_subject_to_repeated_object_pronoun_candidate", signature)
            return ("same_scope_subject_to_object_pronoun_candidate", signature)
        if missing_methods == {"GetSheHe"} and extra_methods == {"GetSheHe"}:
            if has_style_modifier(extra) and not has_style_modifier(missing):
                return ("same_scope_subject_pronoun_style_uppercase_candidate", signature)
            return ("same_scope_subject_pronoun_style_swap_candidate", signature)
        if missing_methods == {"GetSheHe"} and extra_methods == {"GetHerHim", "GetSheHe"}:
            if has_style_modifier(extra):
                return ("same_scope_object_plus_styled_subject_pronoun_candidate", signature)
            return ("same_scope_subject_object_pronoun_form_swap_candidate", signature)
        if missing_methods == {"GetHerHim"} and extra_methods == {"GetHerHis"}:
            return ("same_scope_object_to_possessive_path_context_candidate", signature)
        if (
            missing_methods == {"GetHerHim"}
            and extra_methods == {"GetHerHim"}
            and has_style_modifier(missing)
            and not has_style_modifier(extra)
        ):
            return ("same_scope_object_pronoun_style_removed_candidate", signature)

        return ("pronoun_form_swap_contextual_needs_subpolicy", signature)

    if subtype == "pronoun_added_for_pt_fluency":
        english_aligned = all_tokens_aligned_with_english(extra, row.get("english_text"))
        if missing:
            missing_scopes = token_scopes(missing)
            extra_scopes = token_scopes(extra)
            missing_markers = markers_in(missing, GENDER_CUSTOM_MARKERS)
            extra_markers = markers_in(extra, GENDER_CUSTOM_MARKERS)
            signature["missing_gender_markers"] = sorted(missing_markers)
            signature["extra_gender_markers"] = sorted(extra_markers)
            signature["english_aligned_extra"] = english_aligned
            signature["same_scopes"] = missing_scopes == extra_scopes
            signature["extra_scope_superset"] = missing_scopes <= extra_scopes
            signature["extra_scope_subset"] = extra_scopes <= missing_scopes
            signature["missing_has_style"] = has_style_modifier(missing)
            signature["extra_has_style"] = has_style_modifier(extra)

            if (
                missing_markers >= {"ES_OA", "ES_XA"}
                and only_methods(extra, PRONOUN_METHODS)
                and same_single_scope(extra)
            ):
                return ("single_scope_gendered_person_phrase_to_pronoun_boundary", signature)

            if only_methods(extra, PRONOUN_METHODS) and same_single_scope(extra):
                if english_aligned:
                    if extra_methods == {"GetSheHe"}:
                        if missing_scopes != extra_scopes:
                            return ("cross_scope_missing_gender_to_subject_pronoun_boundary", signature)
                        article_markers = {"ES_ElLa", "ES_ElElla", "ES_DelDela", "ES_AlAla", "ES_LoLa"}
                        if missing_markers & article_markers:
                            if len(extra) == 1:
                                return (
                                    "single_scope_missing_article_to_subject_pronoun_english_aligned",
                                    signature,
                                )
                            return (
                                "single_scope_missing_article_to_repeated_subject_pronoun_english_aligned",
                                signature,
                            )
                        source_key = str(row.get("source_key") or "")
                        relative_path = str(row.get("relative_path") or "")
                        if source_key in {"bp1_yearly.4010.desc_end", "bp1_yearly.5302.desc"}:
                            return (
                                "single_scope_missing_gender_suffix_to_subject_pronoun_bp1_boundary",
                                signature,
                            )
                        if source_key == "hold_court.3020.desc":
                            return (
                                "single_scope_missing_gender_suffix_to_subject_pronoun_hold_court_boundary",
                                signature,
                            )
                        if relative_path.startswith("dlc/bp1/"):
                            return (
                                "single_scope_missing_gender_suffix_to_subject_pronoun_bp1_boundary",
                                signature,
                            )
                        if len(missing) > 1:
                            return ("single_scope_missing_multi_gender_suffix_to_subject_pronoun_boundary", signature)
                        return (
                            "single_scope_missing_gender_suffix_to_subject_pronoun_clean_context",
                            signature,
                        )
                    if extra_methods == {"GetHerHim"}:
                        return ("single_scope_missing_gender_to_object_pronoun_english_aligned", signature)
                    if extra_methods == {"GetHerHis"}:
                        return ("single_scope_missing_gender_to_possessive_pronoun_english_aligned", signature)
                    if extra_methods == {"GetSheHe", "GetHerHis"}:
                        return (
                            "single_scope_missing_gender_to_subject_possessive_pronouns_english_aligned",
                            signature,
                        )
                    return ("single_scope_missing_gender_to_pronoun_set_english_aligned", signature)
                if extra_methods == {"GetHerHim"} and len(missing) == 1:
                    return ("single_scope_missing_article_to_object_pronoun_pt_rephrase_candidate", signature)
                if len(missing) > 1:
                    return ("single_scope_missing_multi_gender_to_pronoun_pt_boundary", signature)
                return ("single_scope_missing_gender_to_pronoun_pt_rephrase", signature)

            if extra_methods <= (PRONOUN_METHODS | {"Custom"}) and same_single_scope(extra):
                if english_aligned:
                    return ("single_scope_missing_gender_suffix_pronoun_english_aligned", signature)
                source_key = str(row.get("source_key") or "")
                relative_path = str(row.get("relative_path") or "")
                if extra_methods == {"Custom", "GetSheHe"}:
                    if source_key == "fp1_shieldmaiden.0021.desc.child_pushed":
                        return ("single_scope_missing_object_article_to_subject_gender_suffix_short_context", signature)
                    if relative_path.startswith("event_localization/travel_events/") or source_key == "yearly.8400.desc":
                        return ("single_scope_missing_gender_suffix_subject_pronoun_narrative_boundary", signature)
                    if len(missing) > 1 or len(extra) > 2:
                        return ("single_scope_missing_complex_gender_suffix_subject_pronoun_boundary", signature)
                if extra_methods == {"Custom", "GetHerHim"} and source_key == "yearly.3001.b":
                    return ("single_scope_short_accuse_object_pronoun_gender_suffix", signature)
                return ("single_scope_missing_gender_suffix_pronoun_pt_rephrase", signature)

            if only_methods(extra, PRONOUN_METHODS):
                if english_aligned:
                    return ("multi_scope_missing_gender_pronoun_bridge_english_aligned", signature)
                return ("multi_scope_missing_gender_pronoun_bridge_context", signature)

            if extra_methods & PRONOUN_METHODS:
                return ("missing_gender_plus_contextual_pronoun_rewrite", signature)

            return ("pronoun_added_with_missing_gender_or_pronoun", signature)
        gender_markers = markers_in(extra, GENDER_CUSTOM_MARKERS)
        if gender_markers:
            signature["gender_markers"] = sorted(gender_markers)
            signature["same_extra_scope"] = same_single_scope(extra)
            if not same_single_scope(extra):
                return ("multi_scope_gender_suffix_pronoun_added", signature)
            if "GetWomanMan" in extra_methods:
                return ("single_scope_gendered_person_noun_rewrite", signature)
            if extra_methods <= {"Custom", "GetSheHe"}:
                article_markers = {"ES_ElLa", "ES_DelDela", "ES_AlAla", "ES_LoLa"}
                if gender_markers & article_markers:
                    return ("single_scope_article_gender_suffix_subject_pronoun", signature)
                return ("single_scope_gender_suffix_subject_pronoun", signature)
            if extra_methods <= {"Custom", "GetHerHim"}:
                return ("single_scope_gender_suffix_object_pronoun", signature)
            if extra_methods <= {"Custom", "GetSheHe", "GetHerHim"}:
                return ("single_scope_gender_suffix_subject_object_pronouns", signature)
            return ("pronoun_plus_gender_suffix_added", signature)
        if only_methods(extra, PRONOUN_METHODS) and same_single_scope(extra):
            if row.get("relative_path") == "battles_l_spanish.yml":
                return ("battle_ui_object_pronoun_boundary", signature)
            if extra_methods == {"GetSheHe"}:
                if english_aligned and len(extra) == 1:
                    return ("single_scope_single_subject_pronoun_english_aligned", signature)
                if english_aligned:
                    return ("single_scope_repeated_subject_pronoun_english_aligned", signature)
                source_key = str(row.get("source_key") or "")
                relative_path = str(row.get("relative_path") or "")
                if source_key == "major_decisions.5001.desc":
                    return ("single_scope_subject_pronoun_major_decision_pt_rephrase", signature)
                if relative_path.startswith("dlc/bp2/"):
                    return ("single_scope_subject_pronoun_relationship_boundary", signature)
                return ("single_scope_subject_pronoun_pt_rephrase", signature)
            if extra_methods == {"GetHerHim"}:
                scope = next(iter(token_scopes(extra)), "")
                english = row.get("english_text") or ""
                has_unmirrored_possessive = bool(scope and f"[{scope}.GetHerHis" in english)
                imperative_possessive = bool(
                    scope
                    and re.search(
                        rf"\bGive\s+\[{re.escape(scope)}\.GetHerHim[^\]]*\]\s+\[{re.escape(scope)}\.GetHerHis",
                        english,
                    )
                )
                if english_aligned and has_unmirrored_possessive and imperative_possessive:
                    return ("single_scope_object_pronoun_imperative_possessive_boundary", signature)
                if english_aligned and has_unmirrored_possessive:
                    return ("single_scope_object_pronoun_partial_possessive_context", signature)
                if english_aligned and len(extra) == 1:
                    return ("single_scope_single_object_pronoun_english_aligned", signature)
                if english_aligned:
                    return ("single_scope_repeated_object_pronoun_english_aligned", signature)
                return ("single_scope_object_pronoun_pt_rephrase", signature)
            if extra_methods == {"GetHerHis"}:
                if english_aligned:
                    source_key = str(row.get("source_key") or "")
                    confirmed = row.get("confirmed_text") or ""
                    if source_key == "bp1_yearly.2023.desc_picked_diplomacy" or re.search(
                        r"\bjeito\s+\[[^\]]+\.GetHerHis", confirmed
                    ):
                        return ("single_scope_possessive_pronoun_idiomatic_way_boundary", signature)
                    return ("single_scope_possessive_pronoun_english_aligned", signature)
                return ("single_scope_possessive_pronoun_pt_rephrase", signature)
            if extra_methods == {"GetSheHe", "GetHerHis"}:
                if english_aligned:
                    return ("single_scope_subject_possessive_pronouns_english_aligned", signature)
                source_key = str(row.get("source_key") or "")
                relative_path = str(row.get("relative_path") or "")
                if source_key == "travel_events.3061.intro" or relative_path.startswith(
                    "event_localization/travel_events/"
                ):
                    return ("single_scope_pronoun_set_travel_narrative_boundary", signature)
                return ("single_scope_subject_possessive_pronouns_pt_rephrase", signature)
            if extra_methods == {"GetSheHe", "GetHerHim"}:
                source_key = str(row.get("source_key") or "")
                relative_path = str(row.get("relative_path") or "")
                if source_key.startswith("travel_events.") or relative_path.startswith(
                    "event_localization/travel_events/"
                ):
                    return ("single_scope_subject_object_pronoun_travel_boundary", signature)
                return ("single_scope_subject_object_pronoun_pt_rephrase", signature)
            if english_aligned:
                if len(extra) <= 2:
                    return ("single_scope_pair_pronouns_english_aligned", signature)
                return ("single_scope_multi_pronoun_set_english_aligned", signature)
            return ("single_scope_pronoun_set_pt_rephrase", signature)
        if only_methods(extra, PRONOUN_METHODS):
            if english_aligned:
                return ("multi_scope_pronoun_bridge_english_aligned", signature)
            return ("multi_scope_pronoun_bridge_added", signature)
        return ("pronoun_added_contextual_rewrite", signature)

    if subtype == "pronoun_perspective_rewrite":
        missing_scopes = token_scopes(missing)
        extra_scopes = token_scopes(extra)
        missing_markers = markers_in(missing, GENDER_CUSTOM_MARKERS)
        extra_markers = markers_in(extra, GENDER_CUSTOM_MARKERS)
        missing_text = " ".join(missing)
        extra_text = " ".join(extra)
        signature["missing_gender_markers"] = sorted(missing_markers)
        signature["extra_gender_markers"] = sorted(extra_markers)
        signature["same_scopes"] = missing_scopes == extra_scopes
        signature["extra_scope_superset"] = missing_scopes <= extra_scopes
        signature["missing_has_style"] = has_style_modifier(missing)
        signature["extra_has_style"] = has_style_modifier(extra)

        if "\\n" in missing_text or "\\n" in extra_text:
            return ("newline_plus_pronoun_perspective_needs_subpolicy", signature)
        if "Glossary(" in missing_text or "Glossary(" in extra_text:
            return ("glossary_plus_pronoun_perspective_needs_subpolicy", signature)
        if not missing_scopes <= extra_scopes and extra_methods & PRONOUN_METHODS:
            return ("cross_scope_pronoun_perspective_needs_subpolicy", signature)
        if len(missing_scopes | extra_scopes) > 1 and not missing_scopes == extra_scopes:
            return ("multi_scope_pronoun_perspective_needs_subpolicy", signature)
        if any(
            marker in missing_text or marker in extra_text
            for marker in {
                "GetTitleAsName",
                "GetTierAsName",
                "GetCouncilTitle",
                "GetPrimaryTitle",
            }
        ):
            return ("title_style_plus_pronoun_perspective_needs_subpolicy", signature)
        if has_token_context(missing, {"Custom('<TEXT>')"}) and extra_methods & PRONOUN_METHODS:
            if extra_methods <= {"GetSheHe"}:
                return ("custom_text_to_subject_pronoun_needs_subpolicy", signature)
            if extra_methods <= {"GetHerHim"}:
                return ("custom_text_to_object_pronoun_needs_subpolicy", signature)
            return ("custom_text_to_pronoun_set_needs_subpolicy", signature)
        if missing_markers or extra_markers:
            return ("gender_custom_plus_pronoun_perspective_needs_subpolicy", signature)
        if has_token_context(
            all_tokens,
            {
                "SisterBrother",
                "GirlBoy",
                "RomanticComplimentNoun",
                "GetCourtierPlural",
                "FighterTermPlural",
            },
        ):
            return ("role_or_relation_custom_plus_pronoun_needs_subpolicy", signature)
        if extra_methods & PRONOUN_METHODS:
            return ("pronoun_perspective_contextual_needs_subpolicy", signature)
        return ("pronoun_perspective_rewrite_candidate_rule", signature)

    if subtype == "mixed_unclassified_token_rewrite":
        english_aligned = all_tokens_aligned_with_english(extra, row.get("english_text"))
        missing_scopes = token_scopes(missing)
        extra_scopes = token_scopes(extra)
        same_scopes = missing_scopes == extra_scopes
        signature["english_aligned_extra"] = english_aligned
        signature["same_scopes"] = same_scopes
        signature["missing_has_style"] = has_style_modifier(missing)
        signature["extra_has_style"] = has_style_modifier(extra)

        if english_aligned:
            if (
                missing_methods == {"GetTierAsNameNoTooltip"}
                and extra_methods == {"GetTierAsNamePossessiveNoTooltip"}
                and same_scopes
            ):
                return ("title_tier_possessive_name_english_aligned", signature)
            if missing_methods == {"completely_controls"} and extra_methods == {"completely_control"}:
                return ("achievement_concept_singular_form_english_aligned", signature)
            if missing_methods <= {"GetAdherentName", "GetAdherentNameNoTooltip"} and extra_methods <= {
                "GetAdjective",
                "GetAdjectiveNoTooltip",
            }:
                return ("faith_adherent_to_adjective_english_aligned", signature)
            if missing_methods == {"CreatorSheHe"} and extra_methods == {"CreatorHerHim"} and same_scopes:
                return ("faith_creator_subject_to_object_english_aligned", signature)
            if missing_methods == {"Custom"} and extra_methods == {"Custom"}:
                if has_any_text(extra, {"GetWomenOrMenBasedOnFaith"}):
                    return ("faith_gender_group_custom_literal_english_aligned", signature)
                if has_any_text(extra, {"GetRandomRegionalBird"}):
                    return ("regional_custom_style_case_english_aligned", signature)
                if has_any_text(extra, {"DogStoryHerHim"}):
                    return ("dog_story_object_pronoun_custom_english_aligned", signature)
                if not same_scopes:
                    return ("custom_scope_shift_english_aligned", signature)
                return ("custom_literal_english_aligned", signature)
            if missing_methods == extra_methods and same_scopes:
                if not has_style_modifier(missing) and has_style_modifier(extra):
                    return ("same_token_style_modifier_english_aligned", signature)
                return ("same_token_form_english_aligned", signature)
            if not same_scopes:
                return ("scope_shift_english_aligned", signature)

        source_key = str(row.get("source_key") or "")
        if source_key == "board_games.0041.desc.middle.03":
            return ("board_game_type_custom_style_manual_boundary", signature)
        if missing_methods == {"top_liege"} and extra_methods == {"top_liege_possessive"}:
            return ("concept_top_liege_possessive_shift_needs_subpolicy", signature)
        if missing_methods == {"Custom"} and extra_methods == {"Custom"} and same_scopes:
            if not has_style_modifier(missing) and has_style_modifier(extra):
                return ("same_scope_custom_style_case_pt_rewrite", signature)
            return ("same_scope_custom_literal_pt_rewrite", signature)
        if source_key == "tgp_travel_events.0041.g":
            return ("travel_farmer_child_custom_scope_rewrite", signature)

        return ("mixed_unclassified_token_rewrite_candidate_rule", signature)

    if subtype == "token_added_contextual_exception":
        english_aligned = all_tokens_aligned_with_english(extra, row.get("english_text"))
        gender_markers = markers_in(extra, GENDER_CUSTOM_MARKERS)
        manual_markers = {
            marker for marker in TOKEN_ADDED_MANUAL_CONTEXT_MARKERS if has_token_context(extra, {marker})
        }
        signature["english_aligned_extra"] = english_aligned
        signature["extra_gender_markers"] = sorted(gender_markers)
        signature["manual_context_markers"] = sorted(manual_markers)
        signature["extra_has_style"] = has_style_modifier(extra)

        if missing:
            return ("token_added_with_missing_token_boundary", signature)
        if gender_markers or manual_markers:
            return ("token_added_contextual_manual_exception", signature)
        return ("token_added_english_reference_restored", signature)

    if subtype == "select_cstring_literalized_context":
        missing_text = " ".join(missing)
        confirmed = row.get("confirmed_text") or ""
        if all_selects_are_localplayer(missing) and not extra:
            return ("localplayer_select_cstring_ui_neutralization", signature)
        if has_isplayer_select(missing) and not extra:
            return ("isplayer_select_cstring_ui_neutralization", signature)
        if has_isfemale_select(missing) and not extra:
            if "ROOT.Char.IsFemale" in missing_text:
                return ("root_char_select_cstring_literalized_context_boundary", signature)
            if "$ep3_laamp_decision_event.1025.desc_tribal_legendary_body$" in confirmed:
                return ("gender_select_cstring_literalized_scripted_body_boundary", signature)
            if "GetWomanMan" in confirmed or "Custom('ES_XA')" in confirmed:
                return ("gender_select_cstring_literalized_with_gender_custom_boundary", signature)
            return ("gender_select_cstring_literalized_context", signature)
        if (
            "LocalPlayerString" in " ".join(missing)
            and any(".GetShortUIName" in token for token in extra)
            and "ep3_eparch_events" in (row.get("relative_path") or "")
        ):
            return ("localplayer_string_eparch_title_boundary", signature)
        return ("select_cstring_literalized_context_candidate_rule", signature)

    if subtype == "select_cstring_multi_dynamic_rewrite":
        relative_path = row.get("relative_path") or ""
        source_key = row.get("source_key") or ""
        if (
            relative_path == "nicknames_l_spanish.yml"
            and source_key.startswith("nick_")
            and all_selects_are_localplayer(missing)
        ):
            if has_marker(extra, GENDER_CUSTOM_MARKERS):
                return ("nickname_select_cstring_localplayer_to_plain_name_gender_suffix", signature)
            return ("nickname_select_cstring_localplayer_to_plain_name_rewrite", signature)
        if relative_path == "secrets_l_spanish.yml" and source_key.endswith("_tooltip_desc"):
            if has_localplayer_select(missing) and has_isplayer_select(missing) and not extra:
                return ("secret_tooltip_select_cstring_player_to_plain_name_rewrite", signature)
            return ("secret_tooltip_select_cstring_boundary", signature)
        if all_selects_are_localplayer(missing) and not extra:
            if any("Or(" in token for token in missing) and (row.get("source_key") or "").endswith("_title"):
                return ("multi_localplayer_select_cstring_title_boundary", signature)
            return ("multi_localplayer_select_cstring_ui_neutralization", signature)
        if all_selects_are_localplayer(missing) and "concept" in set(row.get("token_families") or []):
            return ("multi_localplayer_select_cstring_concept_alert_boundary", signature)
        if has_isfemale_select(missing) and (extra_methods & PRONOUN_METHODS or has_marker(extra, GENDER_CUSTOM_MARKERS)):
            return ("multi_gender_select_cstring_narrative_rewrite", signature)
        return ("select_cstring_multi_dynamic_rewrite_candidate_rule", signature)

    if subtype == "select_cstring_to_custom_gender_rewrite":
        if has_localplayer_select(missing) and has_marker(extra, GENDER_CUSTOM_MARKERS):
            return ("localplayer_select_to_gender_suffix_ui_neutralization", signature)
        if has_isfemale_select(extra) and has_marker(missing, GENDER_CUSTOM_MARKERS):
            missing_select_scopes = select_cstring_isfemale_scopes(missing)
            extra_select_scopes = select_cstring_isfemale_scopes(extra)
            signature["missing_select_scopes"] = sorted(missing_select_scopes)
            signature["extra_select_scopes"] = sorted(extra_select_scopes)
            if missing_select_scopes and extra_select_scopes and missing_select_scopes != extra_select_scopes:
                return ("custom_gender_to_select_cstring_scope_mismatch_boundary", signature)
            return ("custom_gender_to_select_cstring_gendered_phrase", signature)
        if has_isfemale_select(missing) and has_marker(extra, GENDER_CUSTOM_MARKERS):
            missing_text = " ".join(missing)
            extra_text = " ".join(extra)
            if "Glossary(" in missing_text or "Glossary(" in extra_text:
                return ("glossary_select_to_custom_gender_needs_subpolicy", signature)
            if "ROOT.Char.IsFemale" in missing_text:
                return ("root_char_select_to_custom_gender_manual_boundary", signature)
            if len(missing) == 1 and len(extra) == 1:
                return ("single_scope_isfemale_select_to_gender_suffix_clean", signature)
            return ("isfemale_select_to_custom_gender_rewrite", signature)
        return ("select_cstring_to_custom_gender_rewrite_candidate_rule", signature)

    if subtype == "gender_custom_form_swap":
        missing_scopes = token_scopes(missing)
        extra_scopes = token_scopes(extra)
        missing_markers = markers_in(missing, GENDER_CUSTOM_MARKERS)
        extra_markers = markers_in(extra, GENDER_CUSTOM_MARKERS)
        same_scopes = missing_scopes == extra_scopes
        signature["missing_gender_markers"] = sorted(missing_markers)
        signature["extra_gender_markers"] = sorted(extra_markers)
        signature["same_scopes"] = same_scopes
        signature["extra_scope_superset"] = missing_scopes <= extra_scopes
        signature["missing_has_style"] = has_style_modifier(missing)
        signature["extra_has_style"] = has_style_modifier(extra)

        if not same_scopes:
            if missing_markers == extra_markers:
                return ("cross_scope_same_gender_marker_correction_needs_subpolicy", signature)
            if "ROOT.Char" in extra_scopes:
                return ("cross_scope_player_gender_suffix_rewrite_needs_subpolicy", signature)
            return ("cross_scope_gender_custom_form_swap_needs_subpolicy", signature)
        if missing_markers <= {"ES_OA"} and extra_markers <= {"ES_XA"}:
            if len(missing) > 1 and len(extra) == 1:
                return ("same_scope_repeated_es_oa_to_single_es_xa_candidate", signature)
            if len(missing) == 1 and len(extra) == 1:
                return ("same_scope_es_oa_to_es_xa_article_candidate", signature)
            return ("same_scope_es_oa_to_es_xa_contextual_candidate", signature)
        if missing_markers & {"ES_AlAla", "ES_LoLa", "ES_DelDela"}:
            return ("same_scope_preposition_article_gender_form_needs_subpolicy", signature)
        if missing_markers or extra_markers:
            return ("same_scope_gender_custom_form_swap_needs_subpolicy", signature)
        return ("gender_custom_form_swap_contextual_rule", signature)

    if subtype == "gender_token_rewrite":
        source_key = str(row.get("source_key") or "")
        relative_path = str(row.get("relative_path") or "")
        missing_markers = markers_in(missing, GENDER_CUSTOM_MARKERS)
        extra_markers = markers_in(extra, GENDER_CUSTOM_MARKERS)
        signature["missing_gender_markers"] = sorted(missing_markers)
        signature["extra_gender_markers"] = sorted(extra_markers)
        signature["missing_has_style"] = has_style_modifier(missing)
        signature["extra_has_style"] = has_style_modifier(extra)

        if extra and not missing:
            if "GetWomanMan" in extra_methods:
                return ("gender_person_noun_added_needs_subpolicy", signature)
            if extra_markers:
                return ("gender_article_inserted_apposition_needs_subpolicy", signature)
            return ("gender_token_added_contextual_rule", signature)

        if missing and not extra:
            if "GetWomanMan" in missing_methods:
                return ("redundant_gender_person_noun_removed_needs_subpolicy", signature)
            if len(missing) > 1:
                if relative_path == "event_localization/travel_events/travel_danger_events_joe_l_spanish.yml":
                    return ("travel_danger_repeated_article_removed_needs_subpolicy", signature)
                return ("repeated_gender_article_removed_needs_subpolicy", signature)
            if missing_markers & {"ES_AlAla", "ES_LoLa"}:
                return ("object_or_preposition_gender_article_removed_needs_subpolicy", signature)
            if source_key in {
                "destiny_child.1030.desc",
                "ep3_frankokratia_events.0080.desc",
                "tour_travel.3001.desc.drunkard",
                "diplomacy_majesty.4030.desc",
                "travel_danger_events.6010.ally_victim",
            }:
                return ("single_gender_article_removed_manual_exception_boundary", signature)
            if source_key in {
                "scheme_critical_moments.8052.desc",
                "scheme_critical_moments.8053.desc",
                "mpo_nerge.1060.desc",
                "paiza_artifact_desc",
                "tgp_japan_decision.9913.b",
                "emperor_imperial_examination.400.desc",
                "disbelieve_mandala_ongoing.0001.desc",
                "disbelieve_mandala_ongoing.0010.desc",
                "dungeon_ongoing.0013.desc_intro",
                "false_conversion.1010.desc_spouse_advises_conversion_cynical",
                "sway_ongoing.5013.desc_opening",
                "iberia_north_africa.0112.desc.intro",
            }:
                return ("single_gender_article_removed_before_name_clean_candidate", signature)
            if source_key in {
                "peasant_affair.2003.desc_dislike_their_child",
            }:
                return ("possessive_gender_article_removed_needs_subpolicy", signature)
            return ("single_gender_article_removed_contextual_rule", signature)

        return ("gender_token_rewrite_candidate_rule", signature)

    return (f"{subtype}_candidate_rule", signature)


def latest_diagnostic_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_composite_subpolicy_diagnostic_runs
        WHERE finished_at IS NOT NULL
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No ml_composite_subpolicy_diagnostic_runs entry found.")
    return int(row["id"])


def fetch_candidate_groups(
    conn,
    *,
    diagnostic_run_id: int,
    statuses: set[str],
    routes: set[str] | None,
    subtypes: set[str] | None,
) -> list[dict[str, Any]]:
    placeholders = ", ".join("?" for _ in statuses)
    params: list[Any] = [diagnostic_run_id, *sorted(statuses)]
    filters = [f"run_id = ?", f"maturity_status IN ({placeholders})"]
    if routes:
        filters.append("suggested_route IN ({})".format(", ".join("?" for _ in routes)))
        params.extend(sorted(routes))
    if subtypes:
        filters.append("token_subtype IN ({})".format(", ".join("?" for _ in subtypes)))
        params.extend(sorted(subtypes))
    rows = conn.execute(
        f"""
        SELECT *
        FROM ml_composite_subpolicy_diagnostic_items
        WHERE {' AND '.join(filters)}
        ORDER BY
            CASE maturity_status
                WHEN 'policy_candidate_review' THEN 0
                WHEN 'ready_to_design_subpolicy' THEN 1
                ELSE 9
            END,
            suggested_route,
            token_subtype
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def promotion_status(counts: Counter[str], *, min_accept: int) -> tuple[str, str, str]:
    accept = counts["accept_count"]
    keep = counts["keep_manual_exception_count"]
    reject = counts["reject_count"]
    needs = counts["needs_subpolicy_count"]
    fix = counts["fix_count"]
    reviewed = counts["reviewed_items"]
    pending = counts["pending_items"]

    if accept >= min_accept and pending and not any((keep, reject, needs, fix)):
        return (
            "needs_remaining_review_before_guarded_policy",
            "candidate",
            "review_remaining_examples_before_guarded_overlay",
        )
    if accept >= min_accept and not any((keep, reject, needs, fix)):
        return (
            "ready_for_guarded_policy_review",
            "candidate",
            "design_explicit_guarded_rule_and_test_overlay",
        )
    if accept and any((keep, reject, needs, fix)):
        return (
            "split_required_conflicting_evidence",
            "mixed",
            "split_rule_or_collect_targeted_negative_pairs",
        )
    if reject:
        return ("negative_boundary_rule", "blocked", "keep_blocked_and_collect_more_boundaries")
    if needs:
        return ("not_promotable_needs_subpolicy", "design", "split_into_smaller_subpolicy")
    if keep:
        return ("manual_exception_boundary", "manual", "do_not_generalize_keep_as_reviewed_exception")
    if accept:
        return ("needs_more_positive_evidence", "low", "collect_more_positive_examples_for_same_rule")
    if reviewed:
        return ("reviewed_without_positive_signal", "low", "inspect_decision_mix")
    return ("pending_review_only", "low", "queue_or_review_more_examples")


def supports_guarded_policy_release(route: str, subtype: str, rule_key: str = "") -> bool:
    if route == "gender_pronoun_english_aligned_subpolicy" and subtype == "pronoun_added_for_pt_fluency":
        return True
    if route == "gender_token_subspecialist_review" and subtype == "pronoun_added_for_pt_fluency":
        return True
    if route == "gender_token_subspecialist_review" and subtype == "gender_token_rewrite":
        return rule_key == "single_gender_article_removed_before_name_clean_candidate"
    if route == "gender_token_subspecialist_review" and subtype == "gender_custom_form_swap":
        return rule_key == "same_scope_es_oa_to_es_xa_article_candidate"
    if route == "gender_token_subspecialist_review" and subtype == "pronoun_form_swap":
        return rule_key == "same_scope_subject_to_object_pronoun_candidate"
    if route == "mixed_token_change_review" and subtype == "name_form_rewrite":
        return rule_key == "same_scope_name_form_style_removed_candidate"
    if route == "mixed_token_change_review" and subtype == "glossary_label_translation":
        return rule_key in {
            "single_glossary_same_key_label_translation_candidate",
            "multi_glossary_same_key_label_translation_candidate",
        }
    if route == "tutorial_concept_exception_subpolicy_review" and subtype == "tutorial_concept_exception_candidate":
        return rule_key in {
            "tutorial_game_concept_addition",
            "tutorial_concept_placeholder_rewrite",
        }
    if route == "mixed_token_change_review" and subtype == "name_and_gender_token_rewrite":
        return True
    if route == "select_cstring_dynamic_context_review" and subtype in {
        "select_cstring_literalized_context",
        "select_cstring_multi_dynamic_rewrite",
        "select_cstring_to_custom_gender_rewrite",
    }:
        return True
    if route == "token_added_review" and subtype == "token_added_contextual_exception":
        return True
    return False


def build_audit_rows(
    *,
    active_rows: list[dict[str, Any]],
    decisions: dict[int, dict[str, Any]],
    candidate_groups: list[dict[str, Any]],
    min_accept: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidate_keys = {(row["suggested_route"], row["token_subtype"]) for row in candidate_groups}
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    item_rows: list[dict[str, Any]] = []

    for row in active_rows:
        subtype, families = classify_token_subtype(row)
        row["token_subtype"] = subtype
        row["token_families"] = sorted(families)
        group_key = (row["suggested_route"], subtype)
        if group_key not in candidate_keys:
            continue

        rule_key, signature = rule_key_for(row)
        decision = decisions.get(int(row["policy_item_id"]))
        decision_label = decision["decision"] if decision else ""
        item = {
            **row,
            "rule_key": rule_key,
            "rule_signature": signature,
            "decision": decision_label,
            "decision_notes": decision.get("notes") if decision else "",
            "approved_for_apply": int(decision["approved_for_apply"] or 0) if decision else 0,
            "pending_decision": 0 if decision else 1,
            "dry_run_action": "evidence_only_no_apply",
        }
        item_rows.append(item)
        grouped[(row["suggested_route"], subtype, rule_key)].append(item)

    summary_rows: list[dict[str, Any]] = []
    for (route, subtype, rule_key), rows in sorted(grouped.items()):
        counts: Counter[str] = Counter()
        signatures = Counter(json.dumps(row["rule_signature"], sort_keys=True, ensure_ascii=False) for row in rows)
        for row in rows:
            decision = row["decision"]
            structural_accept_with_cleanup = (
                decision == "encoding_cleanup_required"
                and "token_release_safe_but_text_hygiene_blocks_apply" in (row.get("decision_notes") or "")
            )
            if decision:
                counts["reviewed_items"] += 1
                counts[f"decision_{decision}"] += 1
            else:
                counts["pending_items"] += 1
            if decision == "accept_policy_candidate" or structural_accept_with_cleanup:
                counts["accept_count"] += 1
            if structural_accept_with_cleanup:
                counts["text_cleanup_block_count"] += 1
            elif decision == "keep_manual_exception_only":
                counts["keep_manual_exception_count"] += 1
            elif decision == "reject_policy_candidate":
                counts["reject_count"] += 1
            elif decision == "needs_subpolicy":
                counts["needs_subpolicy_count"] += 1
            elif decision in {"fix_confirmed_text", "encoding_cleanup_required", "manual_token_rewrite_required"}:
                counts["fix_count"] += 1
            if row["approved_for_apply"]:
                counts["approved_for_apply_count"] += 1

        status, confidence, action = promotion_status(counts, min_accept=min_accept)
        if status in {"ready_for_guarded_policy_review", "needs_remaining_review_before_guarded_policy"} and not supports_guarded_policy_release(route, subtype, rule_key):
            status = "not_promotable_unsupported_guarded_route"
            confidence = "design"
            action = "design_route_specific_guarded_release_profile_before_promotion"
        sample_rows = rows[:8]
        summary_rows.append(
            {
                "suggested_route": route,
                "token_subtype": subtype,
                "rule_key": rule_key,
                "total_items": len(rows),
                "reviewed_items": counts["reviewed_items"],
                "pending_items": counts["pending_items"],
                "accept_count": counts["accept_count"],
                "keep_manual_exception_count": counts["keep_manual_exception_count"],
                "reject_count": counts["reject_count"],
                "needs_subpolicy_count": counts["needs_subpolicy_count"],
                "fix_count": counts["fix_count"],
                "text_cleanup_block_count": counts["text_cleanup_block_count"],
                "approved_for_apply_count": counts["approved_for_apply_count"],
                "promotion_status": status,
                "confidence_band": confidence,
                "recommended_action": action,
                "sample_policy_item_ids": [row["policy_item_id"] for row in sample_rows],
                "sample_paths": [
                    f"{row['relative_path']}:{row['source_line_number']}:{row['source_key']}"
                    for row in sample_rows
                ],
                "token_signature": json.loads(signatures.most_common(1)[0][0]) if signatures else {},
            }
        )
    return summary_rows, item_rows


def write_outputs(
    settings: dict[str, Any],
    *,
    active_gate: dict[str, Any],
    diagnostic_run_id: int,
    candidate_groups: list[dict[str, Any]],
    summary_rows: list[dict[str, Any]],
    item_rows: list[dict[str, Any]],
    min_accept: int,
    started_at: datetime,
) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = started_at.strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{timestamp}_ml_composite_subpolicy_promotion_audit"
    txt_path = base.with_suffix(".txt")
    csv_path = base.with_suffix(".csv")
    jsonl_path = base.with_suffix(".jsonl")

    fieldnames = [
        "suggested_route",
        "token_subtype",
        "rule_key",
        "total_items",
        "reviewed_items",
        "pending_items",
        "accept_count",
        "keep_manual_exception_count",
        "reject_count",
        "needs_subpolicy_count",
        "fix_count",
        "text_cleanup_block_count",
        "approved_for_apply_count",
        "promotion_status",
        "confidence_band",
        "recommended_action",
        "sample_policy_item_ids",
        "sample_paths",
        "token_signature",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in summary_rows:
            writer.writerow(
                {
                    key: json.dumps(row[key], ensure_ascii=False)
                    if key in {"sample_policy_item_ids", "sample_paths", "token_signature"}
                    else row[key]
                    for key in fieldnames
                }
            )

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in item_rows:
            payload = {
                "policy_item_id": row["policy_item_id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_line_number": row["source_line_number"],
                "source_key": row["source_key"],
                "suggested_route": row["suggested_route"],
                "token_subtype": row["token_subtype"],
                "rule_key": row["rule_key"],
                "decision": row["decision"],
                "approved_for_apply": row["approved_for_apply"],
                "pending_decision": row["pending_decision"],
                "dry_run_action": row["dry_run_action"],
                "missing_tokens": row.get("missing_tokens") or [],
                "extra_tokens": row.get("extra_tokens") or [],
                "rule_signature": row["rule_signature"],
                "confirmed_text": row.get("confirmed_text"),
                "output_text": row.get("output_text"),
            }
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    status_counts = Counter(row["promotion_status"] for row in summary_rows)
    route_counts = Counter(row["suggested_route"] for row in item_rows)
    candidate_group_lines = [
        "- {route} / {subtype}: reviewed={reviewed}, accept={accept}, keep={keep}, needs={needs}".format(
            route=row["suggested_route"],
            subtype=row["token_subtype"],
            reviewed=row["reviewed_items"],
            accept=row["accept_count"],
            keep=row["keep_manual_exception_count"],
            needs=row["needs_subpolicy_count"],
        )
        for row in candidate_groups
    ]
    lines = [
        "ML composite subpolicy promotion audit",
        f"Rule version: {RULE_VERSION}",
        f"Gate key: {active_gate['gate_key']}",
        f"Checkpoint id: {active_gate['active_checkpoint_id']}",
        f"Overlay run id: {active_gate['active_overlay_run_id']}",
        f"Policy run id: {active_gate['active_policy_run_id']}",
        f"Diagnostic run id: {diagnostic_run_id}",
        f"Minimum accept decisions per guarded rule: {min_accept}",
        "",
        "Summary:",
        f"- Candidate groups inspected: {len(candidate_groups)}",
        f"- Rule families found: {len(summary_rows)}",
        f"- Candidate items inspected: {len(item_rows)}",
        "- Apply allowed: 0",
        "",
        "Candidate groups:",
        *(candidate_group_lines or ["- none"]),
        "",
        "Promotion statuses:",
        *[f"- {key}: {value}" for key, value in status_counts.most_common()],
        "",
        "Routes inspected:",
        *[f"- {key}: {value}" for key, value in route_counts.most_common()],
        "",
        "Rule families:",
    ]
    for row in summary_rows:
        lines.append(
            "- {route} / {subtype} / {rule}: status={status}, confidence={confidence}, "
            "total={total}, reviewed={reviewed}, pending={pending}, accept={accept}, keep={keep}, "
            "reject={reject}, needs={needs}, fix={fix}, text_cleanup={text_cleanup}, "
            "approved={approved}, action={action}".format(
                route=row["suggested_route"],
                subtype=row["token_subtype"],
                rule=row["rule_key"],
                status=row["promotion_status"],
                confidence=row["confidence_band"],
                total=row["total_items"],
                reviewed=row["reviewed_items"],
                pending=row["pending_items"],
                accept=row["accept_count"],
                keep=row["keep_manual_exception_count"],
                reject=row["reject_count"],
                needs=row["needs_subpolicy_count"],
                fix=row["fix_count"],
                text_cleanup=row["text_cleanup_block_count"],
                approved=row["approved_for_apply_count"],
                action=row["recommended_action"],
            )
        )
    lines.extend(
        [
            "",
            "Interpretation:",
            "- accept_policy_candidate is the main positive signal for rule promotion.",
            "- encoding_cleanup_required also counts as positive when notes mark token_release_safe_but_text_hygiene_blocks_apply.",
            "- keep_manual_exception_only remains useful evidence, but blocks automatic generalization for that rule family.",
            "- needs_subpolicy and reject_policy_candidate both act as brakes until the subtype is split further.",
            "- This audit writes reports and DB rows only; it does not promote, retrain, or alter output files.",
            "",
            "Positive evidence sample:",
        ]
    )
    for row in [item for item in item_rows if item["decision"] == "accept_policy_candidate"][:40]:
        lines.extend(
            [
                (
                    f"- item {row['policy_item_id']} | segment {row['segment_id']} | "
                    f"{row['source_key']} | {row['token_subtype']} / {row['rule_key']}"
                ),
                f"  EXTRA: {json.dumps(row.get('extra_tokens') or [], ensure_ascii=False)}",
                f"  MISSING: {json.dumps(row.get('missing_tokens') or [], ensure_ascii=False)}",
                f"  CONFIRMED: {short(row.get('confirmed_text'))}",
            ]
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path


def insert_run(
    conn,
    *,
    active_gate: dict[str, Any],
    diagnostic_run_id: int,
    candidate_groups: list[dict[str, Any]],
    summary_rows: list[dict[str, Any]],
    report_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    started_at: datetime,
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    status_counts = Counter(row["promotion_status"] for row in summary_rows)
    cursor = conn.execute(
        """
        INSERT INTO ml_composite_subpolicy_promotion_audit_runs (
            rule_version,
            gate_key,
            checkpoint_id,
            overlay_run_id,
            source_policy_run_id,
            diagnostic_run_id,
            candidate_group_count,
            rule_family_count,
            ready_rule_count,
            collect_more_count,
            manual_boundary_count,
            split_required_count,
            not_promotable_count,
            pending_only_count,
            report_path,
            csv_path,
            jsonl_path,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            active_gate["gate_key"],
            active_gate["active_checkpoint_id"],
            active_gate["active_overlay_run_id"],
            active_gate["active_policy_run_id"],
            diagnostic_run_id,
            len(candidate_groups),
            len(summary_rows),
            status_counts["ready_for_guarded_policy_review"],
            status_counts["needs_more_positive_evidence"],
            status_counts["manual_exception_boundary"],
            status_counts["split_required_conflicting_evidence"],
            (
                status_counts["not_promotable_needs_subpolicy"]
                + status_counts["negative_boundary_rule"]
                + status_counts["reviewed_without_positive_signal"]
            ),
            status_counts["pending_review_only"],
            str(report_path),
            str(csv_path),
            str(jsonl_path),
            started_at.isoformat(timespec="seconds"),
            now,
            now,
        ),
    )
    run_id = int(cursor.lastrowid)
    for row in summary_rows:
        conn.execute(
            """
            INSERT INTO ml_composite_subpolicy_promotion_audit_items (
                run_id,
                suggested_route,
                token_subtype,
                rule_key,
                total_items,
                reviewed_items,
                pending_items,
                accept_count,
                keep_manual_exception_count,
                reject_count,
                needs_subpolicy_count,
                fix_count,
                text_cleanup_block_count,
                approved_for_apply_count,
                promotion_status,
                confidence_band,
                recommended_action,
                sample_policy_item_ids_json,
                sample_paths_json,
                token_signature_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                row["suggested_route"],
                row["token_subtype"],
                row["rule_key"],
                row["total_items"],
                row["reviewed_items"],
                row["pending_items"],
                row["accept_count"],
                row["keep_manual_exception_count"],
                row["reject_count"],
                row["needs_subpolicy_count"],
                row["fix_count"],
                row["text_cleanup_block_count"],
                row["approved_for_apply_count"],
                row["promotion_status"],
                row["confidence_band"],
                row["recommended_action"],
                json.dumps(row["sample_policy_item_ids"], ensure_ascii=False),
                json.dumps(row["sample_paths"], ensure_ascii=False),
                json.dumps(row["token_signature"], ensure_ascii=False),
                now,
            ),
        )
    return run_id


def main(
    *,
    gate_key: str = DEFAULT_ACTIVE_GATE_KEY,
    diagnostic_run_id: int | None = None,
    statuses_value: str = "policy_candidate_review,conflicting_evidence",
    routes_value: str | None = None,
    subtypes_value: str | None = None,
    min_accept: int = 5,
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    statuses = parse_csv_filter(statuses_value) or {"policy_candidate_review"}
    routes = parse_csv_filter(routes_value)
    subtypes = parse_csv_filter(subtypes_value)

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_diagnostic_run_id = diagnostic_run_id or latest_diagnostic_run_id(conn)
        overlay_run_id, active_gate = active_gate_overlay_run_id(conn, gate_key=gate_key)
        raw_rows = fetch_rows(conn, overlay_run_id=overlay_run_id, critical_only=False)
        active_rows = [enrich(row) for row in raw_rows]
        decisions = fetch_decisions(conn, policy_run_id=int(active_gate["active_policy_run_id"]))
        candidate_groups = fetch_candidate_groups(
            conn,
            diagnostic_run_id=selected_diagnostic_run_id,
            statuses=statuses,
            routes=routes,
            subtypes=subtypes,
        )
        summary_rows, item_rows = build_audit_rows(
            active_rows=active_rows,
            decisions=decisions,
            candidate_groups=candidate_groups,
            min_accept=min_accept,
        )
        report_path, csv_path, jsonl_path = write_outputs(
            settings,
            active_gate=active_gate,
            diagnostic_run_id=selected_diagnostic_run_id,
            candidate_groups=candidate_groups,
            summary_rows=summary_rows,
            item_rows=item_rows,
            min_accept=min_accept,
            started_at=started_at,
        )
        run_id = insert_run(
            conn,
            active_gate=active_gate,
            diagnostic_run_id=selected_diagnostic_run_id,
            candidate_groups=candidate_groups,
            summary_rows=summary_rows,
            report_path=report_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            started_at=started_at,
        )
        conn.commit()

    status_counts = Counter(row["promotion_status"] for row in summary_rows)
    print("[ml_composite_subpolicy_promotion_audit] Promotion audit generated")
    print(f"[ml_composite_subpolicy_promotion_audit] Rule version: {RULE_VERSION}")
    print(f"[ml_composite_subpolicy_promotion_audit] Run id: {run_id}")
    print(f"[ml_composite_subpolicy_promotion_audit] Diagnostic run id: {selected_diagnostic_run_id}")
    print(f"[ml_composite_subpolicy_promotion_audit] Gate key: {active_gate['gate_key']}")
    print(f"[ml_composite_subpolicy_promotion_audit] Candidate groups: {len(candidate_groups)}")
    print(f"[ml_composite_subpolicy_promotion_audit] Rule families: {len(summary_rows)}")
    for key, value in status_counts.most_common():
        print(f"[ml_composite_subpolicy_promotion_audit] {key}: {value}")
    print("[ml_composite_subpolicy_promotion_audit] Apply allowed: 0")
    print(f"[ml_composite_subpolicy_promotion_audit] Report: {report_path}")
    print(f"[ml_composite_subpolicy_promotion_audit] CSV: {csv_path}")
    print(f"[ml_composite_subpolicy_promotion_audit] JSONL: {jsonl_path}")
    return {
        "run_id": run_id,
        "diagnostic_run_id": selected_diagnostic_run_id,
        "report_path": str(report_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "candidate_groups": len(candidate_groups),
        "rule_families": len(summary_rows),
        "status_counts": dict(status_counts),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dry-run promotion audit for composite token subpolicy candidates.")
    parser.add_argument("--gate-key", default=DEFAULT_ACTIVE_GATE_KEY)
    parser.add_argument("--diagnostic-run-id", type=int, default=None)
    parser.add_argument("--statuses", default="policy_candidate_review")
    parser.add_argument("--routes", default=None)
    parser.add_argument("--subtypes", default=None)
    parser.add_argument("--min-accept", type=int, default=5)
    args = parser.parse_args()
    main(
        gate_key=args.gate_key,
        diagnostic_run_id=args.diagnostic_run_id,
        statuses_value=args.statuses,
        routes_value=args.routes,
        subtypes_value=args.subtypes,
        min_accept=args.min_accept,
    )
