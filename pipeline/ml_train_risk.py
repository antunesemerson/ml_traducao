from __future__ import annotations

import argparse
import json
import random
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import SGDClassifier
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix, f1_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline

import db
import local_quality_validator


RULE_VERSION = "ml_train_risk_v10_calibration_boundary_features"
MODEL_KIND = "risk_action_classifier"
DEFAULT_FEATURE_SET = "legacy_v1"
STRUCTURAL_FEATURE_SET = "structural_v2"
DELTA_FEATURE_SET = "structural_v3"
LANGUAGE_FEATURE_SET = "language_v4"
FEATURE_SETS = (DEFAULT_FEATURE_SET, STRUCTURAL_FEATURE_SET, DELTA_FEATURE_SET, LANGUAGE_FEATURE_SET)
DEFAULT_TRAIN_STRATEGY = "balanced_v1"
DEDUP_WEIGHTED_TRAIN_STRATEGY = "dedup_weighted_v2"
TRAIN_STRATEGIES = (DEFAULT_TRAIN_STRATEGY, DEDUP_WEIGHTED_TRAIN_STRATEGY)
RANDOM_SEED = 42
SAFE_ACTIONS = {
    "locked_human_confirmation",
    "accepted_local",
    "accept_suggestion",
    "prefer_old",
}
RISK_ACTION_MAP = {
    "human_corrected": "needs_human",
    "needs_human": "needs_human",
    "needs_autofix": "needs_autofix",
    "reject_suggestion": "needs_human",
    "blocked_structure": "blocked_structure",
}
VISIBLE_TOKEN_PATTERN = re.compile(r"\$[^$\s]+\$|\[[^\]]+\]|#[A-Za-z0-9_]+|#!|@[A-Za-z0-9_]+!|\\n")
WORD_PATTERN = re.compile(r"[^\W\d_]+", re.UNICODE)
EXTENDED_LATIN_PATTERN = re.compile(r"[\u0100-\u024f]")
SPANISH_TITLE_RESIDUE_PATTERN = re.compile(
    r"\b(?:"
    r"Gran|Oeste|Este|Sur|Bajo|Baja|Occidental|Noreste|Sureste|Camino\s+de|Ruta\s+de|Imperio\s+de|El\s+Cairo|El\s+Pireo|Qum\s+El\s+Aoiun|"
    r"Polonia|Tánger|Egipto|Finlandia|Rusia|Abisinia|Colonia|Fezán|Nankín|Jaldun|"
    r"Basilea|Carcasona|Castellón|Beluchistán|Kurdistán|Azerbaiyán|Noreste|"
    r"Encargo|bedfordés|Galitzia|Volinia|Juzistán|Venecia|Multán|Luristán|"
    r"Almohades|norfolqués|ipuzcoano|Saboya|Bruselas|Eslavia|Italia|Normandía|"
    r"Franconia|Orense|Alejandreta|oestewalano"
    r")\b| y ",
    re.IGNORECASE,
)
SPANISH_TITLE_ADJECTIVE_PATTERN = re.compile(
    r"\b(?:[a-z]+\u00e9s|[a-z]+\u00ed|[a-z]+\u00f3nico|"
    r"alejandrino|bajolotaringio|capadocio|franconio(?:\s+(?:oriental|occidental))?|pechenego)\b",
    re.IGNORECASE,
)
SPANISH_CULTURAL_TITLE_ADJECTIVE_PATTERN = re.compile(
    r"\b(?:[a-z]+e\u00f1[oa]s?|[a-z]+\u00e9s(?:es|a|as)?|sur[a-z]*|[a-z]+\s+sur|(?:oeste|bajo|baja)[a-z]+)\b",
    re.IGNORECASE,
)
TITLE_LOCALIZATION_PATHS = {"titles_l_spanish.yml", "titles_cultural_names_l_spanish.yml"}
TITLE_KEY_PREFIXES = ("b_", "c_", "d_", "k_", "e_")
TITLE_ALLOWED_LOWERCASE_CONNECTORS = {"a", "as", "da", "das", "de", "do", "dos", "e"}
SAFE_FORMATTED_PT_WORDS = {
    "alto",
    "baixa",
    "baixo",
    "igual",
    "invertida",
    "lenta",
    "n\u00e3o",
    "nao",
    "r\u00e1pida",
    "total",
}
SPANISH_FORMATTED_WORDS = {"baja", "bajo", "media", "medio", "muy"}
BOLD_NO_TAG_PATTERN = re.compile(r"#bold\s+no#!", re.IGNORECASE)
BOLD_NAO_TAG_PATTERN = re.compile(r"#bold\s+n(?:\u00e3|a)o#!", re.IGNORECASE)
OCCIDENTAL_WORD_PATTERN = re.compile(r"\boccidental\b", re.IGNORECASE)
OCIDENTAL_WORD_PATTERN = re.compile(r"\bocidental\b", re.IGNORECASE)
KNOWN_TITLE_ADJECTIVE_UNSAFE_TEXT_BY_KEY = {
    ("titles_l_spanish.yml", "c_karabaigal_adj"): {"karabaigalio"},
    ("titles_l_spanish.yml", "c_constanta_adj"): {"constantiano"},
    ("titles_l_spanish.yml", "c_skopje_adj"): {"eskopiense"},
    ("titles_l_spanish.yml", "c_trencin_adj"): {"trencíniano"},
    ("titles_l_spanish.yml", "d_qyzylsaryarka_adj"): {"quizylsaryarkano"},
    ("titles_l_spanish.yml", "k_outer_ajuraan_adj"): {"ajuraani exeterior"},
    ("titles_l_spanish.yml", "c_saintois_adj"): {"saintois\u00e9s"},
    ("titles_cultural_names_l_spanish.yml", "cn_liptovsky_hradok_adj"): {"liptovske\u00f1o"},
    ("titles_cultural_names_l_spanish.yml", "cn_bartenstein_adj"): {"bartenstein\u00e9s"},
    ("titles_cultural_names_l_spanish.yml", "cn_southscythia_adj"): {"surescita"},
    ("titles_cultural_names_l_spanish.yml", "cn_south_yongwon_adj"): {"yongwon sur"},
}
KNOWN_TITLE_NAME_UNSAFE_TEXT_BY_KEY = {
    ("titles_l_spanish.yml", "b_ourense"): {"Orense"},
}
KNOWN_CUSTOM_LOC_UNSAFE_TEXT_BY_KEY = {
    ("custom_localization/es_custom_loc_l_spanish.yml", "Loc_ES_matrimonio_1"): {"[CHARACTER.GetUIName] se"},
}
KNOWN_CULTURE_NAME_UNSAFE_TEXT_BY_KEY = {
    ("culture/culture_creation_names_l_spanish.yml", "samhan_hybrid_collective_noun"): {"samhanes"},
}
ENGLISH_UI_WORDS = {
    "battlefield",
    "claim",
    "conversion",
    "default",
    "equality",
    "faith",
    "faster",
    "gender",
    "invasion",
    "kingdom",
    "lenient",
    "lifestyle",
    "monthly",
    "multiplayer",
    "realm",
    "slower",
    "speed",
    "throne",
    "war",
}
LATIN_HINT_WORDS = {"aeternum", "alius", "cras", "dies", "dominus", "est", "gloria", "lux", "mors", "semper"}
LANGUAGE_BLOCKING_FEATURES = {
    "RISK_CORE_PRONOUN_CONTEXT",
    "RISK_CORE_CONTEXTUAL_TITLE_TRANSLATION",
    "RISK_CORE_SKILL_INTENSITY",
    "RISK_CORE_UI_CAPITALIZATION_STYLE",
    "RISK_CUSTOM_LOC_KNOWN_UNSAFE_FRAGMENT",
    "RISK_CULTURE_NAME_KNOWN_UNSAFE_TEXT",
    "RISK_CULTURE_TITLE_CAPITALIZATION_STYLE",
    "RISK_DEBUG_PROGRESS_REMAINING_DAYS",
    "RISK_DEBUG_MID_SENTENCE_CAPITALIZATION",
    "RISK_EVENT_INCOMPLETE_WHEN_CLAUSE",
    "RISK_EVENT_LITERAL_FOREVER_STYLE",
    "RISK_EXACT_SPANISH_VISIBLE",
    "RISK_ACCENT_SENSITIVE_SPANISH_RESIDUE",
    "RISK_CONCEPT_CAPITALIZATION_STYLE",
    "RISK_IMPORTANT_ACTION_CAPITALIZATION_STYLE",
    "RISK_ADVENTURER_WARD_TRANSLATION",
    "RISK_EXACT_ENGLISH_VISIBLE",
    "RISK_EXTENDED_LATIN_NAME_EDIT",
    "RISK_FAITH_CONVERSION_PREPOSITION_STYLE",
    "RISK_MOJIBAKE_OR_UNEXPECTED_SCRIPT",
    "RISK_NAME_CAPITALIZATION_LOSS",
    "RISK_RELIGION_ENGLISH_RESIDUE",
    "RISK_RELIGION_HEALTH_GOD_SPACING",
    "RISK_RELIGION_KINSLAYING_STATUS_STYLE",
    "RISK_RELIGION_OLD_FAITH_STYLE",
    "RISK_RELIGION_POSSESSIVE_MISSING_PREPOSITION",
    "RISK_RELIGION_SAINT_POSSESSIVE_STYLE",
    "RISK_RELIGION_TEACHINGS_STYLE",
    "RISK_RELIGION_TO_NAME_TRANSLATED",
    "RISK_REVIEWED_SHORT_SOURCE_CONFLICT",
    "RISK_REDUNDANT_SELECT_CSTRING_OPTIONS",
    "RISK_SAHEL_ARTICLE_TRANSLATION",
    "RISK_SPANISH_LOCALIZATION_HELPER",
    "RISK_SPANISH_FORMATTED_LITERAL",
    "RISK_STRUCTURAL_LEADING_SPACE_LOSS",
    "RISK_TITLE_ADJECTIVE_SPANISH_FORM",
    "RISK_TITLE_ADJECTIVE_KNOWN_UNSAFE_TEXT",
    "RISK_TITLE_ADJECTIVE_CAPITALIZATION_STYLE",
    "RISK_TITLE_NAME_CAPITALIZATION_STYLE",
    "RISK_TITLE_NAME_KNOWN_UNSAFE_TEXT",
    "RISK_TITLE_PREFIX_CAPITALIZATION_STYLE",
    "RISK_TITLE_SPANISH_GEOGRAPHIC_TERMS",
    "RISK_STANDALONE_DOUBLE_DOT",
    "RISK_STRUCTURAL_EMPTY_VISIBLE_TEXT",
    "RISK_TRAIT_GENERIC_CHARACTER_GENDER_STYLE",
}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def percent(value: float | None) -> str:
    if value is None:
        return "0.00%"
    return f"{value:.2%}"


def latest_dataset_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_dataset_runs
        WHERE total_count > 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No ml_dataset_runs found. Run python pipeline/main.py ml-dataset first.")
    return int(row["id"])


def risk_label(
    action_label: str,
    issue_label: str | None = None,
    candidate_text: str | None = None,
    final_text: str | None = None,
    dataset_label: str | None = None,
) -> str:
    if issue_label == "contextual_exception":
        return "auto_safe"
    if (
        dataset_label == "positive"
        and action_label == "human_corrected"
        and issue_label == "minor_fix"
        and final_text is not None
        and (candidate_text or "") == (final_text or "")
    ):
        return "auto_safe"
    if action_label == "human_corrected" and issue_label == "minor_fix":
        return "needs_autofix"
    if action_label in SAFE_ACTIONS:
        return "auto_safe"
    return RISK_ACTION_MAP.get(action_label, "needs_human")


def bucket_number(name: str, value: int | float | None) -> str:
    if value is None:
        return f"{name}:unknown"
    number = float(value)
    if number <= 0:
        return f"{name}:0"
    if number <= 2:
        return f"{name}:1_2"
    if number <= 5:
        return f"{name}:3_5"
    if number <= 15:
        return f"{name}:6_15"
    if number <= 50:
        return f"{name}:16_50"
    return f"{name}:51_plus"


def path_group(relative_path: str | None) -> str:
    path = (relative_path or "").lower()
    if path == "core_l_spanish.yml":
        return "core"
    if path == "game_concepts_l_spanish.yml":
        return "game_concepts"
    if path == "titles_l_spanish.yml":
        return "titles"
    if path == "relationship_reasons_l_spanish.yml":
        return "relationship_reasons"
    if path == "court_positions_l_spanish.yml":
        return "court_positions"
    if path == "council_tasks_l_spanish.yml":
        return "council_tasks"
    if path == "important_actions_l_spanish.yml":
        return "important_actions"
    if path.startswith("custom_localization/"):
        return "custom_localization"
    if path.startswith("dlc/"):
        return "dlc"
    if path.startswith("activities/"):
        return "activities"
    if path.startswith("contracts/"):
        return "contracts"
    if path.startswith("events/") or path.endswith("_events_l_spanish.yml"):
        return "events"
    if path.startswith("gui/"):
        return "gui"
    if path.startswith("religion/"):
        return "religion"
    if path.startswith("names/"):
        return "names"
    if path.startswith("dynasties/"):
        return "dynasties"
    if path.startswith("mottos"):
        return "mottos"
    if path.startswith("game_rules"):
        return "game_rules"
    if path.startswith("wars"):
        return "wars"
    if path.startswith("schemes"):
        return "schemes"
    if path.startswith("triggers/"):
        return "triggers"
    return "other"


def key_shape(source_key: str | None) -> str:
    key = source_key or ""
    if key.startswith("game_concept_"):
        return "game_concept"
    if key.startswith("CHARACTER_"):
        return "character_macro"
    if key.startswith("COURT_POSITIONS_"):
        return "court_positions_ui"
    if key.startswith("APPOINT_COURT_POSITION_"):
        return "appoint_court_position_ui"
    if key.startswith(("b_", "c_", "d_", "k_", "e_")):
        if key.endswith("_adj"):
            return "title_adjective"
        return "title_name"
    if key.isupper():
        return "uppercase_ui"
    if key.islower():
        return "lowercase_key"
    if "_" in key:
        return "mixed_underscore"
    return "other"


def text_shape(name: str, value: str | None) -> str:
    text = value or ""
    stripped = text.strip()
    flags = []
    if not stripped:
        flags.append("empty")
    if stripped == text and text:
        flags.append("no_outer_space")
    if stripped != text:
        flags.append("outer_space")
    if text[:1].isupper():
        flags.append("starts_upper")
    if text[:1].islower():
        flags.append("starts_lower")
    if text.isupper() and text:
        flags.append("all_upper")
    if "[" in text or "]" in text:
        flags.append("has_bracket_token")
    if "$" in text:
        flags.append("has_dollar_token")
    if "#!" in text or "#" in text:
        flags.append("has_hash_format")
    if "@" in text:
        flags.append("has_icon")
    if "\\n" in text:
        flags.append("has_newline_escape")
    if len(stripped.split()) <= 2:
        flags.append("short")
    elif len(stripped.split()) >= 12:
        flags.append("long")
    else:
        flags.append("medium")
    return f"{name}:" + ",".join(flags)


def normalize_compare(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def candidate_differs_from_output(row: dict[str, Any] | None) -> bool:
    if row is None:
        return False
    output = row.get("output_text")
    if output in {None, ""}:
        return False
    return normalize_compare(row.get("candidate_text")) != normalize_compare(output)


def token_set(value: str | None) -> set[str]:
    text = value or ""
    return set(re.findall(r"\$[^$\s]+\$|\[[^\]]+\]|@[A-Za-z0-9_]+!|#[A-Za-z0-9_]+|#!", text))


def visible_text(value: str | None) -> str:
    return normalize_compare(VISIBLE_TOKEN_PATTERN.sub(" ", value or ""))


def visible_display_text(value: str | None) -> str:
    return " ".join(VISIBLE_TOKEN_PATTERN.sub(" ", value or "").split())


def visible_words(value: str | None) -> set[str]:
    return {word.lower() for word in WORD_PATTERN.findall(visible_text(value))}


def formatted_words(value: str | None) -> set[str]:
    return {
        match.group(1).lower()
        for match in re.finditer(r"#[A-Za-z0-9_]+\s+([^\W\d_]+)#!", value or "")
    }


def has_bold_no_to_nao_microrepair(row: dict[str, Any] | None) -> bool:
    if row is None:
        return False
    candidate = row.get("candidate_text")
    if not BOLD_NAO_TAG_PATTERN.search(candidate or ""):
        return False
    references = (
        row.get("output_text"),
        row.get("old_text"),
        row.get("spanish_text"),
        row.get("english_text"),
    )
    bold_no_reference = next((value for value in references if BOLD_NO_TAG_PATTERN.search(value or "")), None)
    if not bold_no_reference:
        return False
    return token_set(candidate) == token_set(bold_no_reference)


def has_occidental_to_ocidental_title_repair(row: dict[str, Any] | None) -> bool:
    if row is None:
        return False
    relative_path = row.get("relative_path") or ""
    source_key = row.get("source_key") or ""
    if relative_path not in TITLE_LOCALIZATION_PATHS or not source_key.endswith("_adj"):
        return False
    candidate = row.get("candidate_text")
    if not OCIDENTAL_WORD_PATTERN.search(candidate or ""):
        return False
    references = (
        row.get("output_text"),
        row.get("old_text"),
        row.get("spanish_text"),
        row.get("english_text"),
    )
    occidental_reference = next((value for value in references if OCCIDENTAL_WORD_PATTERN.search(value or "")), None)
    if not occidental_reference:
        return False
    return token_set(candidate) == token_set(occidental_reference)


def has_confirmed_title_adjective_lexical_repair(row: dict[str, Any] | None) -> bool:
    if row is None:
        return False
    relative_path = row.get("relative_path") or ""
    source_key = row.get("source_key") or ""
    if relative_path not in TITLE_LOCALIZATION_PATHS or not source_key.endswith("_adj"):
        return False
    if row.get("confirmation_source") not in {
        "title_landed_adjective_lexical_residue_repair_production",
        "title_landed_adjective_lexical_residue_medium_repair_production",
    }:
        return False
    if row.get("confirmation_label") not in {
        "title_landed_adjective_lexical_residue_exact_v1",
        "title_landed_adjective_lexical_residue_direction_suffix_v1",
    }:
        return False
    candidate = (row.get("candidate_text") or "").strip()
    old = (row.get("old_text") or "").strip()
    return bool(candidate and old and candidate != old and token_set(candidate) == token_set(old))


def has_title_adjective_capitalization_risk(relative_path: str, source_key: str, candidate: str | None) -> bool:
    if relative_path not in TITLE_LOCALIZATION_PATHS:
        return False
    if not source_key.endswith("_adj"):
        return False
    display = visible_display_text(candidate)
    return bool(display and display[:1].isupper())


def has_title_name_capitalization_risk(relative_path: str, source_key: str, candidate: str | None) -> bool:
    if relative_path != "titles_l_spanish.yml":
        return False
    if not source_key.startswith(TITLE_KEY_PREFIXES) or source_key.endswith("_adj"):
        return False
    words = visible_display_text(candidate).split()
    if len(words) < 2:
        return False
    for word in words[1:]:
        stripped = re.sub(r"^[^\w]+|[^\w]+$", "", word, flags=re.UNICODE)
        if not stripped or not stripped[:1].islower():
            continue
        if stripped.casefold() in TITLE_ALLOWED_LOWERCASE_CONNECTORS:
            continue
        return True
    return False


def has_culture_title_capitalization_risk(row: dict[str, Any]) -> bool:
    if (row.get("relative_path") or "") != "culture/culture_titles_l_spanish.yml":
        return False
    candidate_display = visible_display_text(row.get("candidate_text"))
    if not candidate_display:
        return False
    reference_display = (
        visible_display_text(row.get("output_text"))
        or visible_display_text(row.get("old_text"))
        or visible_display_text(row.get("english_text"))
    )
    if not reference_display or candidate_display == reference_display:
        return False
    if normalize_compare(candidate_display) != normalize_compare(reference_display):
        return False
    candidate_words = candidate_display.split()
    reference_words = reference_display.split()
    if len(candidate_words) != len(reference_words):
        return False
    for candidate_word, reference_word in zip(candidate_words, reference_words):
        candidate_core = re.sub(r"^[^\w]+|[^\w]+$", "", candidate_word, flags=re.UNICODE)
        reference_core = re.sub(r"^[^\w]+|[^\w]+$", "", reference_word, flags=re.UNICODE)
        if not candidate_core or not reference_core:
            continue
        if candidate_core.casefold() != reference_core.casefold():
            continue
        if reference_core[:1].isupper() and candidate_core[:1].islower():
            return True
    return False


def has_reviewed_short_source_conflict(row: dict[str, Any]) -> bool:
    if row.get("evidence_source") != "local_learning_candidate":
        return False
    if row.get("issue_label") != "semantic_error":
        return False
    candidate_visible = visible_text(row.get("candidate_text"))
    english_visible = visible_text(row.get("english_text"))
    spanish_visible = visible_text(row.get("spanish_text"))
    old_visible = visible_text(row.get("old_text"))
    output_visible = visible_text(row.get("output_text"))
    if not candidate_visible or not english_visible or not spanish_visible:
        return False
    if english_visible == spanish_visible:
        return False
    if len(candidate_visible.split()) > 3:
        return False
    if len(english_visible.split()) > 3 or len(spanish_visible.split()) > 3:
        return False
    return candidate_visible in {old_visible, output_visible}


def is_exact_shared_glossary_token(row: dict[str, Any]) -> bool:
    return local_quality_validator.is_exact_shared_glossary_token(row)


def language_features(row: dict[str, Any]) -> list[str]:
    relative_path = row.get("relative_path") or ""
    source_key = row.get("source_key") or ""
    candidate = row.get("candidate_text")
    english = row.get("english_text")
    spanish = row.get("spanish_text")
    candidate_visible = visible_text(candidate)
    english_visible = visible_text(english)
    spanish_visible = visible_text(spanish)
    candidate_display = visible_display_text(candidate)
    english_display = visible_display_text(english)
    candidate_words = visible_words(candidate)
    formatted = formatted_words(candidate)
    group = path_group(relative_path)
    has_es_custom = "Custom('ES_" in (candidate or "") or 'Custom("ES_' in (candidate or "")
    features = [
        f"VISIBLE_EQUALS_ENGLISH={candidate_visible == english_visible}",
        f"VISIBLE_EQUALS_SPANISH={candidate_visible == spanish_visible}",
        f"VISIBLE_EQUALS_BOTH_SOURCES={candidate_visible == english_visible == spanish_visible}",
        f"VISIBLE_EMPTY={not candidate_visible}",
        f"IS_NAME_OR_DYNASTY={group in {'names', 'dynasties'}}",
        f"IS_MOTTO={group == 'mottos'}",
        f"IS_GAME_RULE={group == 'game_rules'}",
        f"HAS_LOC_ES={'Loc_ES_' in (candidate or '')}",
        f"HAS_ES_CUSTOM={has_es_custom}",
        f"HAS_SELECT_CSTRING={'Select_CString' in (candidate or '')}",
        f"HAS_LOCAL_PLAYER_STRING={'LocalPlayerString' in (candidate or '')}",
        f"FORMATTED_SPANISH_WORDS={bool(formatted & SPANISH_FORMATTED_WORDS)}",
        f"FORMATTED_SAFE_PT_WORDS={bool(formatted & SAFE_FORMATTED_PT_WORDS)}",
        f"ENGLISH_UI_WORD_HITS={bool(candidate_words & ENGLISH_UI_WORDS)}",
        f"LATIN_HINT_HITS={bool(candidate_words & LATIN_HINT_WORDS)}",
    ]
    if candidate_visible == english_visible == spanish_visible and group in {"names", "dynasties", "mottos"}:
        features.append("SAFE_SHARED_NAME_OR_MOTTO")
    if is_exact_shared_glossary_token(row):
        features.append("SAFE_EXACT_SHARED_GLOSSARY_TOKEN")
    if has_bold_no_to_nao_microrepair(row):
        features.append("SAFE_BOLD_NO_TO_NAO_MICROREPAIR")
    if has_confirmed_title_adjective_lexical_repair(row):
        features.append("SAFE_CONFIRMED_TITLE_ADJECTIVE_LEXICAL_REPAIR")
    if has_occidental_to_ocidental_title_repair(row):
        features.append("SAFE_OCCIDENTAL_TO_OCIDENTAL_TITLE_REPAIR")
    if formatted & SAFE_FORMATTED_PT_WORDS and not (formatted & SPANISH_FORMATTED_WORDS):
        features.append("SAFE_FORMATTED_PORTUGUESE_LITERAL")
    if candidate_visible == english_visible and candidate_visible != spanish_visible and group not in {"names", "dynasties"}:
        features.append("RISK_EXACT_ENGLISH_VISIBLE")
    if (
        candidate_visible
        and candidate_visible == spanish_visible
        and candidate_visible != english_visible
        and group not in {"names", "dynasties", "titles"}
    ):
        features.append("RISK_EXACT_SPANISH_VISIBLE")
    if source_key.endswith("_WITH_SPACE") and (spanish or "").startswith(" ") and not (candidate or "").startswith(" "):
        features.append("RISK_STRUCTURAL_LEADING_SPACE_LOSS")
    if (
        source_key.endswith("_name")
        and candidate_display
        and english_display
        and candidate_display.casefold() == english_display.casefold()
        and candidate_display[:1].islower()
        and english_display[:1].isupper()
    ):
        features.append("RISK_NAME_CAPITALIZATION_LOSS")
    if not candidate_visible and bool((candidate or "").strip()):
        features.append("RISK_STRUCTURAL_EMPTY_VISIBLE_TEXT")
    if re.search(r"(?<!\.)\.\.(?!\.)", candidate or ""):
        features.append("RISK_STANDALONE_DOUBLE_DOT")
    if (
        relative_path == "core_l_spanish.yml"
        and source_key.startswith("CHARACTER_")
        and candidate_visible in {"dele", "dela", "deles", "delas", "disso"}
    ):
        features.append("RISK_CORE_PRONOUN_CONTEXT")
    if relative_path == "core_l_spanish.yml" and source_key == "CHARACTER_LORD" and candidate_visible == "lorde":
        features.append("RISK_CORE_CONTEXTUAL_TITLE_TRANSLATION")
    if (
        relative_path == "game_concepts_l_spanish.yml"
        and source_key.startswith("game_concept_")
        and any(word[:1].isupper() for word in candidate_display.split()[1:])
    ):
        features.append("RISK_CONCEPT_CAPITALIZATION_STYLE")
    if (
        relative_path == "core_l_spanish.yml"
        and source_key == "SKILL_NICE"
        and candidate_visible in {"ótimo", "otimo"}
    ):
        features.append("RISK_CORE_SKILL_INTENSITY")
    if relative_path == "core_l_spanish.yml" and any(word[:1].isupper() for word in candidate_display.split()[1:]):
        features.append("RISK_CORE_UI_CAPITALIZATION_STYLE")
    if relative_path == "debug_l_spanish.yml" and re.search(
        r"\b(?:de|da|do|das|dos|em|para|por)\s+[A-ZÁÉÍÓÚÂÊÔÃÕÇ][^\s.!?:;,]*",
        candidate or "",
    ):
        features.append("RISK_DEBUG_MID_SENTENCE_CAPITALIZATION")
    if relative_path == "debug_l_spanish.yml" and re.search(r"\bfaltar\s+1\s+dia\b", candidate or "", re.IGNORECASE):
        features.append("RISK_DEBUG_PROGRESS_REMAINING_DAYS")
    if group == "events" and re.search(r"\bquando\s+não\s+estavam\.$", candidate or "", re.IGNORECASE):
        features.append("RISK_EVENT_INCOMPLETE_WHEN_CLAUSE")
    if group == "events" and re.search(r"\b(?:durar|dura)\s+para\s+sempre\b", candidate or "", re.IGNORECASE):
        features.append("RISK_EVENT_LITERAL_FOREVER_STYLE")
    if (
        relative_path == "important_actions_l_spanish.yml"
        and any(word[:1].isupper() for word in candidate_display.split()[1:])
    ):
        features.append("RISK_IMPORTANT_ACTION_CAPITALIZATION_STYLE")
    if has_reviewed_short_source_conflict(row):
        features.append("RISK_REVIEWED_SHORT_SOURCE_CONFLICT")
    if (
        relative_path == "adventurer_name_sections_l_spanish.yml"
        and source_key == "ward_adventurer"
        and candidate_visible == "ala"
    ):
        features.append("RISK_ADVENTURER_WARD_TRANSLATION")
    if (
        relative_path == "traits_l_spanish.yml"
        and source_key.startswith("trait_")
        and source_key.endswith("_desc")
        and candidate_visible.startswith("esta personagem")
    ):
        features.append("RISK_TRAIT_GENERIC_CHARACTER_GENDER_STYLE")
    if (
        local_quality_validator.CYRILLIC_OR_MOJIBAKE_PATTERN.search(candidate or "")
        or local_quality_validator.MOJIBAKE_SEQUENCE_PATTERN.search(candidate or "")
        or local_quality_validator.QUESTION_MARK_MOJIBAKE_PATTERN.search(candidate or "")
    ):
        features.append("RISK_MOJIBAKE_OR_UNEXPECTED_SCRIPT")
    if local_quality_validator.count_accent_sensitive_spanish_residue(candidate)[0]:
        features.append("RISK_ACCENT_SENSITIVE_SPANISH_RESIDUE")
    if local_quality_validator.redundant_select_cstring_literals(candidate):
        features.append("RISK_REDUNDANT_SELECT_CSTRING_OPTIONS")
    if (
        EXTENDED_LATIN_PATTERN.search(candidate or "")
        and candidate_visible not in {english_visible, spanish_visible}
    ):
        features.append("RISK_EXTENDED_LATIN_NAME_EDIT")
    if re.search(r"\bde\s+Sahel\b", candidate or ""):
        features.append("RISK_SAHEL_ARTICLE_TRANSLATION")
    if (
        relative_path == "religion/religion_l_spanish.yml"
        and source_key.startswith("faith_conversion_cost_")
        and re.search(r"\b(?:à\s+uma\s+fé|não-reformad[ao]s?)\b", candidate or "", re.IGNORECASE)
    ):
        features.append("RISK_FAITH_CONVERSION_PREPOSITION_STYLE")
    if group == "religion" and re.search(r"\bAdherents\b", candidate or ""):
        features.append("RISK_RELIGION_ENGLISH_RESIDUE")
    if group == "religion" and re.search(r"\bde\s+Santos\s+[A-ZÁÉÍÓÚÂÊÔÃÕÇ]", candidate or ""):
        features.append("RISK_RELIGION_SAINT_POSSESSIVE_STYLE")
    if group == "religion" and re.search(r"\bVelh[oa]s?\s+", candidate or ""):
        features.append("RISK_RELIGION_OLD_FAITH_STYLE")
    if group == "religion" and re.search(r"\bEnsinos\s+de\b", candidate or ""):
        features.append("RISK_RELIGION_TEACHINGS_STYLE")
    if group == "religion" and re.search(r"\bdeusda\s+saúde\b", candidate or "", re.IGNORECASE):
        features.append("RISK_RELIGION_HEALTH_GOD_SPACING")
    if group == "religion" and source_key.endswith("_possessive") and re.search(r"^O\s+Grande\s+Construtor$", candidate or ""):
        features.append("RISK_RELIGION_POSSESSIVE_MISSING_PREPOSITION")
    if group == "religion" and source_key.endswith("_crime_name") and candidate_visible == "crime":
        features.append("RISK_RELIGION_KINSLAYING_STATUS_STYLE")
    if group == "religion" and source_key.startswith("doctrine_kinslaying_") and re.search(r"\bsão\s+Criminosos\b", candidate or ""):
        features.append("RISK_RELIGION_KINSLAYING_STATUS_STYLE")
    if group == "religion" and source_key.endswith("_high_god_name_alternate") and re.search(r"^Para\s+[A-Z]", candidate or ""):
        features.append("RISK_RELIGION_TO_NAME_TRANSLATED")
    if relative_path == "titles_l_spanish.yml" and SPANISH_TITLE_RESIDUE_PATTERN.search(candidate or ""):
        features.append("RISK_TITLE_SPANISH_GEOGRAPHIC_TERMS")
    if (
        relative_path in TITLE_LOCALIZATION_PATHS
        and source_key.endswith("_adj")
        and (
            SPANISH_TITLE_ADJECTIVE_PATTERN.search(candidate or "")
            or SPANISH_CULTURAL_TITLE_ADJECTIVE_PATTERN.search(candidate or "")
        )
    ):
        features.append("RISK_TITLE_ADJECTIVE_SPANISH_FORM")
    if has_title_adjective_capitalization_risk(relative_path, source_key, candidate):
        features.append("RISK_TITLE_ADJECTIVE_CAPITALIZATION_STYLE")
    blocked_title_adjectives = KNOWN_TITLE_ADJECTIVE_UNSAFE_TEXT_BY_KEY.get((relative_path, source_key), set())
    if source_key.endswith("_adj") and normalize_compare(candidate) in {
        normalize_compare(value) for value in blocked_title_adjectives
    }:
        features.append("RISK_TITLE_ADJECTIVE_KNOWN_UNSAFE_TEXT")
    if has_title_name_capitalization_risk(relative_path, source_key, candidate):
        features.append("RISK_TITLE_NAME_CAPITALIZATION_STYLE")
    blocked_title_names = KNOWN_TITLE_NAME_UNSAFE_TEXT_BY_KEY.get((relative_path, source_key), set())
    if normalize_compare(candidate) in {normalize_compare(value) for value in blocked_title_names}:
        features.append("RISK_TITLE_NAME_KNOWN_UNSAFE_TEXT")
    blocked_custom_loc = KNOWN_CUSTOM_LOC_UNSAFE_TEXT_BY_KEY.get((relative_path, source_key), set())
    if normalize_compare(candidate) in {normalize_compare(value) for value in blocked_custom_loc}:
        features.append("RISK_CUSTOM_LOC_KNOWN_UNSAFE_FRAGMENT")
    blocked_culture_names = KNOWN_CULTURE_NAME_UNSAFE_TEXT_BY_KEY.get((relative_path, source_key), set())
    if normalize_compare(candidate) in {normalize_compare(value) for value in blocked_culture_names}:
        features.append("RISK_CULTURE_NAME_KNOWN_UNSAFE_TEXT")
    if has_culture_title_capitalization_risk(row):
        features.append("RISK_CULTURE_TITLE_CAPITALIZATION_STYLE")
    if (
        relative_path == "titles_l_spanish.yml"
        and source_key.endswith("_pre")
        and candidate_display[:1].isupper()
    ):
        features.append("RISK_TITLE_PREFIX_CAPITALIZATION_STYLE")
    if "Loc_ES_" in (candidate or "") or "Custom('ES_" in (candidate or ""):
        features.append("RISK_SPANISH_LOCALIZATION_HELPER")
    if formatted & SPANISH_FORMATTED_WORDS:
        features.append("RISK_SPANISH_FORMATTED_LITERAL")
    return features


def overlap_bucket(name: str, left: str | None, right: str | None) -> str:
    left_words = set(WORD.lower() for WORD in WORD_PATTERN.findall(left or ""))
    right_words = set(WORD.lower() for WORD in WORD_PATTERN.findall(right or ""))
    if not left_words and not right_words:
        return f"{name}:both_empty"
    if not left_words or not right_words:
        return f"{name}:one_empty"
    overlap = len(left_words & right_words) / max(len(left_words | right_words), 1)
    if overlap == 1:
        bucket = "same"
    elif overlap >= 0.75:
        bucket = "high"
    elif overlap >= 0.35:
        bucket = "medium"
    elif overlap > 0:
        bucket = "low"
    else:
        bucket = "none"
    return f"{name}:{bucket}"


def comparison_features(row: dict[str, Any]) -> list[str]:
    candidate = row.get("candidate_text")
    output = row.get("output_text")
    old = row.get("old_text")
    english = row.get("english_text")
    spanish = row.get("spanish_text")
    candidate_norm = normalize_compare(candidate)
    output_norm = normalize_compare(output)
    old_norm = normalize_compare(old)
    english_norm = normalize_compare(english)
    spanish_norm = normalize_compare(spanish)
    candidate_tokens = token_set(candidate)
    output_tokens = token_set(output)
    old_tokens = token_set(old)
    features = [
        f"CANDIDATE_EQUALS_OUTPUT={candidate_norm == output_norm}",
        f"CANDIDATE_EQUALS_OLD={candidate_norm == old_norm}",
        f"CANDIDATE_EQUALS_ENGLISH={candidate_norm == english_norm}",
        f"CANDIDATE_EQUALS_SPANISH={candidate_norm == spanish_norm}",
        f"OUTPUT_EQUALS_OLD={output_norm == old_norm}",
        f"CANDIDATE_TOKEN_EQUALS_OUTPUT={candidate_tokens == output_tokens}",
        f"CANDIDATE_TOKEN_EQUALS_OLD={candidate_tokens == old_tokens}",
        bucket_number("CANDIDATE_OUTPUT_LEN_DELTA", abs(len(candidate or "") - len(output or ""))),
        bucket_number("CANDIDATE_OLD_LEN_DELTA", abs(len(candidate or "") - len(old or ""))),
        overlap_bucket("CANDIDATE_OUTPUT_WORD_OVERLAP", candidate, output),
        overlap_bucket("CANDIDATE_OLD_WORD_OVERLAP", candidate, old),
        overlap_bucket("CANDIDATE_ENGLISH_WORD_OVERLAP", candidate, english),
        overlap_bucket("CANDIDATE_SPANISH_WORD_OVERLAP", candidate, spanish),
    ]
    if candidate_norm != output_norm and output_norm == old_norm:
        features.append("CANDIDATE_DIVERGES_FROM_CONFIRMED_OUTPUT")
    if candidate_norm == output_norm and output_norm == old_norm:
        features.append("CANDIDATE_MATCHES_CONFIRMED_OUTPUT")
    if is_exact_shared_glossary_token(row):
        features.append("SAFE_EXACT_SHARED_GLOSSARY_TOKEN")
    return features


def make_text_legacy(row: dict[str, Any]) -> str:
    fields = [
        f"PATH={row['relative_path']}",
        f"KEY={row['source_key']}",
        f"LOCKED={row['locked']}",
        f"HAS_ENGLISH={row['has_english']}",
        f"HAS_OLD={row['has_old']}",
        bucket_number("TOKENS", row["token_count"]),
        bucket_number("TEXTLEN", row["text_length"]),
        f"ENGLISH: {row['english_text'] or ''}",
        f"SPANISH: {row['spanish_text'] or ''}",
        f"OLD: {row['old_text'] or ''}",
        f"OUTPUT: {row['output_text'] or ''}",
        f"CANDIDATE: {row['candidate_text'] or ''}",
    ]
    return "\n".join(fields)


def make_text_structural(row: dict[str, Any]) -> str:
    fields = [
        f"PATH={row['relative_path']}",
        f"PATH_GROUP={path_group(row['relative_path'])}",
        f"KEY={row['source_key']}",
        f"KEY_SHAPE={key_shape(row['source_key'])}",
        f"LOCKED={row['locked']}",
        f"HAS_ENGLISH={row['has_english']}",
        f"HAS_OLD={row['has_old']}",
        bucket_number("TOKENS", row["token_count"]),
        bucket_number("TEXTLEN", row["text_length"]),
        text_shape("CANDIDATE_SHAPE", row["candidate_text"]),
        text_shape("OUTPUT_SHAPE", row["output_text"]),
        f"ENGLISH: {row['english_text'] or ''}",
        f"SPANISH: {row['spanish_text'] or ''}",
        f"OLD: {row['old_text'] or ''}",
        f"OUTPUT: {row['output_text'] or ''}",
        f"CANDIDATE: {row['candidate_text'] or ''}",
    ]
    return "\n".join(fields)


def make_text_delta(row: dict[str, Any]) -> str:
    return "\n".join([make_text_structural(row), *comparison_features(row)])


def make_text_language(row: dict[str, Any]) -> str:
    return "\n".join([make_text_delta(row), *language_features(row)])


def make_text(row: dict[str, Any], feature_set: str = DEFAULT_FEATURE_SET) -> str:
    if feature_set == LANGUAGE_FEATURE_SET:
        return make_text_language(row)
    if feature_set == DELTA_FEATURE_SET:
        return make_text_delta(row)
    if feature_set == STRUCTURAL_FEATURE_SET:
        return make_text_structural(row)
    return make_text_legacy(row)


def fetch_examples(conn, dataset_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            id,
            segment_id,
            relative_path,
            source_key,
            english_text,
            spanish_text,
            old_text,
            output_text,
            candidate_text,
            final_text,
            label,
            action_label,
            issue_label,
            trust_level,
            evidence_source,
            evidence_id,
            confidence_score,
            locked,
            token_count,
            has_english,
            has_old,
            text_length
        FROM ml_training_examples
        WHERE run_id = ?
          AND candidate_text IS NOT NULL
          AND candidate_text <> ''
        """,
        (dataset_run_id,),
    ).fetchall()
    examples: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        data["risk_label"] = risk_label(
            data["action_label"],
            data.get("issue_label"),
            candidate_text=data.get("candidate_text"),
            final_text=data.get("final_text"),
            dataset_label=data.get("label"),
        )
        examples.append(data)
    return examples


def balanced_examples(
    examples: list[dict[str, Any]],
    safe_multiplier: int,
    max_examples: int | None,
) -> list[dict[str, Any]]:
    safe = [row for row in examples if row["risk_label"] == "auto_safe"]
    risky = [row for row in examples if row["risk_label"] != "auto_safe"]
    rng = random.Random(RANDOM_SEED)
    rng.shuffle(safe)
    rng.shuffle(risky)
    safe_limit = min(len(safe), max(len(risky) * safe_multiplier, 1))
    selected = risky + safe[:safe_limit]
    if max_examples and len(selected) > max_examples:
        risky_count = min(len(risky), max_examples // 2)
        safe_count = max_examples - risky_count
        selected = risky[:risky_count] + safe[:safe_count]
    rng.shuffle(selected)
    return selected


def example_priority(row: dict[str, Any]) -> tuple[int, int, int, int]:
    evidence_score = {
        "issue_review_decision": 4,
        "local_learning_candidate": 3,
        "suggestion_feedback": 2,
        "segment_confirmation": 1,
    }.get(str(row.get("evidence_source") or ""), 0)
    evidence_id = int(row.get("evidence_id") or 0)
    trust_score = int(row.get("trust_level") or 0)
    risk_score = 1 if row["risk_label"] != "auto_safe" else 0
    return (evidence_score, evidence_id, trust_score, risk_score)


def deduplicate_examples(examples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best_by_key: dict[tuple[int, str, str, str], dict[str, Any]] = {}
    for row in examples:
        key = (int(row["segment_id"]), normalize_compare(row.get("candidate_text")))
        current = best_by_key.get(key)
        if current is None or example_priority(row) > example_priority(current):
            best_by_key[key] = row
    return list(best_by_key.values())


def sample_weight(row: dict[str, Any]) -> float:
    weight = 1.0
    if row["risk_label"] != "auto_safe":
        weight *= 3.0
    if row["risk_label"] == "blocked_structure":
        weight *= 1.5
    if row.get("evidence_source") == "local_learning_candidate":
        weight *= 1.5
    if row.get("evidence_source") == "issue_review_decision":
        weight *= 1.35
    if row.get("action_label") == "human_corrected":
        weight *= 1.25
    if row.get("issue_label") in {"semantic_error", "residual_spanish", "token_mismatch", "structure_error"}:
        weight *= 1.4
    if int(row.get("trust_level") or 0) >= 5:
        weight *= 1.15
    return weight


def select_examples(
    examples: list[dict[str, Any]],
    safe_multiplier: int,
    max_examples: int | None,
    train_strategy: str,
) -> list[dict[str, Any]]:
    if train_strategy == DEDUP_WEIGHTED_TRAIN_STRATEGY:
        examples = deduplicate_examples(examples)
        return balanced_examples(examples, safe_multiplier=max(2, safe_multiplier // 2), max_examples=max_examples)
    return balanced_examples(examples, safe_multiplier=safe_multiplier, max_examples=max_examples)


def fit_model(
    model: Pipeline,
    x_train: list[str],
    y_train: list[str],
    train_rows: list[dict[str, Any]],
    train_strategy: str,
) -> None:
    if train_strategy == DEDUP_WEIGHTED_TRAIN_STRATEGY:
        model.fit(x_train, y_train, classifier__sample_weight=[sample_weight(row) for row in train_rows])
        return
    model.fit(x_train, y_train)


def create_model() -> Pipeline:
    return Pipeline(
        steps=[
            (
                "tfidf",
                TfidfVectorizer(
                    analyzer="char_wb",
                    ngram_range=(3, 5),
                    min_df=2,
                    max_features=50000,
                    lowercase=True,
                ),
            ),
            (
                "classifier",
                SGDClassifier(
                    class_weight="balanced",
                    loss="log_loss",
                    max_iter=2000,
                    tol=1e-4,
                    random_state=RANDOM_SEED,
                ),
            ),
        ]
    )


def threshold_predictions(
    model: Pipeline,
    texts: list[str],
    safe_threshold: float,
    rows: list[dict[str, Any]] | None = None,
) -> list[str]:
    classes = list(model.named_steps["classifier"].classes_)
    probabilities = model.predict_proba(texts)
    safe_index = classes.index("auto_safe")
    predictions: list[str] = []
    for index, row in enumerate(probabilities):
        source_row = rows[index] if rows is not None else None
        language_blocked = False
        if source_row is not None:
            language_blocked = bool(set(language_features(source_row)) & LANGUAGE_BLOCKING_FEATURES)
            if has_bold_no_to_nao_microrepair(source_row) and not candidate_differs_from_output(source_row):
                predictions.append("auto_safe")
                continue
            if has_confirmed_title_adjective_lexical_repair(source_row) and not candidate_differs_from_output(source_row):
                predictions.append("auto_safe")
                continue
            if has_occidental_to_ocidental_title_repair(source_row) and not candidate_differs_from_output(source_row):
                predictions.append("auto_safe")
                continue
        if (
            row[safe_index] >= safe_threshold
            and not candidate_differs_from_output(source_row)
            and not language_blocked
        ):
            predictions.append("auto_safe")
        else:
            best_index = max(
                (index for index, label in enumerate(classes) if label != "auto_safe"),
                key=lambda index: row[index],
            )
            predictions.append(classes[best_index])
    return predictions


def false_safe_stats(y_true: list[str], y_pred: list[str]) -> dict[str, float | int]:
    predicted_safe = sum(1 for label in y_pred if label == "auto_safe")
    actual_safe = sum(1 for label in y_true if label == "auto_safe")
    true_safe = sum(1 for truth, pred in zip(y_true, y_pred) if truth == "auto_safe" and pred == "auto_safe")
    false_safe = sum(1 for truth, pred in zip(y_true, y_pred) if truth != "auto_safe" and pred == "auto_safe")
    safe_precision = true_safe / predicted_safe if predicted_safe else 0.0
    safe_recall = true_safe / actual_safe if actual_safe else 0.0
    false_safe_rate = false_safe / predicted_safe if predicted_safe else 0.0
    return {
        "predicted_safe": predicted_safe,
        "actual_safe": actual_safe,
        "true_safe": true_safe,
        "false_safe": false_safe,
        "safe_precision": safe_precision,
        "safe_recall": safe_recall,
        "false_safe_rate": false_safe_rate,
    }


def false_safe_samples(
    rows: list[dict[str, Any]],
    y_true: list[str],
    y_pred: list[str],
    sample_limit: int = 20,
) -> list[dict[str, Any]]:
    samples = []
    for row, truth, pred in zip(rows, y_true, y_pred):
        if truth == "auto_safe" or pred != "auto_safe":
            continue
        samples.append(
            {
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "truth_label": truth,
                "predicted_label": pred,
                "action_label": row.get("action_label"),
                "issue_label": row.get("issue_label"),
                "dataset_label": row.get("label"),
                "evidence_source": row.get("evidence_source"),
                "evidence_id": row.get("evidence_id"),
                "candidate_text": row.get("candidate_text"),
                "final_text": row.get("final_text"),
                "english_text": row.get("english_text"),
                "spanish_text": row.get("spanish_text"),
            }
        )
        if len(samples) >= sample_limit:
            break
    return samples


def insert_model_run(
    conn,
    dataset_run_id: int,
    model_version: str,
    safe_threshold: float,
    started_at: str,
    model_kind: str = MODEL_KIND,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO ml_model_runs (
            rule_version,
            model_version,
            model_kind,
            dataset_run_id,
            safe_threshold,
            started_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (RULE_VERSION, model_version, model_kind, dataset_run_id, safe_threshold, started_at, started_at),
    )
    return int(cursor.lastrowid)


def update_model_run(conn, run_id: int, values: dict[str, Any]) -> None:
    conn.execute(
        """
        UPDATE ml_model_runs
        SET
            model_path = ?,
            training_examples = ?,
            test_examples = ?,
            accuracy = ?,
            macro_f1 = ?,
            predicted_safe_count = ?,
            false_safe_count = ?,
            false_safe_rate = ?,
            safe_precision = ?,
            safe_recall = ?,
            metrics_json = ?,
            finished_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            values["model_path"],
            values["training_examples"],
            values["test_examples"],
            values["accuracy"],
            values["macro_f1"],
            values["predicted_safe_count"],
            values["false_safe_count"],
            values["false_safe_rate"],
            values["safe_precision"],
            values["safe_recall"],
            values["metrics_json"],
            values["finished_at"],
            values["finished_at"],
            run_id,
        ),
    )


def main(
    dataset_run_id: int | None = None,
    safe_threshold: float = 0.90,
    safe_multiplier: int = 5,
    max_examples: int | None = None,
    feature_set: str = DEFAULT_FEATURE_SET,
    train_strategy: str = DEFAULT_TRAIN_STRATEGY,
    model_kind: str = MODEL_KIND,
) -> int:
    settings = db.load_settings()
    started_at_dt = datetime.now()
    started_at = started_at_dt.isoformat(timespec="seconds")
    print("[ml_train_risk] Starting local risk classifier training")
    print(f"[ml_train_risk] Rule version: {RULE_VERSION}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        dataset_run_id = dataset_run_id or latest_dataset_run_id(conn)
        examples = fetch_examples(conn, dataset_run_id)
        selected = select_examples(
            examples,
            safe_multiplier=safe_multiplier,
            max_examples=max_examples,
            train_strategy=train_strategy,
        )
        label_counts = Counter(row["risk_label"] for row in selected)
        if len(label_counts) < 2:
            raise RuntimeError("Need at least two risk labels to train.")

        train_rows, test_rows = train_test_split(
            selected,
            test_size=0.20,
            random_state=RANDOM_SEED,
            stratify=[row["risk_label"] for row in selected],
        )
        x_train = [make_text(row, feature_set=feature_set) for row in train_rows]
        y_train = [row["risk_label"] for row in train_rows]
        x_test = [make_text(row, feature_set=feature_set) for row in test_rows]
        y_test = [row["risk_label"] for row in test_rows]

        model_prefix = re.sub(r"[^a-zA-Z0-9_]+", "_", model_kind).strip("_") or "risk_classifier"
        model_version = f"{model_prefix}_v{dataset_run_id:04d}_{started_at_dt.strftime('%Y%m%d_%H%M%S')}"
        model_run_id = insert_model_run(
            conn,
            dataset_run_id,
            model_version,
            safe_threshold,
            started_at,
            model_kind=model_kind,
        )
        print(f"[ml_train_risk] Dataset run id: {dataset_run_id}")
        print(f"[ml_train_risk] Model run id: {model_run_id}")
        print(f"[ml_train_risk] Selected examples: {len(selected)}")
        print(f"[ml_train_risk] Label counts: {dict(label_counts)}")
        print(f"[ml_train_risk] Train strategy: {train_strategy}")

        model = create_model()
        fit_model(model, x_train, y_train, train_rows, train_strategy)

        raw_predictions = list(model.predict(x_test))
        thresholded_predictions = threshold_predictions(model, x_test, safe_threshold, test_rows)
        accuracy = accuracy_score(y_test, thresholded_predictions)
        macro_f1 = f1_score(y_test, thresholded_predictions, average="macro", zero_division=0)
        stats = false_safe_stats(y_test, thresholded_predictions)
        false_safe_sample_rows = false_safe_samples(test_rows, y_test, thresholded_predictions)
        labels_sorted = sorted(label_counts)
        report = classification_report(
            y_test,
            thresholded_predictions,
            labels=labels_sorted,
            zero_division=0,
            output_dict=True,
        )
        raw_report = classification_report(
            y_test,
            raw_predictions,
            labels=labels_sorted,
            zero_division=0,
            output_dict=True,
        )
        matrix = confusion_matrix(y_test, thresholded_predictions, labels=labels_sorted).tolist()

        models_dir = db.project_path(settings.get("models_dir", "memory/models"))
        models_dir.mkdir(parents=True, exist_ok=True)
        model_path = models_dir / f"{model_version}.joblib"
        metadata = {
            "rule_version": RULE_VERSION,
            "model_version": model_version,
            "model_kind": model_kind,
            "dataset_run_id": dataset_run_id,
            "safe_threshold": safe_threshold,
            "safe_multiplier": safe_multiplier,
            "max_examples": max_examples,
            "feature_set": feature_set,
            "train_strategy": train_strategy,
            "labels": labels_sorted,
            "created_at": started_at,
        }
        joblib.dump({"model": model, "metadata": metadata}, model_path)

        finished_at = now()
        metrics = {
            "label_counts": dict(label_counts),
            "labels": labels_sorted,
            "classification_report": report,
            "raw_classification_report": raw_report,
            "confusion_matrix": matrix,
            "false_safe_stats": stats,
            "false_safe_samples": false_safe_sample_rows,
            "train_strategy": train_strategy,
        }
        update_model_run(
            conn,
            model_run_id,
            {
                "model_path": str(model_path.relative_to(db.PROJECT_ROOT)),
                "training_examples": len(x_train),
                "test_examples": len(x_test),
                "accuracy": accuracy,
                "macro_f1": macro_f1,
                "predicted_safe_count": stats["predicted_safe"],
                "false_safe_count": stats["false_safe"],
                "false_safe_rate": stats["false_safe_rate"],
                "safe_precision": stats["safe_precision"],
                "safe_recall": stats["safe_recall"],
                "metrics_json": json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                "finished_at": finished_at,
            },
        )
        conn.commit()

    elapsed = datetime.now() - started_at_dt
    report_lines = [
        "ML risk classifier training report",
        f"Started at: {started_at}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Model version: {model_version}",
        f"Model kind: {model_kind}",
        f"Dataset run id: {dataset_run_id}",
        f"Model run id: {model_run_id}",
        f"Model path: {model_path}",
        "",
        "Training sample:",
        f"- Selected examples: {len(selected)}",
        f"- Training examples: {len(x_train)}",
        f"- Test examples: {len(x_test)}",
        f"- Safe multiplier: {safe_multiplier}",
        f"- Safe threshold: {safe_threshold:.2f}",
        f"- Feature set: {feature_set}",
        f"- Train strategy: {train_strategy}",
        "",
        "Label counts:",
        *[f"- {label}: {label_counts[label]}" for label in sorted(label_counts)],
        "",
        "Evaluation:",
        f"- Accuracy: {accuracy:.4f}",
        f"- Macro F1: {macro_f1:.4f}",
        f"- Predicted safe: {stats['predicted_safe']}",
        f"- False safe: {stats['false_safe']}",
        f"- False safe rate among predicted safe: {percent(stats['false_safe_rate'])}",
        f"- Safe precision: {percent(stats['safe_precision'])}",
        f"- Safe recall: {percent(stats['safe_recall'])}",
        "",
        "False-safe samples:",
    ]
    if false_safe_sample_rows:
        report_lines.extend(
            [
                (
                    f"- segment {sample['segment_id']} | {sample['relative_path']}::{sample['source_key']} | "
                    f"action={sample['action_label']} | issue={sample['issue_label'] or 'none'} | "
                    f"evidence={sample['evidence_source']}:{sample['evidence_id']} | "
                    f"candidate=\"{sample['candidate_text'] or ''}\" | final=\"{sample['final_text'] or ''}\""
                )
                for sample in false_safe_sample_rows
            ]
        )
    else:
        report_lines.append("- none")
    report_lines.extend(
        [
            "",
            "Classification report:",
        ]
    )
    for label in labels_sorted:
        label_stats = report.get(label, {})
        report_lines.append(
            f"- {label}: precision={label_stats.get('precision', 0):.4f}, "
            f"recall={label_stats.get('recall', 0):.4f}, "
            f"f1={label_stats.get('f1-score', 0):.4f}, "
            f"support={int(label_stats.get('support', 0))}"
        )
    report_lines.extend(
        [
            "",
            "Confusion matrix labels:",
            f"- {', '.join(labels_sorted)}",
            "Confusion matrix rows:",
            *[f"- {row}" for row in matrix],
            "",
            "Interpretation:",
            "- This is a first local classifier for risk/action triage only.",
            "- It must not apply translations automatically.",
            "- The main safety metric is false safe count/rate.",
            "- Future runs should add more negative examples and validate on package-level holdouts.",
        ]
    )
    report_path = db.write_report(settings, "ml_train_risk", report_lines)
    print(f"[ml_train_risk] Accuracy: {accuracy:.4f}")
    print(f"[ml_train_risk] Macro F1: {macro_f1:.4f}")
    print(
        "[ml_train_risk] False safe: "
        f"{stats['false_safe']}/{stats['predicted_safe']} "
        f"({percent(stats['false_safe_rate'])})"
    )
    print(f"[ml_train_risk] Model: {model_path}")
    print(f"[ml_train_risk] Report: {report_path}")
    print("[ml_train_risk] Done")
    return model_run_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train a local conservative risk classifier.")
    parser.add_argument("--dataset-run-id", type=int, default=None)
    parser.add_argument("--safe-threshold", type=float, default=0.90)
    parser.add_argument("--safe-multiplier", type=int, default=5)
    parser.add_argument("--max-examples", type=int, default=None)
    parser.add_argument("--feature-set", choices=FEATURE_SETS, default=DEFAULT_FEATURE_SET)
    parser.add_argument("--train-strategy", choices=TRAIN_STRATEGIES, default=DEFAULT_TRAIN_STRATEGY)
    parser.add_argument("--model-kind", default=MODEL_KIND)
    args = parser.parse_args()
    main(
        dataset_run_id=args.dataset_run_id,
        safe_threshold=args.safe_threshold,
        safe_multiplier=args.safe_multiplier,
        max_examples=args.max_examples,
        feature_set=args.feature_set,
        train_strategy=args.train_strategy,
        model_kind=args.model_kind,
    )
