from __future__ import annotations

import re
from typing import Any

import local_quality_validator


EXACT_SHARED_GLOSSARY_PATTERN = "exact_shared_glossary_token"
OUTSIDE_CUSTOM_GLOSSARY_PATTERN = "outside_custom_glossary_token"
CONTRACT_ES_HELPER_PATTERN = "contract_es_helper"
CONTRACT_ES_OA_SUFFIX_PATTERN = "contract_es_oa_suffix_only"
CONTRACT_ES_ARTICLE_PREPOSITION_PATTERN = (
    "contract_es_article_preposition_helper"
)

CUSTOM_LOCALIZATION_PREFIX = "custom_localization/"
CONTRACTS_PREFIX = "contracts/"
ARTICLE_PREPOSITION_HELPERS = {
    "es_alala",
    "es_deldela",
    "es_ella",
}
INVALID_ES_OA_STEMS = {
    "algum",
    "bom",
    "nenhum",
    "um",
    "uma",
}
ES_HELPER_TOKEN_PATTERN = re.compile(
    r"""
    \[
      [^\[\]]*?
      \.Custom\(
        \s*(['"])(ES_[A-Za-z0-9_]+)\1\s*
      \)
      [^\[\]]*
    \]
    """,
    re.VERBOSE,
)
WORD_BEFORE_TOKEN_PATTERN = re.compile(
    r"([A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u00ff]{2,})$"
)


def normalized_record(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    normalized["candidate_text"] = (
        normalized.get("candidate_text")
        if normalized.get("candidate_text") is not None
        else normalized.get("output_text")
    )
    return normalized


def relative_path(row: dict[str, Any]) -> str:
    return str(row.get("relative_path") or "").replace("\\", "/")


def candidate_text(row: dict[str, Any]) -> str:
    normalized = normalized_record(row)
    return str(normalized.get("candidate_text") or "")


def es_helper_matches(row: dict[str, Any]) -> list[re.Match[str]]:
    return list(ES_HELPER_TOKEN_PATTERN.finditer(candidate_text(row)))


def es_helper_names(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(match.group(2) for match in es_helper_matches(row))


def is_custom_exact_shared_glossary(row: dict[str, Any]) -> bool:
    normalized = normalized_record(row)
    return (
        relative_path(normalized).startswith(CUSTOM_LOCALIZATION_PREFIX)
        and local_quality_validator.is_exact_shared_glossary_token(normalized)
    )


def is_outside_custom_exact_shared_glossary(row: dict[str, Any]) -> bool:
    normalized = normalized_record(row)
    path = relative_path(normalized)
    return (
        bool(path)
        and not path.startswith(CUSTOM_LOCALIZATION_PREFIX)
        and local_quality_validator.is_exact_shared_glossary_token(normalized)
    )


def is_contract_es_helper(row: dict[str, Any]) -> bool:
    return (
        relative_path(row).startswith(CONTRACTS_PREFIX)
        and bool(es_helper_matches(row))
    )


def is_contract_es_oa_suffix_only(row: dict[str, Any]) -> bool:
    if not relative_path(row).startswith(CONTRACTS_PREFIX):
        return False
    text = candidate_text(row)
    matches = es_helper_matches(row)
    if not matches or any(match.group(2).casefold() != "es_oa" for match in matches):
        return False
    for match in matches:
        stem_match = WORD_BEFORE_TOKEN_PATTERN.search(text[: match.start()])
        if stem_match is None:
            return False
        stem = stem_match.group(1).casefold()
        if stem in INVALID_ES_OA_STEMS or stem.endswith(("a", "o")):
            return False
    return True


def is_contract_es_article_preposition_helper(row: dict[str, Any]) -> bool:
    if not relative_path(row).startswith(CONTRACTS_PREFIX):
        return False
    return any(
        helper_name.casefold() in ARTICLE_PREPOSITION_HELPERS
        for helper_name in es_helper_names(row)
    )


def has_glossary_surface(row: dict[str, Any]) -> bool:
    normalized = normalized_record(row)
    return any(
        "Glossary" in str(normalized.get(field) or "")
        for field in (
            "candidate_text",
            "english_text",
            "spanish_text",
        )
    )
