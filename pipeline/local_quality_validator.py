from __future__ import annotations

import re


RULE_VERSION = "local_quality_validator_v1"

WORD_PATTERN = re.compile(r"[A-Za-z\u00c0-\u00ff]+", re.UNICODE)
PROTECTED_TOKEN_PATTERN = re.compile(
    r"\$[^$\s]+\$|\[[^\]]+\]|#[A-Za-z0-9_]+|#!|@[A-Za-z0-9_]+!|\\n"
)
STRING_LITERAL_PATTERN = re.compile(r"'([^']*)'|\"([^\"]*)\"")

SPANISH_PUNCTUATION = (
    "\u00bf",
    "\u00a1",
    "\u00ab",
    "\u00bb",
    "\u00c2\u00bf",
    "\u00c2\u00a1",
    "\u00c2\u00ab",
    "\u00c2\u00bb",
)

SPANISH_RESIDUE_WORDS = {
    "aguantar",
    "cabeza",
    "contienda",
    "cortesano",
    "cortesanos",
    "cria",
    "crio",
    "decision",
    "decisiones",
    "del",
    "ella",
    "eres",
    "esta",
    "este",
    "gobernante",
    "gobernantes",
    "gusano",
    "ha",
    "hacendado",
    "hacendados",
    "inmundicia",
    "intratable",
    "invitado",
    "invitados",
    "la",
    "las",
    "lo",
    "los",
    "mendiga",
    "mendigo",
    "nueva",
    "nuevas",
    "pestes",
    "rechaza",
    "rechazar",
    "retorciendose",
    "retorci\u00e9ndose",
    "situacion",
    "situaciones",
    "su",
    "tus",
    "un",
    "una",
}

TOKEN_JOINED_TO_WORD_PATTERN = re.compile(
    r"(\]|\$[A-Za-z0-9_]+\$|#!)(?=[A-Za-z\u00c0-\u00ff])"
)
WORD_JOINED_TO_TOKEN_PATTERN = re.compile(
    r"(?<=[A-Za-z\u00c0-\u00ff])(\$[A-Za-z0-9_]+\$|#(?:EMP|V|P|N|bold|weak)\b)"
)
CYRILLIC_OR_MOJIBAKE_PATTERN = re.compile(r"[\u0400-\u04ff\ufffdŤťĂă]")
GENDER_TOKEN_SUFFIX_PATTERN = re.compile(
    r"\[[^\]]*Custom\(\s*['\"]ES_(?:OA|AO)['\"]\s*\)\](?=[ao]\b)",
    re.IGNORECASE,
)
LOCALIZATION_GENDER_FLOW_PATTERN = re.compile(
    r"\[[^\]]*Custom\(\s*['\"]ES_(?:LeLa|LoLa|DelDela)['\"]\s*\)\](?=[A-Za-z\u00c0-\u00ff])",
    re.IGNORECASE,
)


def normalize(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.casefold().split())


def strip_tokens(value: str | None) -> str:
    if not value:
        return ""
    stripped = PROTECTED_TOKEN_PATTERN.sub(" ", value)
    stripped = re.sub(r"[$#@!|_:\[\].()0-9\\/-]+", " ", stripped)
    return " ".join(stripped.split())


def word_count(value: str | None) -> int:
    return len(WORD_PATTERN.findall(strip_tokens(value)))


def literal_texts(value: str | None) -> list[str]:
    if not value:
        return []
    literals: list[str] = []
    for match in STRING_LITERAL_PATTERN.finditer(value):
        literals.append(match.group(1) if match.group(1) is not None else match.group(2))
    return literals


def count_spanish_residue(value: str | None) -> tuple[int, list[str]]:
    if not value:
        return 0, []
    normalized = normalize(value)
    found: list[str] = []
    for word in sorted(SPANISH_RESIDUE_WORDS):
        if re.search(rf"\b{re.escape(word)}\b", normalized):
            found.append(word)
    return len(found), found


def count_literal_spanish_residue(value: str | None) -> tuple[int, list[str]]:
    found: list[str] = []
    for literal in literal_texts(value):
        _, literal_found = count_spanish_residue(literal)
        found.extend(literal_found)
        if any(mark in literal for mark in SPANISH_PUNCTUATION):
            found.append("spanish_punctuation_in_literal")
    return len(found), sorted(set(found))


def validate_text(value: str | None) -> dict:
    issues: list[dict] = []
    text = value or ""
    words = word_count(text)

    marks = [mark for mark in SPANISH_PUNCTUATION if mark in text]
    if marks:
        issues.append(
            {
                "code": "spanish_punctuation",
                "severity": "high",
                "message": "Spanish inverted or angular punctuation remains.",
                "matches": marks[:10],
            }
        )

    if CYRILLIC_OR_MOJIBAKE_PATTERN.search(text):
        issues.append(
            {
                "code": "mojibake_or_unexpected_script",
                "severity": "high",
                "message": "Unexpected mojibake or non-Latin script remains in text.",
            }
        )

    residue_count, residues = count_spanish_residue(text)
    if residue_count:
        issues.append(
            {
                "code": "spanish_residue",
                "severity": "high" if residue_count >= 2 else "medium",
                "message": "Known Spanish residue remains in human text.",
                "matches": residues[:20],
            }
        )

    literal_count, literal_residues = count_literal_spanish_residue(text)
    if literal_count:
        issues.append(
            {
                "code": "spanish_residue_in_literal",
                "severity": "high",
                "message": "Translatable string literal inside a token still looks Spanish.",
                "matches": literal_residues[:20],
            }
        )

    if TOKEN_JOINED_TO_WORD_PATTERN.search(text):
        issues.append(
            {
                "code": "missing_space_after_token",
                "severity": "medium",
                "message": "A token is joined directly to following human text.",
            }
        )

    if WORD_JOINED_TO_TOKEN_PATTERN.search(text):
        issues.append(
            {
                "code": "missing_space_before_token",
                "severity": "medium",
                "message": "Human text is joined directly to a formatting token.",
            }
        )

    if GENDER_TOKEN_SUFFIX_PATTERN.search(text):
        issues.append(
            {
                "code": "gender_token_extra_suffix",
                "severity": "high",
                "message": "A gender token such as ES_OA is followed by an extra a/o suffix.",
            }
        )

    if LOCALIZATION_GENDER_FLOW_PATTERN.search(text):
        issues.append(
            {
                "code": "gender_token_joined_to_word",
                "severity": "medium",
                "message": "A gender/localization token is joined to the next word.",
            }
        )

    high_count = sum(1 for issue in issues if issue["severity"] == "high")
    medium_count = sum(1 for issue in issues if issue["severity"] == "medium")

    confidence_cap = 0.99
    if words >= 70:
        confidence_cap = min(confidence_cap, 0.92)
    elif words >= 30:
        confidence_cap = min(confidence_cap, 0.95)

    if high_count:
        confidence_cap = min(confidence_cap, 0.79 if words >= 30 else 0.88)
    elif medium_count:
        confidence_cap = min(confidence_cap, 0.90 if words >= 30 else 0.94)

    auto_approval_blocked = high_count > 0 or medium_count > 0 or words >= 70
    return {
        "rule_version": RULE_VERSION,
        "word_count": words,
        "issues": issues,
        "issue_count": len(issues),
        "high_issue_count": high_count,
        "medium_issue_count": medium_count,
        "confidence_cap": confidence_cap,
        "auto_approval_blocked": auto_approval_blocked,
    }
