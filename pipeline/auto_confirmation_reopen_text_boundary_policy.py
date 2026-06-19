from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short, structural_tokens


RULE_VERSION = "auto_confirmation_reopen_text_boundary_policy_v1"
DEFAULT_POLICY = "all"

POLICIES: dict[str, dict[str, str]] = {
    "weak_auto_visible_runtime_spanish_verb": {
        "agent_key": "weak_auto_visible_runtime_spanish_verb_boundary",
        "parent_agent_key": "weak_auto_token_surface_semantic_boundary",
        "policy_name": "weak_auto_visible_runtime_spanish_verb_boundary_v1",
    },
    "weak_auto_visible_possessive_connector_loss": {
        "agent_key": "weak_auto_visible_possessive_connector_loss_boundary",
        "parent_agent_key": "weak_auto_token_surface_semantic_boundary",
        "policy_name": "weak_auto_visible_possessive_connector_loss_boundary_v1",
    },
    "weak_auto_visible_sentence_collapse": {
        "agent_key": "weak_auto_visible_sentence_collapse_boundary",
        "parent_agent_key": "weak_auto_token_surface_semantic_boundary",
        "policy_name": "weak_auto_visible_sentence_collapse_boundary_v1",
    },
    "weak_auto_visible_semantic_sentence_loss": {
        "agent_key": "weak_auto_visible_semantic_sentence_loss_boundary",
        "parent_agent_key": "weak_auto_token_surface_semantic_boundary",
        "policy_name": "weak_auto_visible_semantic_sentence_loss_boundary_v1",
    },
    "weak_auto_visible_copula_token_form": {
        "agent_key": "weak_auto_visible_copula_token_form_boundary",
        "parent_agent_key": "weak_auto_token_surface_semantic_boundary",
        "policy_name": "weak_auto_visible_copula_token_form_boundary_v1",
    },
    "weak_auto_embedded_glossary_visible_label": {
        "agent_key": "weak_auto_embedded_glossary_visible_label_boundary",
        "parent_agent_key": "weak_auto_embedded_literal_token_specialist",
        "policy_name": "weak_auto_embedded_glossary_visible_label_boundary_v1",
    },
    "weak_auto_embedded_select_cstring_spanish_literal": {
        "agent_key": "weak_auto_embedded_select_cstring_spanish_literal_boundary",
        "parent_agent_key": "weak_auto_embedded_literal_token_specialist",
        "policy_name": "weak_auto_embedded_select_cstring_spanish_literal_boundary_v1",
    },
    "weak_auto_custom_loc_es_helper": {
        "agent_key": "weak_auto_custom_loc_es_helper_boundary",
        "parent_agent_key": "weak_auto_custom_loc_helper_boundary",
        "policy_name": "weak_auto_custom_loc_es_helper_boundary_v1",
    },
    "weak_auto_dynamic_select_cstring_spanish_literal": {
        "agent_key": "weak_auto_dynamic_select_cstring_spanish_literal_boundary",
        "parent_agent_key": "weak_auto_dynamic_spanish_literal_boundary",
        "policy_name": "weak_auto_dynamic_select_cstring_spanish_literal_boundary_v1",
    },
    "weak_auto_residual_spanish_literal_repair": {
        "agent_key": "weak_auto_residual_spanish_literal_boundary",
        "parent_agent_key": "weak_auto_residual_spanish_boundary",
        "policy_name": "weak_auto_residual_spanish_literal_repair_boundary_v1",
    },
    "weak_auto_short_local_player_sua_pessoa": {
        "agent_key": "weak_auto_short_local_player_sua_pessoa_boundary",
        "parent_agent_key": "weak_auto_short_exact_sampler",
        "policy_name": "weak_auto_short_local_player_sua_pessoa_boundary_v1",
    },
    "weak_auto_short_dynamic_spanish_verb": {
        "agent_key": "weak_auto_short_dynamic_spanish_verb_boundary",
        "parent_agent_key": "weak_auto_short_exact_sampler",
        "policy_name": "weak_auto_short_dynamic_spanish_verb_boundary_v1",
    },
    "weak_auto_short_geo_spanish_direction": {
        "agent_key": "weak_auto_short_geo_spanish_direction_boundary",
        "parent_agent_key": "weak_auto_short_exact_sampler",
        "policy_name": "weak_auto_short_geo_spanish_direction_boundary_v1",
    },
    "weak_auto_short_english_interjection": {
        "agent_key": "weak_auto_short_english_interjection_boundary",
        "parent_agent_key": "weak_auto_short_exact_sampler",
        "policy_name": "weak_auto_short_english_interjection_boundary_v1",
    },
    "weak_auto_short_semantic_label_mismatch": {
        "agent_key": "weak_auto_short_semantic_label_mismatch_boundary",
        "parent_agent_key": "weak_auto_short_exact_sampler",
        "policy_name": "weak_auto_short_semantic_label_mismatch_boundary_v1",
    },
    "weak_auto_short_custom_loc_article_helper": {
        "agent_key": "weak_auto_short_custom_loc_article_helper_boundary",
        "parent_agent_key": "weak_auto_short_exact_sampler",
        "policy_name": "weak_auto_short_custom_loc_article_helper_boundary_v1",
    },
    "weak_auto_short_adjective_label_style": {
        "agent_key": "weak_auto_short_adjective_label_style_boundary",
        "parent_agent_key": "weak_auto_short_exact_sampler",
        "policy_name": "weak_auto_short_adjective_label_style_boundary_v1",
    },
    "weak_auto_short_credit_title_style": {
        "agent_key": "weak_auto_short_credit_title_style_boundary",
        "parent_agent_key": "weak_auto_short_exact_sampler",
        "policy_name": "weak_auto_short_credit_title_style_boundary_v1",
    },
    "weak_auto_short_possessive_phrase_style": {
        "agent_key": "weak_auto_short_possessive_phrase_style_boundary",
        "parent_agent_key": "weak_auto_short_exact_sampler",
        "policy_name": "weak_auto_short_possessive_phrase_style_boundary_v1",
    },
    "weak_auto_short_direct_object_preposition": {
        "agent_key": "weak_auto_short_direct_object_preposition_boundary",
        "parent_agent_key": "weak_auto_short_exact_sampler",
        "policy_name": "weak_auto_short_direct_object_preposition_boundary_v1",
    },
    "weak_auto_issue_es_helper_narrative": {
        "agent_key": "weak_auto_issue_es_helper_narrative_boundary",
        "parent_agent_key": "weak_auto_issue_boundary",
        "policy_name": "weak_auto_issue_es_helper_narrative_boundary_v1",
    },
    "weak_auto_issue_hostage_context": {
        "agent_key": "weak_auto_issue_hostage_context_boundary",
        "parent_agent_key": "weak_auto_issue_boundary",
        "policy_name": "weak_auto_issue_hostage_context_boundary_v1",
    },
    "weak_auto_issue_dynamic_spanish_literal": {
        "agent_key": "weak_auto_issue_dynamic_spanish_literal_boundary",
        "parent_agent_key": "weak_auto_issue_boundary",
        "policy_name": "weak_auto_issue_dynamic_spanish_literal_boundary_v1",
    },
    "weak_auto_issue_inline_spanish_literal": {
        "agent_key": "weak_auto_issue_inline_spanish_literal_boundary",
        "parent_agent_key": "weak_auto_issue_boundary",
        "policy_name": "weak_auto_issue_inline_spanish_literal_boundary_v1",
    },
    "weak_auto_issue_credit_spanish_label": {
        "agent_key": "weak_auto_issue_credit_spanish_label_boundary",
        "parent_agent_key": "weak_auto_issue_boundary",
        "policy_name": "weak_auto_issue_credit_spanish_label_boundary_v1",
    },
    "weak_auto_issue_concept_spanish_label": {
        "agent_key": "weak_auto_issue_concept_spanish_label_boundary",
        "parent_agent_key": "weak_auto_issue_boundary",
        "policy_name": "weak_auto_issue_concept_spanish_label_boundary_v1",
    },
    "unclassified_negative_boundary": {
        "agent_key": "weak_auto_unclassified_negative_boundary",
        "parent_agent_key": "weak_auto_confirmation_reconciler",
        "policy_name": "weak_auto_unclassified_negative_boundary_v1",
    },
}

BOUNDARY_SUBFAMILIES = {
    "weak_auto_token_surface_semantic_delta",
    "weak_auto_embedded_literal_token_exact",
    "weak_auto_custom_loc_helper_token",
    "weak_auto_dynamic_spanish_literal",
    "weak_auto_residual_spanish_literal",
    "weak_auto_tiny_exact",
    "weak_auto_issue_signal",
}

SPANISH_RUNTIME_SECOND_PERSON = {
    "abandonaras",
    "abandonarás",
    "decidiste",
    "despides",
    "entregas",
    "extendiste",
    "ganas",
    "gastas",
    "pierdes",
    "posees",
    "resultas",
    "usas",
}

MOJIBAKE_HINTS = {
    "abandonarã¡s",
    "abandonarÃ¡s",
    "extendiã³",
    "extendiÃ³",
    "ã©",
    "Ã©",
}


def sha256_text(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_text(value: str | None) -> str:
    text = (value or "").casefold()
    replacements = {
        "á": "a",
        "à": "a",
        "ã": "a",
        "â": "a",
        "é": "e",
        "ê": "e",
        "í": "i",
        "ó": "o",
        "ô": "o",
        "õ": "o",
        "ú": "u",
        "ç": "c",
    }
    for before, after in replacements.items():
        text = text.replace(before, after)
    return text


def contains_word(text: str, word: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(word)}(?![\w])", text, flags=re.IGNORECASE) is not None


def has_runtime_spanish_verb(row: dict[str, Any]) -> bool:
    text = normalize_text(row.get("confirmed_text") or "")
    notes = normalize_text(row.get("notes") or "")
    return any(contains_word(text, word) or contains_word(notes, word) for word in SPANISH_RUNTIME_SECOND_PERSON)


def has_mojibake_runtime_literal(row: dict[str, Any]) -> bool:
    text = row.get("confirmed_text") or ""
    notes = row.get("notes") or ""
    return any(hint in text or hint in notes for hint in MOJIBAKE_HINTS)


def has_connector_loss(row: dict[str, Any]) -> bool:
    text = row.get("confirmed_text") or ""
    corrected = row.get("corrected_text") or ""
    notes = normalize_text(row.get("notes") or "")
    english = normalize_text(row.get("english_text") or "")
    spanish = normalize_text(row.get("spanish_text") or "")
    return (
        "ES_DelDela" in text
        or "connector" in notes
        or "possessive" in notes
        or "adventure-name" in notes
        or " of " in english
        and " de " in corrected
        or " de " in spanish
        and " de " in corrected
        and " de " not in text
    )


def has_sentence_collapse(row: dict[str, Any]) -> bool:
    text = (row.get("confirmed_text") or "").strip()
    notes = normalize_text(row.get("notes") or "")
    return text in {"!", ".", ""} or "collapsed" in notes or "dropped visible sentence" in notes


def has_semantic_sentence_loss(row: dict[str, Any]) -> bool:
    text = normalize_text(row.get("confirmed_text") or "")
    corrected = normalize_text(row.get("corrected_text") or "")
    notes = normalize_text(row.get("notes") or "")
    english = normalize_text(row.get("english_text") or "")
    spanish = normalize_text(row.get("spanish_text") or "")
    return (
        "semantic text" in notes
        or "restore pt-br sentence" in notes
        or "no longer considered" in notes
        or (
            "no longer" in english
            and ("ya no" in spanish or "no se le considera" in spanish)
            and ("nao e mais" in corrected or "não é mais" in corrected)
            and "nao" not in text
            and "não" not in text
        )
    )


def has_copula_token_form_issue(row: dict[str, Any]) -> bool:
    text = row.get("confirmed_text") or ""
    notes = normalize_text(row.get("notes") or "")
    return "GetShortUINameU" in text or "copula" in notes or "charareis" in notes


def has_short_local_player_sua_pessoa_issue(row: dict[str, Any]) -> bool:
    text = normalize_text(row.get("confirmed_text") or "")
    corrected = normalize_text(row.get("corrected_text") or "")
    notes = normalize_text(row.get("notes") or "")
    return (
        "sua pessoa" in text
        or "local-player" in notes
        and "voce" in corrected
    )


def has_short_dynamic_spanish_verb_issue(row: dict[str, Any]) -> bool:
    text = normalize_text(row.get("confirmed_text") or "")
    corrected = normalize_text(row.get("corrected_text") or "")
    notes = normalize_text(row.get("notes") or "")
    spanish_verb_hints = {
        "abandonara",
        "abandonaras",
        "ablandaste",
        "ablando",
        "aportaste",
        "aporto",
        "deseaste",
        "deseo",
        "gana",
        "ganas",
        "resulta",
        "resultas",
    }
    return (
        any(contains_word(text, hint) or contains_word(notes, hint) for hint in spanish_verb_hints)
        or "amaciou" in corrected
        or "spanish verb" in notes
        or "spanish-style verb" in notes
        or "select_cstring branches still contain spanish verbs" in notes
        or "dynamic spanish verb" in notes
        or "runtime spanish verb" in notes
    )


def has_short_geo_spanish_direction_issue(row: dict[str, Any]) -> bool:
    path = str(row.get("relative_path") or "")
    key = str(row.get("source_key") or "")
    text = normalize_text(row.get("confirmed_text") or "")
    corrected = normalize_text(row.get("corrected_text") or "")
    return (
        path == "titles_l_spanish.yml"
        and (
            "_south_" in key
            or "_north_" in key
            or "_east_" in key
            or "_west_" in key
            or contains_word(text, "sur")
            or contains_word(text, "norte")
            or contains_word(text, "este")
            or contains_word(text, "oeste")
        )
        and (
            " do sul" in corrected
            or " do norte" in corrected
            or " do leste" in corrected
            or " do oeste" in corrected
        )
    )


def has_short_english_interjection_issue(row: dict[str, Any]) -> bool:
    text = normalize_text(row.get("confirmed_text") or "")
    corrected = normalize_text(row.get("corrected_text") or "")
    notes = normalize_text(row.get("notes") or "")
    return contains_word(text, "ahem") or "english interjection" in notes or "ha..." in corrected


def has_short_semantic_label_mismatch(row: dict[str, Any]) -> bool:
    text = normalize_text(row.get("confirmed_text") or "")
    corrected = normalize_text(row.get("corrected_text") or "")
    notes = normalize_text(row.get("notes") or "")
    key = str(row.get("source_key") or "")
    return (
        "semantic" in notes
        or key == "diarch_legal_meddling_interaction"
        or "intervencao legal" in text
        and "intromissao legal" in corrected
    )


def has_short_custom_loc_article_helper(row: dict[str, Any]) -> bool:
    path = str(row.get("relative_path") or "")
    key = str(row.get("source_key") or "")
    notes = normalize_text(row.get("notes") or "")
    return (
        path == "custom_localization/es_custom_loc_l_spanish.yml"
        and key.startswith("Loc_ES_el_")
        or "es_only_el_getshortuiname" in notes
    )


def has_short_adjective_label_style(row: dict[str, Any]) -> bool:
    key = str(row.get("source_key") or "")
    notes = normalize_text(row.get("notes") or "")
    return key.endswith("_adj") and ("awkward" in notes or "adjective label" in notes)


def has_short_credit_title_style(row: dict[str, Any]) -> bool:
    path = str(row.get("relative_path") or "")
    notes = normalize_text(row.get("notes") or "")
    return path.startswith("credits/") and ("credit title" in notes or "word order" in notes)


def has_short_possessive_phrase_style(row: dict[str, Any]) -> bool:
    notes = normalize_text(row.get("notes") or "")
    return "possessive phrasing" in notes or "possessive" in notes and "awkward" in notes


def has_short_direct_object_preposition(row: dict[str, Any]) -> bool:
    notes = normalize_text(row.get("notes") or "")
    text = normalize_text(row.get("confirmed_text") or "")
    return (
        "direct object" in notes
        or "crasis/preposition" in notes
        or "alegram" in text
        and ("ao menino" in text or "a menina" in text)
    )


def has_issue_hostage_context(row: dict[str, Any]) -> bool:
    path = str(row.get("relative_path") or "")
    key = str(row.get("source_key") or "")
    text = normalize_text(row.get("confirmed_text") or "")
    notes = normalize_text(row.get("notes") or "")
    return (
        "hostage" in path
        or "hostage" in key
        or "hostage" in text
        or "refem" in text
        or "refÃ©m" in text
        or "cativo" in text
        or "cativa" in text
        or "boundary de hostage" in notes
    )


def has_issue_credit_spanish_label(row: dict[str, Any]) -> bool:
    path = str(row.get("relative_path") or "")
    notes = normalize_text(row.get("notes") or "")
    return path.startswith("credits/") or "credito" in notes or "crÃ©dito" in notes


def has_issue_concept_spanish_label(row: dict[str, Any]) -> bool:
    text = normalize_text(row.get("confirmed_text") or "")
    notes = normalize_text(row.get("notes") or "")
    return "Concept(" in (row.get("confirmed_text") or "") and (
        "tradici" in text or "concept" in notes or "label visivel" in notes
    )


def has_issue_dynamic_spanish_literal(row: dict[str, Any]) -> bool:
    text = normalize_text(row.get("confirmed_text") or "")
    notes = normalize_text(row.get("notes") or "")
    has_dynamic = "Select_CString" in (row.get("confirmed_text") or "") or "LocalPlayerString" in (row.get("confirmed_text") or "")
    spanish_markers = {
        "hÃ©roe",
        "heroe",
        "odio",
        "pasa",
        "pasas",
        "sus",
        "te",
        "ti",
        "tus",
        "vio",
        "viste",
    }
    return has_dynamic and (
        any(contains_word(text, marker) or contains_word(notes, marker) for marker in spanish_markers)
        or "select_cstring" in notes
        or "localplayerstring" in notes
        or "literal espanhol" in notes
        or "spanish" in notes
    )


def has_issue_inline_spanish_literal(row: dict[str, Any]) -> bool:
    text = normalize_text(row.get("confirmed_text") or "")
    notes = normalize_text(row.get("notes") or "")
    markers = {
        "casi",
        "demasiado",
        "eleccion",
        "elecciÃ³n",
        "fuerza",
        "gorrÃ³n",
        "gorrona",
        "plagada",
        "perfecto",
        "quiera",
    }
    return (
        any(contains_word(text, marker) or contains_word(notes, marker) for marker in markers)
        or "resÃ­duo espanhol" in notes
        or "residuo espanhol" in notes
        or "frase de alerta ainda em espanhol" in notes
    )


def has_issue_es_helper_narrative(row: dict[str, Any]) -> bool:
    text = row.get("confirmed_text") or ""
    notes = normalize_text(row.get("notes") or "")
    return ".Custom('ES_" in text and (
        "prosa" in notes
        or "narrativa" in notes
        or "gender suffix" in notes
        or "gÃªnero" in notes
        or "genero" in notes
        or "helper" in notes
        or True
    )


def classify_boundary(row: dict[str, Any]) -> tuple[str, list[str]]:
    subfamily = row.get("text_subfamily") or ""
    text = row.get("confirmed_text") or ""
    notes = normalize_text(row.get("notes") or "")
    reasons: list[str] = []

    if subfamily == "weak_auto_custom_loc_helper_token":
        if "ES_DelDela" in text or "ES_only_el_" in text or "es_deldela" in notes:
            reasons.append("es_custom_loc_runtime_helper")
            return "weak_auto_custom_loc_es_helper", reasons

    if subfamily == "weak_auto_dynamic_spanish_literal":
        if "Select_CString" in text or "LocalPlayerString" in text:
            reasons.append("dynamic_select_cstring_spanish_literal")
            return "weak_auto_dynamic_select_cstring_spanish_literal", reasons

    if subfamily == "weak_auto_residual_spanish_literal":
        reasons.append("residual_spanish_literal_reviewed_negative")
        return "weak_auto_residual_spanish_literal_repair", reasons

    if subfamily == "weak_auto_issue_signal":
        if has_issue_credit_spanish_label(row):
            reasons.append("issue_credit_spanish_label")
            return "weak_auto_issue_credit_spanish_label", reasons
        if has_issue_concept_spanish_label(row):
            reasons.append("issue_concept_spanish_label")
            return "weak_auto_issue_concept_spanish_label", reasons
        if has_issue_hostage_context(row):
            reasons.append("issue_hostage_context")
            return "weak_auto_issue_hostage_context", reasons
        if has_issue_dynamic_spanish_literal(row):
            reasons.append("issue_dynamic_spanish_literal")
            return "weak_auto_issue_dynamic_spanish_literal", reasons
        if has_issue_inline_spanish_literal(row):
            reasons.append("issue_inline_spanish_literal")
            return "weak_auto_issue_inline_spanish_literal", reasons
        if has_issue_es_helper_narrative(row):
            reasons.append("issue_es_helper_narrative")
            return "weak_auto_issue_es_helper_narrative", reasons

    if has_short_custom_loc_article_helper(row):
        reasons.append("short_custom_loc_article_helper")
        return "weak_auto_short_custom_loc_article_helper", reasons
    if has_short_dynamic_spanish_verb_issue(row):
        reasons.append("short_dynamic_spanish_verb")
        return "weak_auto_short_dynamic_spanish_verb", reasons

    if subfamily in {"", "weak_auto_tiny_exact"}:
        if has_short_custom_loc_article_helper(row):
            reasons.append("short_custom_loc_article_helper")
            return "weak_auto_short_custom_loc_article_helper", reasons
        if has_short_local_player_sua_pessoa_issue(row):
            reasons.append("short_local_player_sua_pessoa")
            return "weak_auto_short_local_player_sua_pessoa", reasons
        if has_short_dynamic_spanish_verb_issue(row):
            reasons.append("short_dynamic_spanish_verb")
            return "weak_auto_short_dynamic_spanish_verb", reasons
        if has_short_geo_spanish_direction_issue(row):
            reasons.append("short_geo_spanish_direction")
            return "weak_auto_short_geo_spanish_direction", reasons
        if has_short_english_interjection_issue(row):
            reasons.append("short_english_interjection")
            return "weak_auto_short_english_interjection", reasons
        if has_short_semantic_label_mismatch(row):
            reasons.append("short_semantic_label_mismatch")
            return "weak_auto_short_semantic_label_mismatch", reasons
        if has_short_adjective_label_style(row):
            reasons.append("short_adjective_label_style")
            return "weak_auto_short_adjective_label_style", reasons
        if has_short_credit_title_style(row):
            reasons.append("short_credit_title_style")
            return "weak_auto_short_credit_title_style", reasons
        if has_short_possessive_phrase_style(row):
            reasons.append("short_possessive_phrase_style")
            return "weak_auto_short_possessive_phrase_style", reasons
        if has_short_direct_object_preposition(row):
            reasons.append("short_direct_object_preposition")
            return "weak_auto_short_direct_object_preposition", reasons

    if subfamily == "weak_auto_embedded_literal_token_exact":
        if "Glossary(" in text:
            reasons.append("embedded_glossary_visible_label")
            return "weak_auto_embedded_glossary_visible_label", reasons
        if "Select_CString" in text and (
            has_runtime_spanish_verb(row)
            or "exhibido" in normalize_text(text)
            or "spanish" in notes
        ):
            reasons.append("embedded_select_cstring_spanish_literal")
            return "weak_auto_embedded_select_cstring_spanish_literal", reasons

    if subfamily == "weak_auto_token_surface_semantic_delta":
        if has_runtime_spanish_verb(row) or has_mojibake_runtime_literal(row):
            reasons.append("visible_runtime_spanish_verb")
            return "weak_auto_visible_runtime_spanish_verb", reasons
        if has_sentence_collapse(row):
            reasons.append("visible_sentence_collapse")
            return "weak_auto_visible_sentence_collapse", reasons
        if has_semantic_sentence_loss(row):
            reasons.append("visible_semantic_sentence_loss")
            return "weak_auto_visible_semantic_sentence_loss", reasons
        if has_copula_token_form_issue(row):
            reasons.append("visible_copula_token_form")
            return "weak_auto_visible_copula_token_form", reasons
        if has_connector_loss(row):
            reasons.append("visible_possessive_connector_loss")
            return "weak_auto_visible_possessive_connector_loss", reasons

    reasons.append("unclassified_negative_boundary")
    return "unclassified_negative_boundary", reasons


def token_status(row: dict[str, Any]) -> str:
    corrected = (row.get("corrected_text") or "").strip()
    if not corrected:
        return "no_corrected_text"
    current = row.get("confirmed_text") or ""
    if structural_tokens(current) == structural_tokens(corrected):
        return "same_structural_tokens"
    return "structural_token_change_review_required"


def evaluate_row(row: dict[str, Any]) -> dict[str, Any]:
    policy_key, reasons = classify_boundary(row)
    policy = POLICIES[policy_key]
    corrected = (row.get("corrected_text") or "").strip()
    tokens = token_status(row)
    if policy_key == "unclassified_negative_boundary":
        status = "boundary_unclassified"
        action = "manual_boundary_review"
        block_reason = "unclassified_negative_boundary"
    elif corrected and tokens == "same_structural_tokens":
        status = "repair_candidate_same_tokens"
        action = "would_queue_reviewed_repair_same_tokens"
        block_reason = ""
    elif corrected:
        status = "repair_candidate_token_change"
        action = "would_queue_reviewed_repair_token_policy_required"
        block_reason = "structural_token_change_review_required"
    else:
        status = "boundary_block_only"
        action = "would_block_auto_lifecycle_close"
        block_reason = "missing_reviewed_correction"
    return {
        **row,
        "boundary_policy": policy_key,
        "boundary_policy_name": policy["policy_name"],
        "boundary_agent_key": policy["agent_key"],
        "parent_agent_key": policy["parent_agent_key"],
        "boundary_status": status,
        "boundary_action": action,
        "token_status": tokens,
        "block_reason": block_reason,
        "boundary_reasons": reasons,
        "current_confirmed_text_hash": sha256_text(row.get("confirmed_text")),
        "corrected_text_hash": sha256_text(row.get("corrected_text")),
    }


def report_paths(settings: dict[str, Any], *, policy_key: str) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_auto_confirmation_reopen_text_boundary_policy_{policy_key}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_reviewed_negative_rows(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            decision.*,
            COALESCE(queue_item.issue_count, diag.issue_count, 0) AS issue_count,
            COALESCE(queue_item.select_cstring_count, diag.select_cstring_count, 0) AS select_cstring_count,
            COALESCE(diag.concept_link_count, 0) AS concept_link_count,
            COALESCE(queue_item.spanish_literal_hint_count, diag.spanish_literal_hint_count, 0) AS spanish_literal_hint_count,
            source.english_text,
            source.spanish_text,
            output.portuguese_text,
            confirmation.confirmed_text
        FROM auto_confirmation_reopen_text_review_decisions decision
        JOIN auto_confirmation_reopen_text_review_queue_items queue_item
          ON queue_item.id = decision.queue_item_id
        LEFT JOIN auto_confirmation_reopen_text_diagnostic_items diag
          ON diag.id = decision.diagnostic_item_id
        JOIN source_segments source ON source.id = decision.segment_id
        LEFT JOIN output_segments output ON output.segment_id = decision.segment_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.id = (
              SELECT c.id
              FROM segment_confirmations c
              WHERE c.segment_id = decision.segment_id
              ORDER BY c.updated_at DESC, c.id DESC
              LIMIT 1
          )
        WHERE decision.evidence_label = 'negative_boundary'
          AND decision.text_subfamily IN (
              'weak_auto_token_surface_semantic_delta',
              'weak_auto_embedded_literal_token_exact',
              'weak_auto_custom_loc_helper_token',
              'weak_auto_dynamic_spanish_literal',
              'weak_auto_residual_spanish_literal',
              'weak_auto_tiny_exact',
              'weak_auto_issue_signal'
          )
          AND decision.id = (
              SELECT latest.id
              FROM auto_confirmation_reopen_text_review_decisions latest
              WHERE latest.diagnostic_item_id = decision.diagnostic_item_id
              ORDER BY latest.updated_at DESC, latest.id DESC
              LIMIT 1
          )
        ORDER BY decision.text_subfamily, decision.relative_path, decision.source_line_number, decision.source_key
        """
    ).fetchall()
    return [dict(row) for row in rows]


def insert_run(
    conn,
    *,
    policy_key: str,
    rows: list[dict[str, Any]],
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    started_at: str,
    finished_at: str,
) -> int:
    counts = Counter(row["boundary_status"] for row in rows)
    token_counts = Counter(row["token_status"] for row in rows)
    cursor = conn.execute(
        """
        INSERT INTO auto_confirmation_reopen_text_boundary_policy_runs (
            rule_version,
            policy_name,
            policy_status,
            total_candidates,
            repair_candidate_count,
            block_only_count,
            same_token_count,
            token_change_count,
            unclassified_count,
            report_path,
            csv_path,
            jsonl_path,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            f"weak_auto_boundary_policy_{policy_key}_v1",
            "shadow",
            len(rows),
            counts["repair_candidate_same_tokens"] + counts["repair_candidate_token_change"],
            counts["boundary_block_only"],
            token_counts["same_structural_tokens"],
            token_counts["structural_token_change_review_required"],
            counts["boundary_unclassified"],
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            started_at,
            finished_at,
            finished_at,
        ),
    )
    return int(cursor.lastrowid)


def insert_items(conn, *, run_id: int, rows: list[dict[str, Any]], now: str) -> None:
    for row in rows:
        conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_text_boundary_policy_items (
                run_id,
                review_decision_id,
                diagnostic_run_id,
                diagnostic_item_id,
                queue_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                source_agent_key,
                source_text_subfamily,
                boundary_agent_key,
                boundary_policy,
                boundary_status,
                boundary_action,
                token_status,
                block_reason,
                issue_count,
                select_cstring_count,
                concept_link_count,
                spanish_literal_hint_count,
                current_confirmed_text_hash,
                corrected_text_hash,
                reasons_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                row["id"],
                row.get("diagnostic_run_id"),
                row.get("diagnostic_item_id"),
                row.get("queue_item_id"),
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                row["agent_key"],
                row["text_subfamily"],
                row["boundary_agent_key"],
                row["boundary_policy"],
                row["boundary_status"],
                row["boundary_action"],
                row["token_status"],
                row["block_reason"],
                int(row.get("issue_count") or 0),
                int(row.get("select_cstring_count") or 0),
                int(row.get("concept_link_count") or 0),
                int(row.get("spanish_literal_hint_count") or 0),
                row["current_confirmed_text_hash"],
                row["corrected_text_hash"],
                json.dumps(row["boundary_reasons"], ensure_ascii=False, sort_keys=True),
                now,
            ),
        )


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    policy_key: str,
    rows: list[dict[str, Any]],
    started_at: datetime,
) -> None:
    fieldnames = [
        "boundary_item_id",
        "review_decision_id",
        "diagnostic_run_id",
        "diagnostic_item_id",
        "queue_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "source_text_subfamily",
        "boundary_agent_key",
        "boundary_policy",
        "boundary_status",
        "boundary_action",
        "token_status",
        "block_reason",
        "issue_count",
        "select_cstring_count",
        "concept_link_count",
        "spanish_literal_hint_count",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                **{field: row.get(field) for field in fieldnames},
                "english_preview": short(row.get("english_text")),
                "spanish_preview": short(row.get("spanish_text")),
                "confirmed_preview": short(row.get("confirmed_text")),
                "corrected_preview": short(row.get("corrected_text")),
                "notes": row.get("notes"),
                "reasons": row["boundary_reasons"],
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    by_policy = Counter(row["boundary_policy"] for row in rows)
    by_status = Counter(row["boundary_status"] for row in rows)
    by_token = Counter(row["token_status"] for row in rows)
    lines = [
        "Auto-confirmation text boundary policy",
        f"Rule version: {RULE_VERSION}",
        f"Policy key: {policy_key}",
        "Policy status: shadow",
        f"Boundary run id: {run_id}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Rows inspected: {len(rows):,}",
        "- By boundary policy:",
        *[f"  - {key}: {value:,}" for key, value in by_policy.most_common()],
        "- By status:",
        *[f"  - {key}: {value:,}" for key, value in by_status.most_common()],
        "- By token status:",
        *[f"  - {key}: {value:,}" for key, value in by_token.most_common()],
        "",
        "Repair candidates:",
    ]
    for row in [item for item in rows if item["boundary_status"].startswith("repair_candidate")][:25]:
        lines.extend(
            [
                f"- {row['boundary_policy']} | {row['token_status']} | {row['relative_path']}:{row['source_line_number']}:{row['source_key']}",
                f"  confirmed={short(row.get('confirmed_text'))}",
                f"  corrected={short(row.get('corrected_text'))}",
            ]
        )
    if not any(item["boundary_status"].startswith("repair_candidate") for item in rows):
        lines.append("- none")
    lines.extend(["", "Block-only/unclassified sample:"])
    for row in [item for item in rows if not item["boundary_status"].startswith("repair_candidate")][:25]:
        lines.extend(
            [
                f"- {row['boundary_status']} | {row['boundary_policy']} | {row['relative_path']}:{row['source_line_number']}:{row['source_key']}",
                f"  reasons={', '.join(row['boundary_reasons'])}",
                f"  confirmed={short(row.get('confirmed_text'))}",
            ]
        )
    if all(item["boundary_status"].startswith("repair_candidate") for item in rows):
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- This is a learning/shadow boundary policy only.",
            "- It does not change confirmations, train models, promote production policy, or write output files.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, policy_key: str = DEFAULT_POLICY) -> dict[str, Any]:
    if policy_key != "all" and policy_key not in POLICIES:
        expected = ", ".join(["all", *sorted(POLICIES)])
        raise ValueError(f"Unknown policy_key={policy_key!r}. Expected one of: {expected}")
    settings = db.load_settings()
    started_at = datetime.now()
    started_at_text = started_at.isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        rows = [evaluate_row(row) for row in fetch_reviewed_negative_rows(conn)]
        if policy_key != "all":
            rows = [row for row in rows if row["boundary_policy"] == policy_key]
        txt_path, csv_path, jsonl_path = report_paths(settings, policy_key=policy_key)
        finished_at = datetime.now().isoformat(timespec="seconds")
        run_id = insert_run(
            conn,
            policy_key=policy_key,
            rows=rows,
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            started_at=started_at_text,
            finished_at=finished_at,
        )
        insert_items(conn, run_id=run_id, rows=rows, now=finished_at)
        # Load item ids for output files.
        item_ids = conn.execute(
            """
            SELECT id, review_decision_id
            FROM auto_confirmation_reopen_text_boundary_policy_items
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchall()
        by_decision = {int(row["review_decision_id"]): int(row["id"]) for row in item_ids}
        for row in rows:
            row["boundary_item_id"] = by_decision.get(int(row["id"]))
            row["review_decision_id"] = row["id"]
        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            policy_key=policy_key,
            rows=rows,
            started_at=started_at,
        )
        conn.commit()

    status_counts = Counter(row["boundary_status"] for row in rows)
    token_counts = Counter(row["token_status"] for row in rows)
    policy_counts = Counter(row["boundary_policy"] for row in rows)
    print("[auto_confirmation_reopen_text_boundary_policy] Boundary policy generated")
    print(f"[auto_confirmation_reopen_text_boundary_policy] Policy: {policy_key}")
    print(f"[auto_confirmation_reopen_text_boundary_policy] Run id: {run_id}")
    print(f"[auto_confirmation_reopen_text_boundary_policy] Rows inspected: {len(rows):,}")
    for key, value in policy_counts.most_common():
        print(f"[auto_confirmation_reopen_text_boundary_policy] {key}: {value:,}")
    for key, value in status_counts.most_common():
        print(f"[auto_confirmation_reopen_text_boundary_policy] {key}: {value:,}")
    for key, value in token_counts.most_common():
        print(f"[auto_confirmation_reopen_text_boundary_policy] {key}: {value:,}")
    print("[auto_confirmation_reopen_text_boundary_policy] Apply allowed: 0")
    print(f"[auto_confirmation_reopen_text_boundary_policy] Report: {txt_path}")
    print(f"[auto_confirmation_reopen_text_boundary_policy] CSV: {csv_path}")
    print(f"[auto_confirmation_reopen_text_boundary_policy] JSONL: {jsonl_path}")
    return {
        "run_id": run_id,
        "rows": len(rows),
        "status_counts": dict(status_counts),
        "token_counts": dict(token_counts),
        "policy_counts": dict(policy_counts),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build shadow-only boundary/repair policy from reviewed weak-auto negatives.")
    parser.add_argument("--policy", choices=["all", *sorted(POLICIES)], default=DEFAULT_POLICY)
    args = parser.parse_args()
    main(policy_key=args.policy)
