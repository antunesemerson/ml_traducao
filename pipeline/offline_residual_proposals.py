from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

import db
import local_quality_validator
from apply_safe_output_updates import protected_tokens
from pending_diagnostic import classify_bucket, fast_classify_item, fetch_pending_rows


RULE_VERSION = "offline_residual_proposals_v2"
MODEL_VERSION = "rules_memory_glossary_bootstrap_v2"

TOKEN_PATTERN = re.compile(r"\[[^\]]+\]")
PROTECTED_TOKEN_PATTERN = re.compile(
    r"\$[^$\s]+\$|\[[^\]]+\]|#[A-Za-z0-9_]+|#!|@[A-Za-z0-9_]+!|\\n"
)
STRING_LITERAL_PATTERN = re.compile(r"'([^']*)'|\"([^\"]*)\"")
WORD_CHARS = r"A-Za-zÀ-ÿ"

TARGET_BUCKETS = {
    "translation_required",
    "residual_spanish_long_text",
    "residual_spanish_short_text",
    "inline_literal_residue",
    "mechanical_autofix",
    "encoding_repair",
}

MOJIBAKE_REPLACEMENTS = {
    "Ã¡": "á",
    "ÃÁ": "Á",
    "Ã¢": "â",
    "Ã£": "ã",
    "ÃÃ": "Ã",
    "Ã§": "ç",
    "Ã©": "é",
    "Ã‰": "É",
    "Ãª": "ê",
    "Ã­": "í",
    "Ã³": "ó",
    "Ã´": "ô",
    "Ãµ": "õ",
    "Ãº": "ú",
    "Ã¼": "ü",
    "Ã±": "ñ",
    "Â¿": "",
    "Â¡": "",
    "Â«": '"',
    "Â»": '"',
    "Âº": "º",
    "Âª": "ª",
    "�": "",
}

PUNCTUATION_REPLACEMENTS = {
    "¿": "",
    "¡": "",
    "«": '"',
    "»": '"',
}

SAFE_LITERAL_REPLACEMENTS = {
    "idioma de tu corte": "idioma da sua corte",
    "tu favor": "seu favor",
    "tu persona": "você",
    "tu personaje": "seu personagem",
    "tu señorío": "seu senhorio",
    "tu señor": "seu senhor",
    "cabeza de tu fe": "chefe da sua fé",
    "independientes": "independentes",
    "independiente": "independente",
    "decision": "decisão",
    "decisión": "decisão",
    "decisiones": "decisões",
    "situacion": "situação",
    "situación": "situação",
    "situaciones": "situações",
    "cortesano": "cortesão",
    "cortesanos": "cortesões",
    "cortesana": "cortesã",
    "cortesanas": "cortesãs",
    "consejo": "conselho",
    "consejos": "conselhos",
    "rechaza": "rejeita",
    "rechazar": "rejeitar",
    "rechazado": "rejeitado",
    "rechazada": "rejeitada",
    "libertad": "liberdade",
    "derecho": "direito",
    "derechos": "direitos",
    "vasallaje": "vassalagem",
    "vasallo": "vassalo",
    "vasalla": "vassala",
    "vasall": "vassalo",
    "gobernante": "governante",
    "gobernantes": "governantes",
    "invitado": "convidado",
    "invitados": "convidados",
    "invitada": "convidada",
    "invitadas": "convidadas",
    "hacendado": "com terras",
    "hacendados": "com terras",
    "cría": "menina",
    "crío": "menino",
    "una cría": "uma menina",
    "un crío": "um menino",
    "una poetisa": "uma poetisa",
    "un poeta": "um poeta",
    "una gran poetisa": "uma grande poetisa",
    "un gran poeta": "um grande poeta",
    "retorciéndose": "se retorcendo",
    "inmundicia": "imundície",
    "flechazo": "paixão",
    "se mete": "intimida",
    "un día": "um dia",
    "de verdad": "de verdade",
    "tamaño": "tamanho",
    "coste": "custo",
    "deudas": "dívidas",
}

VISIBLE_REPLACEMENTS = {
    "#EMP un#!": "#EMP um#!",
    "#EMP una#!": "#EMP uma#!",
    "#EMP el#!": "#EMP o#!",
    "#EMP la#!": "#EMP a#!",
    "#EMP los#!": "#EMP os#!",
    "#EMP las#!": "#EMP as#!",
    "#EMP es#!": "#EMP é#!",
    "#EMP debe#!": "#EMP deve#!",
    "#EMP y#!": "#EMP e#!",
    "#EMP mi#!": "#EMP minha#!",
    "#EMP su#!": "#EMP sua#!",
    "#EMP de verdad#!": "#EMP de verdade#!",
    "#EMP mucho cuidado#!": "#EMP muito cuidado#!",
    "#EMP cuidado aún mayor#!": "#EMP cuidado ainda maior#!",
    "#EMP la Santísima Virgen María#!": "#EMP a Santíssima Virgem Maria#!",
    "#EMP Haré que la use!#!": "#EMP Farei com que use!#!",
    "#EMP superaré#!": "#EMP superarei#!",
    "#EMP llama#!": "#EMP chama#!",
    "#EMP venganza#!": "#EMP vingança#!",
    "#EMP malnacid": "#EMP maldit",
    "#EMP Canalla": "#EMP Canalha",
    "#EMP canalla": "#EMP canalha",
    "#EMP No!#!": "#EMP Não!#!",
    "¡No!": "Não!",
    "¡Ja!": "Ha!",
    "¡Ha!": "Ha!",
    "¡Guardias!": "Guardas!",
    "Guardias!": "Guardas!",
    "Puede que le pillen!#!": "Podem pegá-lo!#!",
    "Puede que te pillen!#!": "Podem pegar você!#!",
    "¡Puede que le pillen!#!": "Podem pegá-lo!#!",
    "¡Puede que te pillen!#!": "Podem pegar você!#!",
    "#V ti#!": "#V você#!",
    "#bold no#!": "#bold não#!",
    "#bold No#!": "#bold Não#!",
    "Renda Mensal do [income|E] por nível de [stress_level|lE]": "[income|E] mensal por [stress_level|lE]",
    "Renda Mensal do Renda por nível de nível de Estresse": "[income|E] mensal por [stress_level|lE]",
    "Renda Mensal do [income|E]": "[income|E] mensal",
    "Renda Mensal do Renda": "[income|E] mensal",
    "Libértema": "Libertem",
    "Libertema": "Libertem",
    "libértema": "libertem",
    "libertema": "libertem",
    "beneficio": "benefício",
    "Beneficio": "Benefício",
    "Administración": "Administração",
    "administración": "administração",
}

GENDER_TOKEN_EXTRA_SUFFIX_PATTERN = re.compile(
    r"(\[[^\]]*Custom\(\s*['\"]ES_(?:OA|AO)['\"]\s*\)\])([ao])\b",
    re.IGNORECASE,
)
GENDER_TOKEN_JOINED_PATTERN = re.compile(
    r"(\[[^\]]*Custom\(\s*['\"]ES_(?:LeLa|LoLa|DelDela)['\"]\s*\)\])(?=[A-Za-zÀ-ÿ])",
    re.IGNORECASE,
)
STYLE_TOKEN_JOINED_TO_WORD_PATTERN = re.compile(r"(#!|\$[A-Za-z0-9_]+\$)(?=[^\W\d_])")
BRACKET_TOKEN_JOINED_TO_WORD_PATTERN = re.compile(
    r"(\](?![aos]\b|as\b|os\b))(?=[^\W\d_])",
    re.IGNORECASE,
)
WORD_JOINED_TO_TOKEN_PATTERN = re.compile(
    r"(?<!\\n)(?<!\\t)(?<=[^\W\d_])(\$[A-Za-z0-9_]+\$|#(?:EMP|V|P|N|bold|weak)\b)"
)
SPACE_BEFORE_PUNCTUATION_PATTERN = re.compile(r"\s+([,.;:!?])")
SPANISH_RISK_PATTERN = re.compile(
    r"\b("
    r"fuera|cuando|sí|mí|bien|llama|pertenece|debe|"
    r"malnacid[ao]?|respondón|respondona|"
    r"mucho|aplicarlo|acuerdo|sabía|sus|espeluznantes|maestr[ao]?|"
    r"qué|cómo|diantres|haré|debéis|canalla|gusano|doquier|aficionad[ao]s?|"
    r"eso|maravilloso|tengo|escoria|estudio|esto|dos-horas|"
    r"nuestr[ao]?|amad[ao]?|contempla|debo|imb[eé]ciles|exhibir|esa|"
    r"fracaso|mi|mí|m[ií]a|m[ií]o|hasta|ahora|mejor|todo|"
    r"recibe|beneficios?|t[eé]"
    r")\b",
    re.IGNORECASE,
)
TRANSLATABLE_LITERAL_RISK_PATTERN = re.compile(
    r"\b("
    r"un|una|el|la|los|las|al|del|suyo|suya|"
    r"eres|es|seguir[aá]s|seguir[aá]|tienes|tiene|"
    r"respond[oó]n|respondona|m[ií]a|m[ií]o|mejor|hasta|ahora|"
    r"vasallos?|vasallas?|asalto|todo|fertilidad|inspiraci[oó]n"
    r")\b",
    re.IGNORECASE,
)
PUNCTUATION_ONLY_ALLOWED_RESIDUES = {"ha"}
EMPHASIS_HUMAN_TEXT_PATTERN = re.compile(r"#EMP[^#]*[A-Za-zÀ-ÿ][^#]*#!")
QUOTE_AFTER_ES_CUSTOM_TOKEN_PATTERN = re.compile(r"Custom\(\s*['\"]ES_[^'\"]+['\"]\s*\)\]\"")
SEPARATED_GENDER_SUFFIX_PATTERN = re.compile(
    r"\[[^\]]*Custom\(\s*['\"]ES_[^'\"]+['\"]\s*\)\]\s+(?:s|es|os|as)\b",
    re.IGNORECASE,
)
PUNCTUATION_REPAIR_RISK_PATTERN = re.compile(
    r"\b(?:de|do|da|dos|das|em|a|o|e)[.!?]|:[#@$]|#!\s*(?:pt-BR|en)\b",
    re.IGNORECASE,
)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sample_text(value: str | None, limit: int = 220) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t")
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def normalize_key(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def command_base_name(token: str) -> str:
    command_name = token[1:].split("(", 1)[0].split("|", 1)[0].strip()
    return command_name.split(".")[-1]


def is_literal_translatable(base_name: str, literal_index: int, literal: str) -> bool:
    if local_quality_validator.is_technical_literal(literal):
        return False
    if base_name == "Concept":
        return literal_index >= 2
    if (
        base_name in {"Select_CString", "LocalPlayerString", "PlayerString", "GetString"}
        or base_name.startswith("SelectLocalization")
        or base_name.endswith("String")
    ):
        return True
    return False


def replace_case_aware(source: str, target: str, text: str) -> tuple[str, int]:
    pattern = re.compile(rf"(?<![{WORD_CHARS}]){re.escape(source)}(?![{WORD_CHARS}])", re.IGNORECASE)

    def repl(match: re.Match) -> str:
        value = match.group(0)
        if value.isupper():
            return target.upper()
        if value[:1].isupper():
            return target[:1].upper() + target[1:]
        return target

    return pattern.subn(repl, text)


def apply_mojibake_and_punctuation(text: str) -> tuple[str, list[str]]:
    fixed = text
    rules: list[str] = []
    for source, target in MOJIBAKE_REPLACEMENTS.items():
        if source in fixed:
            fixed = fixed.replace(source, target)
            rules.append("fix_mojibake")
    for source, target in PUNCTUATION_REPLACEMENTS.items():
        if source in fixed:
            fixed = fixed.replace(source, target)
            rules.append("normalize_spanish_punctuation")
    return fixed, sorted(set(rules))


def apply_visible_replacements(text: str) -> tuple[str, list[str]]:
    fixed = text
    rules: list[str] = []
    for source, target in sorted(VISIBLE_REPLACEMENTS.items(), key=lambda item: len(item[0]), reverse=True):
        if source in fixed:
            fixed = fixed.replace(source, target)
            rules.append("visible_phrase_replacement")
    for source, target in sorted(SAFE_LITERAL_REPLACEMENTS.items(), key=lambda item: len(item[0]), reverse=True):
        fixed, count = replace_case_aware(source, target, fixed)
        if count:
            rules.append("visible_word_replacement")
    return fixed, sorted(set(rules))


def fix_string_literals_in_token(token: str) -> tuple[str, list[str]]:
    base_name = command_base_name(token)
    literal_index = 0
    rules: list[str] = []

    def repl(match: re.Match) -> str:
        nonlocal literal_index
        literal_index += 1
        quote = "'" if match.group(1) is not None else '"'
        literal = match.group(1) if match.group(1) is not None else match.group(2)
        if literal is None or not is_literal_translatable(base_name, literal_index, literal):
            return match.group(0)
        fixed, literal_rules = apply_mojibake_and_punctuation(literal)
        fixed, visible_rules = apply_visible_replacements(fixed)
        if fixed == literal:
            return match.group(0)
        rules.extend(literal_rules)
        rules.extend(visible_rules)
        rules.append("inline_literal_replacement")
        return f"{quote}{fixed}{quote}"

    return STRING_LITERAL_PATTERN.sub(repl, token), sorted(set(rules))


def apply_token_aware_replacements(text: str) -> tuple[str, list[str]]:
    parts: list[str] = []
    position = 0
    rules: list[str] = []
    for match in TOKEN_PATTERN.finditer(text):
        outside = text[position : match.start()]
        outside, outside_rules = apply_visible_replacements(outside)
        rules.extend(outside_rules)

        token, token_rules = fix_string_literals_in_token(match.group(0))
        rules.extend(token_rules)
        parts.append(outside)
        parts.append(token)
        position = match.end()

    outside = text[position:]
    outside, outside_rules = apply_visible_replacements(outside)
    rules.extend(outside_rules)
    parts.append(outside)
    return "".join(parts), sorted(set(rules))


def apply_mechanical_rules(text: str) -> tuple[str, list[str]]:
    fixed = text
    rules: list[str] = []

    fixed2, count = GENDER_TOKEN_EXTRA_SUFFIX_PATTERN.subn(r"\1", fixed)
    if count:
        fixed = fixed2
        rules.append("remove_gender_token_extra_suffix")

    fixed2, count = GENDER_TOKEN_JOINED_PATTERN.subn(r"\1 ", fixed)
    if count:
        fixed = fixed2
        rules.append("space_after_gender_token")

    fixed2, count = STYLE_TOKEN_JOINED_TO_WORD_PATTERN.subn(r"\1 ", fixed)
    fixed3, count2 = BRACKET_TOKEN_JOINED_TO_WORD_PATTERN.subn(r"\1 ", fixed2)
    if count or count2:
        fixed = fixed3
        rules.append("space_after_token")

    fixed2, count = WORD_JOINED_TO_TOKEN_PATTERN.subn(r" \1", fixed)
    if count:
        fixed = fixed2
        rules.append("space_before_token")

    fixed2, count = SPACE_BEFORE_PUNCTUATION_PATTERN.subn(r"\1", fixed)
    if count:
        fixed = fixed2
        rules.append("remove_space_before_punctuation")

    return fixed, sorted(set(rules))


def token_shape(value: str | None) -> list[str]:
    shapes: list[str] = []
    for token in PROTECTED_TOKEN_PATTERN.findall(value or ""):
        if token.startswith("[") and token.endswith("]"):
            token = STRING_LITERAL_PATTERN.sub("''", token)
        shapes.append(token)
    return shapes


def token_status(source_text: str | None, proposed_text: str | None) -> str:
    if protected_tokens(source_text) == protected_tokens(proposed_text):
        return "ok"
    if token_shape(source_text) == token_shape(proposed_text):
        return "literal_changed"
    return "mismatch"


def load_exact_memory(conn, candidate_rows: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    memory: dict[tuple[str, str], str] = {}
    conflicts: set[tuple[str, str]] = set()
    raw_keys: list[tuple[str, str]] = []
    seen_raw_keys: set[tuple[str, str]] = set()
    for candidate in candidate_rows:
        english_text = candidate.get("english_text") or ""
        spanish_text = candidate.get("spanish_text") or ""
        raw_key = (english_text, spanish_text)
        if raw_key in seen_raw_keys or not english_text or not spanish_text:
            continue
        seen_raw_keys.add(raw_key)
        raw_keys.append(raw_key)
    if not raw_keys:
        return memory

    conn.execute("DROP TABLE IF EXISTS temp.offline_memory_keys")
    conn.execute(
        """
        CREATE TEMP TABLE offline_memory_keys (
            english_text TEXT NOT NULL,
            spanish_text TEXT NOT NULL
        )
        """
    )
    conn.executemany(
        "INSERT INTO offline_memory_keys (english_text, spanish_text) VALUES (?, ?)",
        raw_keys,
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS temp.idx_offline_memory_keys_pair
        ON offline_memory_keys(english_text, spanish_text)
        """
    )

    rows = conn.execute(
        """
        SELECT
            s.english_text,
            s.spanish_text,
            sc.confirmed_text
        FROM offline_memory_keys k
        JOIN source_segments s
          ON s.english_text = k.english_text
         AND s.spanish_text = k.spanish_text
        JOIN segment_confirmations sc ON sc.segment_id = s.id
        WHERE s.is_active = 1
          AND sc.confirmed_text IS NOT NULL
          AND (sc.locked = 1 OR sc.confirmation_level = 'human_confirmed')
        ORDER BY sc.locked DESC, sc.confirmation_level = 'human_confirmed' DESC, sc.updated_at DESC
        """
    ).fetchall()

    for row in rows:
        key = (normalize_key(row["english_text"]), normalize_key(row["spanish_text"]))
        confirmed = row["confirmed_text"]
        existing = memory.get(key)
        if existing is None and key not in conflicts:
            validation = local_quality_validator.validate_text(confirmed)
            if validation["high_issue_count"] == 0:
                memory[key] = confirmed
        elif existing != confirmed:
            memory.pop(key, None)
            conflicts.add(key)
    return memory


def load_glossary_replacements(conn) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT term, portuguese_term
        FROM glossary
        WHERE LENGTH(COALESCE(term, '')) >= 3
          AND LENGTH(COALESCE(portuguese_term, '')) >= 1
        """
    ).fetchall()
    replacements: dict[str, str] = {}
    for row in rows:
        term = row["term"]
        target = row["portuguese_term"]
        if not term or not target:
            continue
        if "_" in term or "$" in term or "[" in term:
            continue
        replacements[term] = target
    return replacements


def apply_glossary(text: str, glossary: dict[str, str]) -> tuple[str, list[str]]:
    fixed = text
    rules: list[str] = []
    for source, target in sorted(glossary.items(), key=lambda item: len(item[0]), reverse=True):
        if len(source.split()) > 4:
            continue
        fixed, count = replace_case_aware(source, target, fixed)
        if count:
            rules.append("glossary_replacement")
    return fixed, sorted(set(rules))


def choose_base_text(row: dict[str, Any]) -> tuple[str, str]:
    if row.get("old_text"):
        return "old_text", row["old_text"]
    if row.get("spanish_text"):
        return "spanish_source", row["spanish_text"]
    return "empty", ""


def visible_spanish_risk_terms(value: str | None) -> list[str]:
    visible = PROTECTED_TOKEN_PATTERN.sub(" ", value or "")
    return sorted({match.group(0) for match in SPANISH_RISK_PATTERN.finditer(visible)})


def translatable_literal_risk_terms(value: str | None) -> list[str]:
    risks: set[str] = set()
    for token_match in TOKEN_PATTERN.finditer(value or ""):
        token = token_match.group(0)
        base_name = command_base_name(token)
        literal_index = 0
        for literal_match in STRING_LITERAL_PATTERN.finditer(token):
            literal_index += 1
            literal = literal_match.group(1) if literal_match.group(1) is not None else literal_match.group(2)
            if literal is None or not is_literal_translatable(base_name, literal_index, literal):
                continue
            for risk_match in TRANSLATABLE_LITERAL_RISK_PATTERN.finditer(literal):
                risks.add(risk_match.group(0))
            for risk_match in SPANISH_RISK_PATTERN.finditer(literal):
                risks.add(risk_match.group(0))
    return sorted(risks)


def is_spanish_ha_interjection_only(value: str | None) -> bool:
    visible = PROTECTED_TOKEN_PATTERN.sub(" ", value or "")
    return re.search(r"(^|[\s\"'“”])Ha!($|[\s\"'“”])", visible) is not None


def is_clean_punctuation_only_proposal(
    rules: list[str],
    validation: dict[str, Any],
    proposed_text: str | None,
    risk_terms: list[str],
) -> bool:
    if sorted(set(rules)) != ["normalize_spanish_punctuation"]:
        return False
    if risk_terms:
        return False
    if int(validation.get("word_count") or 0) >= 80:
        return False
    text = proposed_text or ""
    if text.count('"') % 2:
        return False
    if EMPHASIS_HUMAN_TEXT_PATTERN.search(text):
        return False
    if QUOTE_AFTER_ES_CUSTOM_TOKEN_PATTERN.search(text):
        return False
    for issue in validation["issues"]:
        code = issue["code"]
        if code == "spanish_residue":
            matches = set(issue.get("matches") or [])
            if matches <= PUNCTUATION_ONLY_ALLOWED_RESIDUES and is_spanish_ha_interjection_only(proposed_text):
                continue
        return False
    return True


def issue_codes(validation: dict[str, Any]) -> set[str]:
    return {str(issue.get("code")) for issue in validation.get("issues") or [] if issue.get("code")}


def has_separated_gender_suffix(value: str | None) -> bool:
    return SEPARATED_GENDER_SUFFIX_PATTERN.search(value or "") is not None


def has_punctuation_repair_risk(value: str | None) -> bool:
    return PUNCTUATION_REPAIR_RISK_PATTERN.search(value or "") is not None


def build_proposal(row: dict[str, Any], memory: dict[tuple[str, str], str], glossary: dict[str, str]) -> dict[str, Any]:
    proxy = fast_classify_item(row)
    bucket = classify_bucket(proxy)
    base_source, base_text = choose_base_text(row)
    rules: list[str] = []
    proposal_source = "rules"

    memory_key = (normalize_key(row.get("english_text")), normalize_key(row.get("spanish_text")))
    if memory_key in memory:
        proposed = memory[memory_key]
        proposal_source = "exact_confirmed_memory"
        rules.append("exact_confirmed_memory")
    else:
        proposed, new_rules = apply_mojibake_and_punctuation(base_text)
        rules.extend(new_rules)
        proposed, new_rules = apply_mechanical_rules(proposed)
        rules.extend(new_rules)
        proposed, new_rules = apply_token_aware_replacements(proposed)
        rules.extend(new_rules)
        proposed, new_rules = apply_glossary(proposed, glossary)
        rules.extend(new_rules)

    status_token = token_status(row.get("spanish_text"), proposed)
    validation = local_quality_validator.validate_text(proposed)
    high = int(validation["high_issue_count"])
    medium = int(validation["medium_issue_count"])
    issues = issue_codes(validation)
    risk_terms = visible_spanish_risk_terms(proposed)
    literal_risk_terms = translatable_literal_risk_terms(proposed)
    clean_punctuation_only = is_clean_punctuation_only_proposal(rules, validation, proposed, risk_terms)
    separated_gender_suffix = has_separated_gender_suffix(proposed)
    embedded_gender_token_fragment = bool(
        local_quality_validator.EMBEDDED_GENDER_TOKEN_FRAGMENT_PATTERN.search(proposed)
        or local_quality_validator.BROKEN_PORTUGUESE_GENDER_TOKEN_WORD_PATTERN.search(proposed)
    )
    punctuation_repair_risk = has_punctuation_repair_risk(proposed)
    changed = proposed != base_text
    rule_set = set(rules)
    clean_composite_rules = (
        changed
        and high == 0
        and medium == 0
        and not risk_terms
        and not literal_risk_terms
        and not separated_gender_suffix
        and not punctuation_repair_risk
        and status_token in {"ok", "literal_changed"}
        and bool(
            rule_set
            - {
                "fix_mojibake",
                "normalize_spanish_punctuation",
                "remove_space_before_punctuation",
                "space_after_token",
                "space_before_token",
                "remove_gender_token_extra_suffix",
                "space_after_gender_token",
            }
        )
    )
    reasons: list[str] = []

    if not base_text:
        status = "rejected"
        confidence = 0.0
        reasons.append("empty_base_text")
    elif status_token == "mismatch":
        status = "rejected"
        confidence = 0.15
        reasons.append("token_structure_mismatch")
    elif not changed and proposal_source != "exact_confirmed_memory":
        status = "rejected"
        confidence = 0.25
        reasons.append("no_change")
    elif separated_gender_suffix:
        status = "needs_review"
        confidence = 0.60
        reasons.append("gender_token_suffix_separated_requires_review")
    elif embedded_gender_token_fragment:
        status = "needs_review"
        confidence = 0.50
        reasons.append("embedded_gender_token_fragment_requires_review")
    elif punctuation_repair_risk:
        status = "needs_review"
        confidence = 0.78
        reasons.append("punctuation_repair_risk_requires_review")
    elif "spanish_residue_in_literal" in issues:
        status = "needs_review"
        confidence = 0.62
        reasons.append("inline_literal_residue_requires_review")
    elif literal_risk_terms:
        status = "needs_review"
        confidence = 0.62
        reasons.append("translatable_literal_risk_terms")
    elif high:
        status = "needs_review"
        confidence = 0.62
        reasons.append("remaining_high_issues")
    elif medium:
        status = "needs_review"
        confidence = 0.78
        reasons.append("remaining_medium_issues")
    elif clean_punctuation_only:
        status = "auto_ready"
        confidence = 0.95
        reasons.append("punctuation_only_clean")
    elif clean_composite_rules:
        status = "auto_ready"
        confidence = 0.965 if status_token == "ok" else 0.94
        reasons.append("composite_rules_clean")
    elif "normalize_spanish_punctuation" in rules:
        status = "needs_review"
        confidence = 0.80
        reasons.append("punctuation_change_requires_review")
    elif validation["word_count"] >= 80 and proposal_source != "exact_confirmed_memory":
        status = "needs_review"
        confidence = 0.82
        reasons.append("long_text_requires_review")
    elif risk_terms:
        status = "needs_review"
        confidence = 0.76
        reasons.append("visible_spanish_risk_terms")
    elif rules == ["normalize_spanish_punctuation"] and validation["word_count"] > 12:
        status = "needs_review"
        confidence = 0.80
        reasons.append("punctuation_only_long_text_requires_review")
    elif status_token == "literal_changed":
        status = "auto_ready"
        confidence = 0.93
        reasons.append("literal_only_structure_change")
    elif proposal_source == "exact_confirmed_memory":
        status = "auto_ready"
        confidence = 0.997
        reasons.append("confirmed_exact_reuse")
    else:
        status = "auto_ready"
        confidence = 0.94
        reasons.append("validator_clean")

    if bucket in {"translation_required", "residual_spanish_long_text"} and proposal_source != "exact_confirmed_memory":
        if status == "auto_ready":
            status = "needs_review"
            confidence = min(confidence, 0.86)
            reasons.append("semantic_translation_not_proven_offline")

    return {
        "segment_id": row["segment_id"],
        "relative_path": row["relative_path"],
        "source_key": row["source_key"],
        "source_line_number": row["source_line_number"],
        "candidate_bucket": bucket,
        "proposal_source": proposal_source if proposal_source != "rules" else "+".join(rules) or "rules",
        "original_text": base_text,
        "proposed_text": proposed,
        "confidence_score": confidence,
        "status": status,
        "token_status": status_token,
        "issue_count": int(validation["issue_count"]),
        "high_issue_count": high,
        "medium_issue_count": medium,
        "rules": sorted(set(rules)),
        "reasons": reasons,
        "issues": validation["issues"],
    }


def fetch_candidate_rows(conn, limit: int | None, path_like: str | None) -> list[dict[str, Any]]:
    rows = fetch_pending_rows(conn, None, path_like)
    candidates: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        item = fast_classify_item(row)
        bucket = classify_bucket(item)
        if bucket in TARGET_BUCKETS:
            candidates.append(row)
            if limit and len(candidates) >= limit:
                break
        if index % 10000 == 0:
            print(f"[offline_residual_proposals] Scanned pending rows: {index}; candidates: {len(candidates)}")
    return candidates


def create_run(conn, path_like: str | None, limit: int | None, started_at: datetime) -> int:
    cursor = conn.execute(
        """
        INSERT INTO offline_proposal_runs (
            rule_version, model_version, path_filter, limit_count,
            started_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (RULE_VERSION, MODEL_VERSION, path_like, limit, started_at.isoformat(timespec="seconds"), now()),
    )
    return int(cursor.lastrowid)


def save_proposals(conn, run_id: int, proposals: list[dict[str, Any]]) -> None:
    timestamp = now()
    conn.executemany(
        """
        INSERT INTO offline_proposals (
            run_id, segment_id, relative_path, source_key, source_line_number,
            candidate_bucket, proposal_source, original_text, proposed_text,
            confidence_score, status, token_status, issue_count, high_issue_count,
            medium_issue_count, rules_json, reasons_json, issues_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                item["segment_id"],
                item["relative_path"],
                item["source_key"],
                item["source_line_number"],
                item["candidate_bucket"],
                item["proposal_source"],
                item["original_text"],
                item["proposed_text"],
                item["confidence_score"],
                item["status"],
                item["token_status"],
                item["issue_count"],
                item["high_issue_count"],
                item["medium_issue_count"],
                json.dumps(item["rules"], ensure_ascii=False),
                json.dumps(item["reasons"], ensure_ascii=False),
                json.dumps(item["issues"], ensure_ascii=False),
                timestamp,
                timestamp,
            )
            for item in proposals
        ],
    )


def update_run(conn, run_id: int, candidate_count: int, proposals: list[dict[str, Any]], started_at: datetime) -> None:
    counts = Counter(item["status"] for item in proposals)
    conn.execute(
        """
        UPDATE offline_proposal_runs
        SET candidate_count = ?,
            proposed_count = ?,
            auto_ready_count = ?,
            needs_review_count = ?,
            rejected_count = ?,
            finished_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            candidate_count,
            len(proposals),
            counts.get("auto_ready", 0),
            counts.get("needs_review", 0),
            counts.get("rejected", 0),
            datetime.now().isoformat(timespec="seconds"),
            now(),
            run_id,
        ),
    )


def build_report(
    started_at: datetime,
    run_id: int,
    candidates: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    path_like: str | None,
    limit: int | None,
) -> list[str]:
    elapsed = datetime.now() - started_at
    status_counts = Counter(item["status"] for item in proposals)
    bucket_counts = Counter(item["candidate_bucket"] for item in proposals)
    token_counts = Counter(item["token_status"] for item in proposals)
    rule_counts = Counter(rule for item in proposals for rule in item["rules"])
    reason_counts = Counter(reason for item in proposals for reason in item["reasons"])
    by_package = defaultdict(Counter)
    for item in proposals:
        by_package[item["relative_path"]][item["status"]] += 1

    lines = [
        "Offline residual proposals report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Run id: {run_id}",
        f"Rule version: {RULE_VERSION}",
        f"Model version: {MODEL_VERSION}",
        f"Path filter: {path_like or 'none'}",
        f"Limit: {limit or 'none'}",
        "",
        "Summary:",
        f"- Candidate rows selected: {len(candidates)}",
        f"- Proposals stored: {len(proposals)}",
        f"- Auto-ready: {status_counts.get('auto_ready', 0)}",
        f"- Needs review: {status_counts.get('needs_review', 0)}",
        f"- Rejected/no useful proposal: {status_counts.get('rejected', 0)}",
        "",
        "Buckets:",
        *[f"- {key}: {count}" for key, count in bucket_counts.most_common()],
        "",
        "Token status:",
        *[f"- {key}: {count}" for key, count in token_counts.most_common()],
        "",
        "Rules used:",
        *[f"- {key}: {count}" for key, count in rule_counts.most_common(30)],
        "",
        "Reasons:",
        *[f"- {key}: {count}" for key, count in reason_counts.most_common(30)],
        "",
        "Package impact:",
    ]
    for path, counts in sorted(by_package.items(), key=lambda item: (-sum(item[1].values()), item[0]))[:40]:
        detail = ", ".join(f"{status}:{count}" for status, count in counts.most_common())
        lines.append(f"- {path}: {sum(counts.values())} ({detail})")

    lines.extend(["", "Auto-ready examples:"])
    for item in [p for p in proposals if p["status"] == "auto_ready"][:30]:
        lines.append(
            f"- #{item['segment_id']} | {item['confidence_score']:.3f} | {item['token_status']} | "
            f"{item['relative_path']}::{item['source_key']}"
        )
        lines.append(f"  before: {sample_text(item['original_text'])}")
        lines.append(f"  after:  {sample_text(item['proposed_text'])}")

    lines.extend(["", "Needs-review examples:"])
    for item in [p for p in proposals if p["status"] == "needs_review"][:30]:
        lines.append(
            f"- #{item['segment_id']} | {item['confidence_score']:.3f} | {','.join(item['reasons'])} | "
            f"{item['relative_path']}::{item['source_key']}"
        )
        lines.append(f"  before: {sample_text(item['original_text'])}")
        lines.append(f"  after:  {sample_text(item['proposed_text'])}")

    lines.extend(["", "Rejected examples:"])
    for item in [p for p in proposals if p["status"] == "rejected"][:20]:
        lines.append(
            f"- #{item['segment_id']} | {','.join(item['reasons'])} | "
            f"{item['relative_path']}::{item['source_key']}"
        )
    return lines


def main(limit: int | None = None, path_like: str | None = None) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[offline_residual_proposals] Starting offline proposal generation")
    print(f"[offline_residual_proposals] Rule version: {RULE_VERSION}")
    print(f"[offline_residual_proposals] Model version: {MODEL_VERSION}")
    print(f"[offline_residual_proposals] Limit: {limit or 'none'}")
    print(f"[offline_residual_proposals] Path filter: {path_like or 'none'}")
    print(f"[offline_residual_proposals] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        run_id = create_run(conn, path_like, limit, started_at)
        print(f"[offline_residual_proposals] Run id: {run_id}")
        candidates = fetch_candidate_rows(conn, limit, path_like)
        print(f"[offline_residual_proposals] Candidate rows selected: {len(candidates)}")
        memory = load_exact_memory(conn, candidates)
        glossary = load_glossary_replacements(conn)
        print(f"[offline_residual_proposals] Exact confirmed memory keys: {len(memory)}")
        print(f"[offline_residual_proposals] Glossary terms: {len(glossary)}")

        proposals: list[dict[str, Any]] = []
        for index, row in enumerate(candidates, start=1):
            proposals.append(build_proposal(row, memory, glossary))
            if index % 1000 == 0:
                print(f"[offline_residual_proposals] Proposed: {index}/{len(candidates)}")

        save_proposals(conn, run_id, proposals)
        update_run(conn, run_id, len(candidates), proposals, started_at)
        conn.commit()

    status_counts = Counter(item["status"] for item in proposals)
    report_lines = build_report(started_at, run_id, candidates, proposals, path_like, limit)
    report_path = db.write_report(settings, "offline_residual_proposals", report_lines)

    print(f"[offline_residual_proposals] Proposals stored: {len(proposals)}")
    print(f"[offline_residual_proposals] Auto-ready: {status_counts.get('auto_ready', 0)}")
    print(f"[offline_residual_proposals] Needs review: {status_counts.get('needs_review', 0)}")
    print(f"[offline_residual_proposals] Rejected/no useful proposal: {status_counts.get('rejected', 0)}")
    print(f"[offline_residual_proposals] Report: {report_path}")
    print("[offline_residual_proposals] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate offline proposals for unresolved CK3 localization residues.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum candidate rows to propose.")
    parser.add_argument("--path-like", default=None, help="Optional SQL LIKE filter for relative_path.")
    args = parser.parse_args()
    main(limit=args.limit, path_like=args.path_like)
