from __future__ import annotations

import argparse
import re
from collections import Counter
from datetime import datetime

import db
import local_quality_validator
from apply_safe_output_updates import protected_tokens


RULE_VERSION = "apply_inline_literal_fixes_v1"
AUTO_SCORE = 0.993

TOKEN_PATTERN = re.compile(r"\[[^\]]+\]")
STRING_LITERAL_PATTERN = re.compile(r"'([^']*)'|\"([^\"]*)\"")
WORD_CHARS = r"A-Za-zÀ-ÿ"

INLINE_LITERAL_COMMANDS = {
    "Select_CString",
    "LocalPlayerString",
    "PlayerString",
    "GetString",
}

SAFE_LITERAL_REPLACEMENTS = {
    "Lealtad": "Lealdade",
    "lealtad": "lealdade",
    "decision": "decisão",
    "decisiones": "decisões",
    "situacion": "situação",
    "situación": "situação",
    "situaciones": "situações",
    "cortesano": "cortesão",
    "cortesanos": "cortesãos",
    "cortesana": "cortesã",
    "cortesanas": "cortesãs",
    "invitado": "convidado",
    "invitados": "convidados",
    "gobernante": "governante",
    "gobernantes": "governantes",
    "hacendado": "com terras",
    "hacendados": "com terras",
    "independiente": "independente",
    "independientes": "independentes",
    "con tierras": "com terras",
    "tiene tierras": "tem terras",
    "se metía": "intimidava",
    "se mete": "intimida",
    "flechazo": "paixão",
    "ti": "você",
    "tu persona": "você",
    "tu personaje": "seu personagem",
    "tu señorío": "seu senhorio",
    "tu casa": "sua casa",
    "tu mano": "sua mão",
    "tu arco": "seu arco",
    "tu corte": "sua corte",
    "tus perreras": "seus canis",
    "tu hueste": "sua hoste",
    "unidad de tu casa": "unidade da sua casa",
    "tamaño de tu señorío": "tamanho do seu senhorio",
    "capital de tu señorío": "capital do seu senhorio",
    "jefe de tu casa": "chefe da sua casa",
    "líder de tu movimiento": "líder do seu movimento",
    "idioma de tu corte": "idioma da sua corte",
    "hereder": "herdeir",
    "familiar cercano": "familiar próximo",
    "tamaño": "tamanho",
    "jefa": "chefe",
    "jefe": "chefe",
    "decidiste": "decidiu",
    "decidió": "decidiu",
    "decidieron": "decidiram",
    "viste": "viu",
    "vio": "viu",
    "enseñaste": "ensinou",
    "enseñó": "ensinou",
    "tiene": "tem",
    "tengas": "tenha",
    "tenga": "tenha",
    "serás": "será",
    "será": "será",
    "convenciste": "convenceu",
    "convenció": "convenceu",
    "abandonaste": "abandonou",
    "abandonó": "abandonou",
    "disfrutaste": "desfrutou",
    "disfrutó": "desfrutou",
    "eliminaste": "eliminou",
    "eliminó": "eliminou",
    "saqueaste": "saqueou",
    "saqueó": "saqueou",
    "deshonraste": "desonrou",
    "deshonró": "desonrou",
    "congeniaste": "se deu bem",
    "congenió": "se deu bem",
    "robaste": "roubou",
    "robó": "roubou",
    "resultaste": "ficou",
    "resultó": "ficou",
    "prometiste": "prometeu",
    "prometió": "prometeu",
    "participasteis": "participaram",
    "participaron": "participaram",
    "pasaste": "passou",
    "pasó": "passou",
    "conseguiste": "conseguiu",
    "consiguió": "conseguiu",
    "vislumbraste": "vislumbrou",
    "vislumbró": "vislumbrou",
    "tienes": "tem",
    "eras": "era",
    "reducida": "reduzida",
    "odio": "ódio",
    "amigo imaginario": "amigo imaginário",
    "amiga imaginaria": "amiga imaginária",
    "Esta cría": "Esta menina",
    "Este crío": "Este menino",
    "cría": "menina",
    "crío": "menino",
    "un anfitrión apreciado": "um anfitrião estimado",
    "una anfitriona apreciada": "uma anfitriã estimada",
    "nuestra apreciada invitada": "nossa convidada estimada",
    "nuestro apreciado invitado": "nosso convidado estimado",
    "el cónyuge": "o cônjuge",
    "la cónyuge": "a cônjuge",
    "el casamentero": "o casamenteiro",
    "la casamentera": "a casamenteira",
    "vasall": "vassalo",
    "vasalla": "vassala",
    "vasallo": "vassalo",
    "vasalla mía": "minha vassala",
    "vasallo mío": "meu vassalo",
}

SAFE_TEXT_REPLACEMENTS = {
    "#bold no#!": "#bold não#!",
    "#bold No#!": "#bold Não#!",
}

REDUNDANT_LITERAL_WORDS = {
    "atrapalhou",
    "conseguiu",
    "decidiu",
    "decidiram",
    "ensinou",
    "eliminou",
    "ficou",
    "participaram",
    "passou",
    "prometeu",
}

FOCUS_TERM_GROUPS = {
    "common": (
        "tu personaje",
        "Tu personaje",
        "tu persona",
        "tu señorío",
        "tu corte",
        "de ti",
        "hacendado",
        "hacendados",
        "cortesana",
        "cortesano",
        "cortesanos",
        "situacion",
        "situación",
        "situaciones",
        "decision",
        "decisiones",
        "invitado",
        "invitados",
        "gobernante",
        "gobernantes",
        "tamaño",
        "jefe",
        "hereder",
        "eras",
        "familiar cercano",
        "tiene tierras",
        "vasalla",
        "vasallo",
        "#bold no#!",
        "#bold No#!",
    ),
    "pronouns": (
        "tu personaje",
        "Tu personaje",
        "tu persona",
        "tu señorío",
        "tu corte",
        "tu casa",
        "tu mano",
        "tu arco",
        "tu hueste",
        "de ti",
    ),
    "concepts": (
        "hacendado",
        "hacendados",
        "cortesana",
        "cortesano",
        "cortesanos",
        "situacion",
        "situación",
        "situaciones",
        "decision",
        "decisiones",
        "invitado",
        "invitados",
        "gobernante",
        "gobernantes",
        "tamaño",
        "jefe",
        "familiar cercano",
        "tiene tierras",
        "vasalla",
        "vasallo",
    ),
}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def command_base_name(token: str) -> str:
    command_name = token[1:].split("(", 1)[0].split("|", 1)[0].strip()
    return command_name.split(".")[-1]


def should_translate_literal(base_name: str, literal_index: int, literal: str) -> bool:
    if local_quality_validator.is_technical_literal(literal):
        return False
    if base_name == "Concept":
        return literal_index >= 2
    if (
        base_name in INLINE_LITERAL_COMMANDS
        or base_name.startswith("SelectLocalization")
        or base_name.endswith("String")
    ):
        return True
    return False


def replace_literal_text(literal: str) -> tuple[str, int]:
    result = literal
    changes = 0
    for source, target in sorted(SAFE_LITERAL_REPLACEMENTS.items(), key=lambda item: len(item[0]), reverse=True):
        pattern = re.compile(rf"(?<![{WORD_CHARS}]){re.escape(source)}(?![{WORD_CHARS}])", re.IGNORECASE)
        def replace_match(match: re.Match) -> str:
            matched_text = match.group(0)
            if matched_text[:1].isupper() and target[:1].islower():
                return target[:1].upper() + target[1:]
            return target

        result, count = pattern.subn(replace_match, result)
        changes += count
    return result, changes


def fix_token(token: str) -> tuple[str, int]:
    base_name = command_base_name(token)
    literal_index = 0
    changes = 0

    def replace_match(match: re.Match) -> str:
        nonlocal literal_index, changes
        literal_index += 1
        quote = "'" if match.group(1) is not None else '"'
        literal = match.group(1) if match.group(1) is not None else match.group(2)
        if not should_translate_literal(base_name, literal_index, literal):
            return match.group(0)
        fixed, count = replace_literal_text(literal)
        if not count:
            return match.group(0)
        changes += count
        return f"{quote}{fixed}{quote}"

    return STRING_LITERAL_PATTERN.sub(replace_match, token), changes


def fix_inline_literals(text: str) -> tuple[str, int]:
    changes = 0
    fixed_text = text
    for source, target in SAFE_TEXT_REPLACEMENTS.items():
        count = fixed_text.count(source)
        fixed_text = fixed_text.replace(source, target)
        changes += count

    def replace_token(match: re.Match) -> str:
        nonlocal changes
        fixed, count = fix_token(match.group(0))
        changes += count
        return fixed

    return TOKEN_PATTERN.sub(replace_token, fixed_text), changes


def has_redundant_literal_after_token(text: str) -> bool:
    for word in REDUNDANT_LITERAL_WORDS:
        pattern = re.compile(
            rf"\[[^\]]*(?:Select_CString|LocalPlayerString|PlayerString)[^\]]*['\"]{re.escape(word)}['\"][^\]]*\]\s+{re.escape(word)}\b",
            re.IGNORECASE,
        )
        if pattern.search(text):
            return True
    awkward_patterns = (
        r"\[[^\]]*(?:Select_CString|LocalPlayerString|PlayerString)[^\]]*['\"]conseguiu['\"][^\]]*\]\s+matou\b",
        r"\[[^\]]*(?:Select_CString|LocalPlayerString|PlayerString)[^\]]*['\"]ensinou['\"][^\]]*\]\s+ensinou\b",
        r"\[[^\]]*(?:Select_CString|LocalPlayerString|PlayerString)[^\]]*['\"]desfrutou['\"][^\]]*\]\s+assistiu\b",
        r"\[[^\]]*(?:Select_CString|LocalPlayerString|PlayerString)[^\]]*['\"]desonrou['\"][^\]]*\]\s+ao\b",
    )
    for pattern_text in awkward_patterns:
        if re.search(pattern_text, text, re.IGNORECASE):
            return True
    if re.search(r"\]\s+[ao],", text, re.IGNORECASE):
        return True
    return False


def build_search_terms(focus: str | None, extra_terms: list[str] | None) -> list[str]:
    terms: list[str] = []
    if focus:
        terms.extend(FOCUS_TERM_GROUPS[focus])
    if extra_terms:
        terms.extend(term for term in extra_terms if term)
    if not terms:
        terms.extend(SAFE_LITERAL_REPLACEMENTS)
        terms.extend(SAFE_TEXT_REPLACEMENTS)
    return sorted(set(terms))


def fetch_candidates(
    conn,
    limit: int | None,
    path_like: str | None,
    offset: int = 0,
    search_terms: list[str] | None = None,
) -> list[dict]:
    params: list[object] = []
    path_sql = ""
    literal_terms = search_terms or sorted(SAFE_LITERAL_REPLACEMENTS)
    literal_sql = " OR ".join(
        "COALESCE(NULLIF(s.old_text, ''), s.spanish_text, '') LIKE ?"
        for _ in literal_terms
    )
    params.extend(f"%{term}%" for term in literal_terms)
    if path_like:
        path_sql = "AND s.relative_path LIKE ?"
        params.append(path_like)
    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT ?"
        params.append(limit)
    offset_sql = ""
    if offset:
        offset_sql = "OFFSET ?"
        if limit is None:
            limit_sql = "LIMIT -1"
        params.append(offset)

    rows = conn.execute(
        f"""
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.spanish_text,
            s.english_text,
            COALESCE(NULLIF(s.old_text, ''), s.spanish_text, '') AS candidate_text
        FROM source_segments s
        LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
        WHERE s.is_active = 1
          AND sc.segment_id IS NULL
          AND COALESCE(NULLIF(s.old_text, ''), s.spanish_text, '') LIKE '%[%''%'
          AND ({literal_sql})
          {path_sql}
        ORDER BY s.id ASC
        {limit_sql}
        {offset_sql}
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def validate_fix(item: dict, fixed_text: str, before_changes: int) -> tuple[bool, dict, list[str]]:
    reasons: list[str] = []
    before = item["candidate_text"] or ""
    after = local_quality_validator.validate_text(fixed_text)
    before_validation = local_quality_validator.validate_text(before)
    if before_changes == 0 or fixed_text == before:
        reasons.append("no_change")
        return False, after, reasons
    if protected_tokens(before) != protected_tokens(fixed_text):
        reasons.append("protected_structure_mismatch")
        return False, after, reasons
    if has_redundant_literal_after_token(fixed_text):
        reasons.append("redundant_literal_after_token")
        return False, after, reasons
    if after["high_issue_count"] > 0 or after["medium_issue_count"] > 0:
        reasons.append("remaining_validator_issues")
        return False, after, reasons
    if after["issue_count"] >= before_validation["issue_count"]:
        reasons.append("no_issue_improvement")
        return False, after, reasons
    reasons.append("validator_clean")
    return True, after, reasons


def apply_confirmations(conn, accepted: list[dict]) -> None:
    timestamp = now()
    conn.executemany(
        """
        INSERT INTO segment_confirmations (
            segment_id, confirmation_level, confirmed_text, confirmation_source,
            confirmation_label, locked, confidence_score, reviewer, confirmed_at, updated_at
        )
        VALUES (?, 'auto_confirmed', ?, 'inline_literal_fixes', 'inline_literal_fix', 0, ?, 'inline_literal_fixes', ?, ?)
        ON CONFLICT(segment_id) DO UPDATE SET
            confirmation_level = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmation_level
                ELSE 'auto_confirmed'
            END,
            confirmed_text = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmed_text
                ELSE excluded.confirmed_text
            END,
            confirmation_source = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmation_source
                ELSE excluded.confirmation_source
            END,
            confirmation_label = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmation_label
                ELSE excluded.confirmation_label
            END,
            confidence_score = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confidence_score
                ELSE excluded.confidence_score
            END,
            reviewer = CASE
                WHEN segment_confirmations.locked = 1 THEN segment_confirmations.reviewer
                ELSE excluded.reviewer
            END,
            updated_at = ?
        """,
        [
            (
                item["segment_id"],
                item["fixed_text"],
                AUTO_SCORE,
                timestamp,
                timestamp,
                timestamp,
            )
            for item in accepted
        ],
    )


def main(
    limit: int | None = None,
    path_like: str | None = None,
    offset: int = 0,
    focus: str | None = None,
    terms: list[str] | None = None,
    apply: bool = False,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    search_terms = build_search_terms(focus, terms)
    print("[apply_inline_literal_fixes] Starting inline literal fixes")
    print(f"[apply_inline_literal_fixes] Rule version: {RULE_VERSION}")
    print(f"[apply_inline_literal_fixes] Apply: {apply}")
    print(f"[apply_inline_literal_fixes] Limit: {limit or 'none'}")
    print(f"[apply_inline_literal_fixes] Offset: {offset}")
    print(f"[apply_inline_literal_fixes] Focus: {focus or 'none'}")
    print(f"[apply_inline_literal_fixes] Search terms: {len(search_terms)}")
    print(f"[apply_inline_literal_fixes] Path filter: {path_like or 'none'}")
    print(f"[apply_inline_literal_fixes] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        rows = fetch_candidates(conn, limit, path_like, offset, search_terms)
        accepted: list[dict] = []
        skipped: Counter[str] = Counter()
        for item in rows:
            fixed_text, changes = fix_inline_literals(item["candidate_text"] or "")
            ok, after, reasons = validate_fix(item, fixed_text, changes)
            if ok:
                item["fixed_text"] = fixed_text
                item["after"] = after
                item["changes"] = changes
                accepted.append(item)
            else:
                skipped.update(reasons)
        if apply:
            apply_confirmations(conn, accepted)
            conn.commit()

    elapsed = datetime.now() - started_at
    lines = [
        "Apply inline literal fixes report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Apply: {apply}",
        f"Limit: {limit or 'none'}",
        f"Offset: {offset}",
        f"Focus: {focus or 'none'}",
        f"Search terms: {', '.join(search_terms[:40])}{' ...' if len(search_terms) > 40 else ''}",
        f"Path filter: {path_like or 'none'}",
        "",
        "Summary:",
        f"- Candidates inspected: {len(rows)}",
        f"- Auto-confirmable fixes: {len(accepted)}",
        f"- Applied confirmations: {len(accepted) if apply else 0}",
        "",
        "Skipped:",
        *[f"- {reason}: {count}" for reason, count in skipped.most_common()],
        "",
        "Preview:",
    ]
    for item in accepted[:60]:
        before = (item["candidate_text"] or "").replace("\n", "\\n")
        after = item["fixed_text"].replace("\n", "\\n")
        if len(before) > 220:
            before = before[:220] + "..."
        if len(after) > 220:
            after = after[:220] + "..."
        lines.extend(
            [
                f"- segment {item['segment_id']} | changes:{item['changes']} | {item['relative_path']}::{item['source_key']}",
                f"  before: {before}",
                f"  after:  {after}",
            ]
        )
    if not accepted:
        lines.append("- No auto-confirmable fixes")
    report_path = db.write_report(settings, "apply_inline_literal_fixes", lines)
    print(f"[apply_inline_literal_fixes] Candidates inspected: {len(rows)}")
    print(f"[apply_inline_literal_fixes] Auto-confirmable fixes: {len(accepted)}")
    print(f"[apply_inline_literal_fixes] Applied confirmations: {len(accepted) if apply else 0}")
    print(f"[apply_inline_literal_fixes] Report: {report_path}")
    print("[apply_inline_literal_fixes] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fix safe translatable literals inside CK3 localization tokens.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum unconfirmed rows to inspect.")
    parser.add_argument("--offset", type=int, default=0, help="Skip this many unconfirmed candidate rows.")
    parser.add_argument("--path-like", default=None, help="Optional SQL LIKE filter for relative_path.")
    parser.add_argument("--focus", choices=sorted(FOCUS_TERM_GROUPS), default=None, help="Use a focused term group.")
    parser.add_argument("--term", action="append", default=None, help="Extra search term to prioritize. Can be used more than once.")
    parser.add_argument("--apply", action="store_true", help="Write auto_confirmed rows. Default is dry-run.")
    args = parser.parse_args()
    main(limit=args.limit, path_like=args.path_like, offset=args.offset, focus=args.focus, terms=args.term, apply=args.apply)
