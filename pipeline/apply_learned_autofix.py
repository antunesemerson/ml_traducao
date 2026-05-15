from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime

import db
import local_quality_validator
from apply_safe_output_updates import protected_tokens


RULE_VERSION = "apply_learned_autofix_v3"
AUTO_SCORE = 0.992
FRAGILE_PRONOUN_TOKENS = (
    "GetHerHim",
    "GetHerHis",
    "GetSheHe",
    "GetHerselfHimself",
    "GetHersHis",
)

INVERTED_PUNCTUATION = str.maketrans({"¿": "", "¡": ""})
ANGLED_QUOTE_REPLACEMENTS = {
    "«": '"',
    "»": '"',
}
GENDER_TOKEN_EXTRA_SUFFIX_PATTERN = re.compile(
    r"(\[[^\]]*Custom\(\s*['\"]ES_(?:OA|AO)['\"]\s*\)\])([ao])\b",
    re.IGNORECASE,
)
GENDER_TOKEN_JOINED_PATTERN = re.compile(
    r"(\[[^\]]*Custom\(\s*['\"]ES_(?:LeLa|LoLa|DelDela)['\"]\s*\)\])(?=[A-Za-zÀ-ÿ])",
    re.IGNORECASE,
)
TOKEN_JOINED_TO_WORD_PATTERN = re.compile(r"(\]|\$[A-Za-z0-9_]+\$|#!)(?=[A-Za-zÀ-ÿ])")

SAFE_LITERAL_REPLACEMENTS = {
    "cortesano": "cortesão",
    "cortesanos": "cortesãos",
    "situacion": "situação",
    "situación": "situação",
    "situaciones": "situações",
    "decision": "decisão",
    "decisiones": "decisões",
    "deudas": "dívidas",
    "en la cárcel": "na prisão",
    "nota en el examen": "nota no exame",
    "con tierras": "com terras",
    "trueque": "escambo",
    "tu persona": "você",
    "tu personaje": "seu personagem",
    "tu señor": "seu senhor",
    "tu señorío": "seu senhorio",
    "tuyo/a": "seu/sua",
    "tuyo": "seu",
    "Tu honor": "Sua honra",
    "El honor de": "A honra de",
    "Tus": "Seus",
    "Los": "Os",
    "eres": "é",
    "es": "é",
    "yo": "eu",
    "tan": "tão",
    "eso": "isso",
    "actualmente": "atualmente",
    "alguien": "alguém",
    "de verdad": "de verdade",
    "pertenece": "pertence",
    "vecina": "moradora",
    "vecinas": "moradoras",
    "vecino": "morador",
    "vecinos": "moradores",
    "vitriol": "vitríolo",
    "Cathay": "Catai",
    "Catay": "Catai",
    "inmensamente": "imensamente",
    "ligeramente": "ligeiramente",
    "mínimamente": "minimamente",
    "minimamente": "minimamente",
    "muy": "muito",
    "mi postre favorito": "minha sobremesa favorita",
    "pequeño": "pequeno",
    "pequeña": "pequena",
    "extraordinaria": "extraordinária",
}

# Canonical UTF-8 replacements. Some older literals above may appear mojibaked
# when read through legacy Windows code pages, so keep the runtime map explicit.
INVERTED_PUNCTUATION = str.maketrans({"¿": "", "¡": ""})
MOJIBAKE_INVERTED_PUNCTUATION = ("Â¿", "Â¡")
ANGLED_QUOTE_REPLACEMENTS = {
    "«": '"',
    "»": '"',
    "Â«": '"',
    "Â»": '"',
}
SAFE_LITERAL_REPLACEMENTS = {
    "cortesano": "cortesão",
    "cortesanos": "cortesãos",
    "situacion": "situação",
    "situación": "situação",
    "situaciones": "situações",
    "decision": "decisão",
    "decisiones": "decisões",
    "Coronación exigua": "Coroação simples",
    "Coronación modesta": "Coroação modesta",
    "Coronación respetable": "Coroação respeitável",
    "Coronación resplandeciente": "Coroação resplandecente",
    "coronación": "coroação",
    "Gran coronación": "Grande coroação",
    "deudas": "dívidas",
    "en la cárcel": "na prisão",
    "nota en el examen": "nota no exame",
    "con tierras": "com terras",
    "trueque": "escambo",
    "tu persona": "você",
    "yo": "eu",
    "tan": "tão",
    "eso": "isso",
    "actualmente": "atualmente",
    "alguien": "alguém",
    "de verdad": "de verdade",
    "pertenece": "pertence",
    "vecina": "moradora",
    "vecinas": "moradoras",
    "vecino": "morador",
    "vecinos": "moradores",
    "vitriol": "vitríolo",
    "Cathay": "Catai",
    "Catay": "Catai",
    "inmensamente": "imensamente",
    "ligeramente": "ligeiramente",
    "mínimamente": "minimamente",
    "minimamente": "minimamente",
    "muy": "muito",
    "mi postre favorito": "minha sobremesa favorita",
    "pequeño": "pequeno",
    "pequeña": "pequena",
    "extraordinaria": "extraordinária",
    "por doquier": "por toda parte",
    "plañido": "assobio",
    "cautivadora": "cativante",
    "cautivador": "cativante",
    "cautiva": "cativa",
    "cautivo": "cativo",
    "excautiva": "ex-refém",
    "excautivo": "ex-refém",
    "atravesar": "atravessar",
    "consumarse": "consumar-se",
    "supposed": "deveria",
    "Una narradora": "Uma narradora",
    "Un narrador": "Um narrador",
    "una marinera anciana parlanchina": "uma marinheira idosa falante",
    "un marinero anciano parlanchín": "um marinheiro idoso falante",
    "una marinera anciana": "uma marinheira idosa",
    "un marinero anciano": "um marinheiro idoso",
    "una marinera": "uma marinheira",
    "un marinero": "um marinheiro",
    "esta mujer": "esta mulher",
    "este hombre": "este homem",
    "ninguna campesina": "nenhuma camponesa",
    "ningún campesino": "nenhum camponês",
    "una poetisa": "uma poetisa",
    "un poeta": "um poeta",
    "Los artistas básicos en cualquier corte respetable.": "Artistas básicos em qualquer corte respeitável.",
    "LO SABÍA": "EU SABIA",
    "malnacid": "maldit",
    "una asesina": "uma assassina",
    "un asesino": "um assassino",
    "Ganancia de": "Ganho de",
    "según el número de": "conforme o número de",
    "presentes en tu": "presentes em sua",
    "reduce a la mitad el coste del": "reduz pela metade o custo do",
    "cualquier tipo de comandante": "qualquer tipo de comandante",
    "Como [minister|El], puedes reclutar a": "Como [minister|El], você pode recrutar",
    "probabilidad equivalente a un sexto de tu": "probabilidade equivalente a um sexto de sua",
    "solo se puede ganar oro una vez por posesión": "só é possível ganhar ouro uma vez por posse",
    "Puede que le pillen": "Podem pegá-lo",
    "Hay que pararle los pies": "É preciso detê-lo",
    "Unas cuantas muertes en la naturaleza a leguas de aquí, y hace cuánto?": "Algumas mortes na natureza a léguas daqui, e há quanto tempo?",
}
STYLE_TOKEN_JOINED_TO_WORD_PATTERN = re.compile(r"(#!|\$[A-Za-z0-9_]+\$)(?=[^\W\d_])")
BRACKET_TOKEN_JOINED_TO_WORD_PATTERN = re.compile(
    r"(\](?![aos]\b|as\b|os\b))(?=[^\W\d_])",
    re.IGNORECASE,
)
CAUTIV_GENDER_TOKEN_PATTERN = re.compile(
    r"\bcautiv(\[[^\]]*Custom\(\s*['\"]ES_(?:OA|AO)['\"]\s*\)\])",
    re.IGNORECASE,
)
MALNACID_GENDER_TOKEN_PATTERN = re.compile(
    r"\bmalnacid(\[[^\]]*Custom\(\s*['\"]ES_(?:OA|AO)['\"]\s*\)\])",
    re.IGNORECASE,
)

SENSITIVE_PATH_PARTS = (
    "event_localization/",
    "schemes/",
    "/events/",
    "story_cycle",
)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def latest_run_id(conn) -> int | None:
    row = conn.execute("SELECT MAX(id) AS id FROM learned_validation_runs").fetchone()
    if not row or row["id"] is None:
        return None
    return int(row["id"])


def replace_safe_literals(text: str) -> tuple[str, int]:
    changed = 0
    result = text
    for source, target in sorted(SAFE_LITERAL_REPLACEMENTS.items(), key=lambda item: len(item[0]), reverse=True):
        pattern = re.compile(rf"(?<![A-Za-zÀ-ÿ]){re.escape(source)}(?![A-Za-zÀ-ÿ])", re.IGNORECASE)

        def replace_match(match: re.Match) -> str:
            matched = match.group(0)
            if matched[:1].isupper() and target[:1].islower():
                return target[:1].upper() + target[1:]
            return target

        result, count = pattern.subn(replace_match, result)
        changed += count
    return result, changed


def autofix_text(text: str) -> tuple[str, list[str]]:
    fixed = text
    rules: list[str] = []

    translated = fixed.translate(INVERTED_PUNCTUATION)
    for marker in MOJIBAKE_INVERTED_PUNCTUATION:
        translated = translated.replace(marker, "")
    if translated != fixed:
        fixed = translated
        rules.append("remove_inverted_punctuation")

    for old, new in ANGLED_QUOTE_REPLACEMENTS.items():
        if old in fixed:
            fixed = fixed.replace(old, new)
            rules.append("replace_angled_quotes")

    fixed2, count = GENDER_TOKEN_EXTRA_SUFFIX_PATTERN.subn(r"\1", fixed)
    if count:
        fixed = fixed2
        rules.append("remove_gender_token_extra_suffix")

    fixed2, count = GENDER_TOKEN_JOINED_PATTERN.subn(r"\1 ", fixed)
    if count:
        fixed = fixed2
        rules.append("space_after_gender_token")

    fixed2, count = CAUTIV_GENDER_TOKEN_PATTERN.subn(r"cativ\1", fixed)
    if count:
        fixed = fixed2
        rules.append("translate_cautiv_gender_stem")

    fixed2, count = MALNACID_GENDER_TOKEN_PATTERN.subn(r"maldit\1", fixed)
    if count:
        fixed = fixed2
        rules.append("translate_malnacid_gender_stem")

    fixed2, count = STYLE_TOKEN_JOINED_TO_WORD_PATTERN.subn(r"\1 ", fixed)
    fixed3, count2 = BRACKET_TOKEN_JOINED_TO_WORD_PATTERN.subn(r"\1 ", fixed2)
    count += count2
    if count:
        fixed = fixed3
        rules.append("space_after_token")

    fixed2, count = replace_safe_literals(fixed)
    if count:
        fixed = fixed2
        rules.append("safe_literal_replacement")

    return fixed, rules


def fetch_candidates(conn, run_id: int, limit: int | None, path_like: str | None) -> list[dict]:
    params: list[object] = [run_id]
    path_sql = ""
    if path_like:
        path_sql = "AND lvi.relative_path LIKE ?"
        params.append(path_like)
    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT ?"
        params.append(limit)
    rows = conn.execute(
        f"""
        SELECT lvi.*, s.spanish_text
        FROM learned_validation_items lvi
        JOIN source_segments s ON s.id = lvi.segment_id
        LEFT JOIN segment_confirmations sc ON sc.segment_id = lvi.segment_id
        WHERE lvi.run_id = ?
          AND lvi.action IN ('needs_autofix', 'needs_suggestion')
          AND lvi.token_status = 'ok'
          AND sc.segment_id IS NULL
          {path_sql}
        ORDER BY lvi.confidence_score DESC, lvi.segment_id ASC
        {limit_sql}
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def validate_fix(
    item: dict,
    fixed_text: str,
    rules: list[str],
    allowed_rules: set[str] | None = None,
) -> tuple[bool, dict, list[str]]:
    reasons: list[str] = []
    before_issues = json.loads(item["issues_json"] or "[]")
    after = local_quality_validator.validate_text(fixed_text)
    if allowed_rules is not None and not set(rules).issubset(allowed_rules):
        reasons.append("rule_not_allowed")
        return False, after, reasons
    if "space_after_token" in rules and local_quality_validator.EMBEDDED_GENDER_TOKEN_FRAGMENT_PATTERN.search(
        item["candidate_text"] or ""
    ):
        reasons.append("embedded_gender_token_fragment")
        return False, after, reasons
    if local_quality_validator.GENDER_TOKEN_UPPERCASE_SUFFIX_PATTERN.search(item["candidate_text"] or ""):
        reasons.append("gender_token_uppercase_suffix")
        return False, after, reasons
    if fixed_text == item["candidate_text"]:
        reasons.append("no_change")
        return False, after, reasons
    if protected_tokens(item["spanish_text"]) != protected_tokens(fixed_text):
        reasons.append("protected_tokens_mismatch")
        return False, after, reasons
    if after["high_issue_count"] > 0 or after["medium_issue_count"] > 0:
        reasons.append("remaining_validator_issues")
        return False, after, reasons
    if after["issue_count"] >= len(before_issues):
        reasons.append("no_issue_improvement")
        return False, after, reasons
    if any(token in fixed_text for token in FRAGILE_PRONOUN_TOKENS):
        reasons.append("fragile_pronoun")
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
        VALUES (?, 'auto_confirmed', ?, 'learned_autofix', 'autofix', 0, ?, 'learned_autofix', ?, ?)
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
    run_id: int | None = None,
    limit: int | None = None,
    path_like: str | None = None,
    allowed_rules: set[str] | None = None,
    apply: bool = False,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[apply_learned_autofix] Starting learned autofix")
    print(f"[apply_learned_autofix] Rule version: {RULE_VERSION}")
    print(f"[apply_learned_autofix] Apply: {apply}")
    print(f"[apply_learned_autofix] Limit: {limit or 'none'}")
    print(f"[apply_learned_autofix] Path filter: {path_like or 'none'}")
    print(
        "[apply_learned_autofix] Allowed rules: "
        f"{', '.join(sorted(allowed_rules)) if allowed_rules else 'all'}"
    )
    print(f"[apply_learned_autofix] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_run_id = run_id if run_id is not None else latest_run_id(conn)
        if selected_run_id is None:
            raise RuntimeError("No learned_validation_runs found. Run learned-report first.")
        rows = fetch_candidates(conn, selected_run_id, limit, path_like)
        accepted: list[dict] = []
        skipped: Counter[str] = Counter()
        rule_hits: Counter[str] = Counter()
        for item in rows:
            fixed_text, rules = autofix_text(item["candidate_text"] or "")
            item["rules"] = rules
            ok, after, reasons = validate_fix(item, fixed_text, rules, allowed_rules)
            if ok:
                item["fixed_text"] = fixed_text
                item["after"] = after
                accepted.append(item)
                rule_hits.update(rules)
            else:
                skipped.update(reasons)
        if apply:
            apply_confirmations(conn, accepted)
            conn.commit()

    elapsed = datetime.now() - started_at
    lines = [
        "Apply learned autofix report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {selected_run_id}",
        f"Apply: {apply}",
        f"Limit: {limit or 'none'}",
        f"Path filter: {path_like or 'none'}",
        f"Allowed rules: {', '.join(sorted(allowed_rules)) if allowed_rules else 'all'}",
        "",
        "Summary:",
        f"- Candidates inspected: {len(rows)}",
        f"- Auto-confirmable fixes: {len(accepted)}",
        f"- Applied confirmations: {len(accepted) if apply else 0}",
        "",
        "Rule hits:",
        *[f"- {rule}: {count}" for rule, count in rule_hits.most_common()],
        "",
        "Skipped:",
        *[f"- {reason}: {count}" for reason, count in skipped.most_common()],
        "",
        "Preview:",
    ]
    for item in accepted[:50]:
        before = (item["candidate_text"] or "").replace("\n", "\\n")
        after = item["fixed_text"].replace("\n", "\\n")
        if len(before) > 180:
            before = before[:180] + "..."
        if len(after) > 180:
            after = after[:180] + "..."
        lines.extend(
            [
                f"- segment {item['segment_id']} | {item['relative_path']}::{item['source_key']}",
                f"  rules: {', '.join(item['rules'])}",
                f"  before: {before}",
                f"  after:  {after}",
            ]
        )
    if not accepted:
        lines.append("- No auto-confirmable fixes")
    report_path = db.write_report(settings, "apply_learned_autofix", lines)
    print(f"[apply_learned_autofix] Candidates inspected: {len(rows)}")
    print(f"[apply_learned_autofix] Auto-confirmable fixes: {len(accepted)}")
    print(f"[apply_learned_autofix] Applied confirmations: {len(accepted) if apply else 0}")
    print(f"[apply_learned_autofix] Report: {report_path}")
    print("[apply_learned_autofix] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apply conservative autofixes from learned validation rows.")
    parser.add_argument("--run-id", type=int, default=None, help="learned_validation_runs id. Default: latest.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum needs_autofix/needs_suggestion rows to inspect.")
    parser.add_argument("--path-like", default=None, help="Optional SQL LIKE filter for relative_path.")
    parser.add_argument(
        "--allowed-rules",
        default=None,
        help="Comma-separated rule names allowed for automatic confirmation. Default: all rules.",
    )
    parser.add_argument("--apply", action="store_true", help="Write auto_confirmed rows. Default is dry-run.")
    args = parser.parse_args()
    allowed_rules = None
    if args.allowed_rules:
        allowed_rules = {rule.strip() for rule in args.allowed_rules.split(",") if rule.strip()}
    main(
        run_id=args.run_id,
        limit=args.limit,
        path_like=args.path_like,
        allowed_rules=allowed_rules,
        apply=args.apply,
    )
