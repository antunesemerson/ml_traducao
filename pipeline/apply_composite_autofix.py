from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from typing import Any

import db
import local_quality_validator
from apply_safe_output_updates import protected_tokens


RULE_VERSION = "apply_composite_autofix_v5"
AUTO_SCORE = 0.993

PROTECTED_TOKEN_PATTERN = re.compile(
    r"\$[^$\s]+\$|\[[^\]]+\]|#[A-Za-z0-9_]+|#!|@[A-Za-z0-9_]+!|\\n"
)
TOKEN_PATTERN = re.compile(r"\[[^\]]+\]")
STRING_LITERAL_PATTERN = re.compile(r"'([^']*)'|\"([^\"]*)\"")
WORD_CHARS = r"A-Za-zÀ-ÿ"
SPANISH_VERB_RESIDUE_PATTERN = re.compile(
    r"\b[\wÀ-ÿ]+(?:aste|iste|ó|ió|aras|ases|asteis|aron|ieron)\b",
    re.IGNORECASE,
)

FRAGILE_PRONOUN_TOKENS = (
    "GetHerHim",
    "GetHerHis",
    "GetSheHe",
    "GetHerselfHimself",
    "GetHersHis",
)
POST_TOKEN_DUPLICATED_VERB_PATTERN = re.compile(
    r"\]\s+("
    r"questionou|salvou|ajudou|rejeitou|aceitou|acusou|humilhou|"
    r"insultou|atacou|derrotou|defendeu|apoiou|expôs|expuso|"
    r"abandonou|roubou|resgatou|pagou|perdoou|recompensou"
    r")\s+\1\b",
    re.IGNORECASE,
)
LOCAL_PLAYER_STRING_DUPLICATED_FOLLOWING_WORD_PATTERN = re.compile(
    r"LocalPlayerString\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]\1['\"]\s*\)\]\s+\1\b",
    re.IGNORECASE,
)

VISIBLE_REPLACEMENTS = {
    "Cualquiera de estas:": "Qualquer uma destas:",
    "Ganancia de": "Ganho de",
    "#high Ganarás#!": "#high Ganhará#!",
    "perderás": "perderá",
    "Ganarás": "Ganhará",
    "ganarás": "ganhará",
    "Puede que le pillen": "Podem pegá-lo",
    "Puede que te pillen": "Podem pegar você",
    "de tus [counties|lE]": "dos seus [counties|lE]",
    "extender el señorío": "expandir o senhorio",
    "ahora mismo": "agora",
    "20 años": "20 anos",
    "10 años": "10 anos",
    "según el número de": "conforme o número de",
    "presentes en tu": "presentes em sua",
    "reduce a la mitad el coste del": "reduz pela metade o custo do",
    "Como [minister|El], puedes reclutar a cualquier tipo de comandante": (
        "Como [minister|El], você pode recrutar qualquer tipo de comandante"
    ),
    "Solo necesito los textos y comentarios esenciales para exponer mis argumentos.": (
        "Só preciso dos textos e comentários essenciais para expor meus argumentos."
    ),
    "Mis criterios son altos, pero razonables.": "Meus critérios são altos, mas razoáveis.",
    "Si queremos unos funcionarios gubernamentales nombrados en función del mérito": (
        "Se queremos funcionários governamentais nomeados por mérito"
    ),
    "tendrán que alcanzar un cierto nivel de excelencia": "terão que alcançar certo nível de excelência",
    "Un barco ha de ser dirigido por la tripulación correcta.": (
        "Um barco deve ser conduzido pela tripulação certa."
    ),
    "La muerte no es momento de ostentación y pompa": "A morte não é momento de ostentação e pompa",
    "es una ocasión para reunirnos y presentar nuestros humildes respetos.": (
        "é uma ocasião para nos reunirmos e prestar nossos humildes respeitos."
    ),
    "El nuevo [modifier|lE] de [skill|El] del [artifact|lE] deriva del tipo de [education|lE]": (
        "O novo [modifier|lE] de [skill|El] do [artifact|lE] deriva do tipo de [education|lE]"
    ),
    "Una coronación exigua, un intento patético de impresionar": (
        "Uma coroação simples, uma tentativa patética de impressionar"
    ),
    "Gran coronación": "Grande coroação",
    "Coronación exigua": "Coroação simples",
    "Coronación modesta": "Coroação modesta",
    "Coronación respetable": "Coroação respeitável",
    "Coronación resplandeciente": "Coroação resplandecente",
    "coronación": "coroação",
    "de verdad": "de verdade",
    "LO SABÍA": "EU SABIA",
}

LITERAL_REPLACEMENTS = {
    "cabeza de tu fe": "chefe da sua fé",
    "tu persona": "você",
    "tu personaje": "seu personagem",
    "tu señorío": "seu senhorio",
    "tu señor": "seu senhor",
    "Mi": "Minha",
    "tuyo/a": "seu/sua",
    "tuyo": "seu",
    "Tu honor": "Sua honra",
    "El honor de ": "A honra de ",
    "una asesina": "uma assassina",
    "un asesino": "um assassino",
    "una mala anfitriona": "uma má anfitriã",
    "un mal anfitrión": "um mau anfitrião",
    "te unirás": "vai se unir",
    "se unirá": "vai se unir",
    "te vengaste": "vingou-se",
    "se vengó": "se vingou",
    "te enfrentarás": "enfrentará",
    "se enfrentará": "enfrentará",
    "cuestionaste": "questionou",
    "cuestionó": "questionou",
}

EXACT_LITERAL_REPLACEMENTS = {
    "Mi": "Minha",
    "Tu": "Sua",
    "Su": "Sua",
    "Tú": "Você",
    "tu": "sua",
    "su": "sua",
    "tus": "seus",
    "Tus": "Seus",
    "los": "os",
    "Los": "Os",
    "las": "as",
    "Las": "As",
    "un": "um",
    "Un": "Um",
    "una": "uma",
    "Una": "Uma",
    "del": "do",
    "Del": "Do",
    "de la": "da",
    "De la": "Da",
    "ti": "você",
    "Ti": "Você",
    "eres": "é",
    "es": "é",
}

PUNCTUATION_REPLACEMENTS = {
    "¿": "",
    "¡": "",
    "«": '"',
    "»": '"',
}

GENDER_STEM_REPLACEMENTS = (
    (re.compile(r"\bmalnacid(\[[^\]]*Custom\(\s*['\"]ES_(?:OA|AO)['\"]\s*\)\])", re.IGNORECASE), r"maldit\1"),
    (re.compile(r"\bcautiv(\[[^\]]*Custom\(\s*['\"]ES_(?:OA|AO)['\"]\s*\)\])", re.IGNORECASE), r"cativ\1"),
)

STYLE_TOKEN_JOINED_TO_WORD_PATTERN = re.compile(r"(#!|\$[A-Za-z0-9_]+\$)(?=[^\W\d_])")
BRACKET_TOKEN_JOINED_TO_WORD_PATTERN = re.compile(
    r"(\](?![aos]\b|as\b|os\b))(?=[^\W\d_])",
    re.IGNORECASE,
)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def latest_run_id(conn) -> int | None:
    row = conn.execute("SELECT MAX(id) AS id FROM learned_validation_runs").fetchone()
    if not row or row["id"] is None:
        return None
    return int(row["id"])


def command_base_name(token: str) -> str:
    command_name = token[1:].split("(", 1)[0].split("|", 1)[0].strip()
    return command_name.split(".")[-1]


def is_literal_translatable(base_name: str, literal_index: int, literal: str) -> bool:
    if local_quality_validator.is_technical_literal(literal):
        return False
    if base_name == "Concept":
        return literal_index >= 2
    return (
        base_name in {"Select_CString", "LocalPlayerString", "PlayerString", "GetString"}
        or base_name.startswith("SelectLocalization")
        or base_name.endswith("String")
    )


def replace_case_aware(source: str, target: str, text: str) -> tuple[str, int]:
    pattern = re.compile(rf"(?<![{WORD_CHARS}]){re.escape(source)}(?![{WORD_CHARS}])", re.IGNORECASE)

    def repl(match: re.Match) -> str:
        value = match.group(0)
        if value.isupper():
            return target.upper()
        if value[:1].isupper() and target[:1].islower():
            return target[:1].upper() + target[1:]
        return target

    return pattern.subn(repl, text)


def replace_literal_content(literal: str) -> tuple[str, int]:
    stripped = literal.strip()
    if stripped in EXACT_LITERAL_REPLACEMENTS:
        prefix = literal[: len(literal) - len(literal.lstrip())]
        suffix = literal[len(literal.rstrip()) :]
        return f"{prefix}{EXACT_LITERAL_REPLACEMENTS[stripped]}{suffix}", 1

    fixed = literal
    changed = 0
    for source, target in sorted(LITERAL_REPLACEMENTS.items(), key=lambda item: len(item[0]), reverse=True):
        fixed, count = replace_case_aware(source, target, fixed)
        changed += count
    return fixed, changed


def apply_literal_replacements(text: str) -> tuple[str, int]:
    result: list[str] = []
    position = 0
    changed = 0
    for token_match in TOKEN_PATTERN.finditer(text):
        result.append(text[position : token_match.start()])
        token = token_match.group(0)
        base_name = command_base_name(token)
        literal_index = 0
        rebuilt: list[str] = []
        literal_position = 0
        for literal_match in STRING_LITERAL_PATTERN.finditer(token):
            literal_index += 1
            literal = literal_match.group(1) if literal_match.group(1) is not None else literal_match.group(2)
            quote = "'" if literal_match.group(1) is not None else '"'
            rebuilt.append(token[literal_position : literal_match.start()])
            if is_literal_translatable(base_name, literal_index, literal):
                fixed_literal, count = replace_literal_content(literal)
                changed += count
            else:
                fixed_literal = literal
            rebuilt.append(f"{quote}{fixed_literal}{quote}")
            literal_position = literal_match.end()
        rebuilt.append(token[literal_position:])
        result.append("".join(rebuilt))
        position = token_match.end()
    result.append(text[position:])
    return "".join(result), changed


def transform_text(text: str) -> tuple[str, list[str]]:
    fixed = text
    rules: list[str] = []

    for source, target in PUNCTUATION_REPLACEMENTS.items():
        if source in fixed:
            fixed = fixed.replace(source, target)
            rules.append("normalize_spanish_punctuation")

    for pattern, replacement in GENDER_STEM_REPLACEMENTS:
        fixed2, count = pattern.subn(replacement, fixed)
        if count:
            fixed = fixed2
            rules.append("gender_stem_replacement")

    for source, target in sorted(VISIBLE_REPLACEMENTS.items(), key=lambda item: len(item[0]), reverse=True):
        fixed, count = replace_case_aware(source, target, fixed)
        if count:
            rules.append("visible_composite_replacement")

    fixed2, count = apply_literal_replacements(fixed)
    if count:
        fixed = fixed2
        rules.append("literal_composite_replacement")

    fixed2, count = STYLE_TOKEN_JOINED_TO_WORD_PATTERN.subn(r"\1 ", fixed)
    fixed3, count2 = BRACKET_TOKEN_JOINED_TO_WORD_PATTERN.subn(r"\1 ", fixed2)
    if count or count2:
        fixed = fixed3
        rules.append("space_after_token")

    return fixed, sorted(set(rules))


def fetch_candidates(conn, run_id: int, limit: int | None, path_like: str | None) -> list[dict[str, Any]]:
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
        ORDER BY
          CASE lvi.action WHEN 'needs_autofix' THEN 0 ELSE 1 END,
          lvi.word_count ASC,
          lvi.segment_id ASC
        {limit_sql}
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def validate_candidate(item: dict[str, Any], fixed_text: str, rules: list[str]) -> tuple[bool, dict[str, Any], list[str]]:
    reasons: list[str] = []
    if fixed_text == (item.get("candidate_text") or ""):
        reasons.append("no_change")
        return False, {}, reasons
    if any(token in fixed_text for token in FRAGILE_PRONOUN_TOKENS):
        reasons.append("fragile_pronoun")
        return False, {}, reasons
    lowered = fixed_text.casefold()
    blocked_fragments = (
        "vengaste",
        "vengó",
        "enfrentarás",
        "enfrentará",
        "unidas as",
        "unidos os",
        "ermitañ",
        "gobernación",
        "tesorería",
        "hermosa",
        "hermoso",
        "buena",
        "buen ",
        "verdadera",
        "verdadero",
        "reina",
        "rey",
        "mujer",
        "hombre",
        "señora",
        "señor",
        "partidaria",
        "partidario",
        "patrón",
        "anciana",
        "anciano",
        "caballero",
        "escribana",
        "escribano",
        "seductora",
        "seductor",
        "juego",
        "marcialidad",
        "ganas",
        "gana'",
        "formas",
        "forma'",
        "respondes",
        "sacaste",
        "sacó",
        "socavaste",
        "socavó",
        "cumpliste",
        "cumplió",
        "confesaste",
        "confesó",
        "abusaste",
        "abusó",
        "organizaste",
        "organizó",
        "colmaste",
        "colmó",
        "llenaste",
        "llenó",
        "sacrificaste",
        "sacrificó",
        "nombras",
        "nombra'",
        "culpas",
        "culpa'",
        "sus'",
        "uno'",
        "vosotras",
        "vosotros",
        "councilwoman",
        "councilman",
        "coemperatriz",
        "coemperador",
        "niña",
        "niño",
        "atea",
        "ateo",
        "maestra",
        "maestro",
        "plebeya",
        "plebeyo",
        "muchas",
        "muchos",
        "tejedoras",
        "tejedores",
        "'mi'",
        "'su'",
        "'tu'",
        "'te'",
        "'le'",
    )
    if any(fragment in lowered for fragment in blocked_fragments):
        reasons.append("blocked_residual_literal_fragment")
        return False, {}, reasons
    if SPANISH_VERB_RESIDUE_PATTERN.search(fixed_text):
        reasons.append("blocked_residual_spanish_verb")
        return False, {}, reasons
    if POST_TOKEN_DUPLICATED_VERB_PATTERN.search(fixed_text):
        reasons.append("blocked_duplicated_post_token_verb")
        return False, {}, reasons
    if LOCAL_PLAYER_STRING_DUPLICATED_FOLLOWING_WORD_PATTERN.search(fixed_text):
        reasons.append("blocked_local_player_string_duplicate")
        return False, {}, reasons
    if protected_tokens(item["spanish_text"]) != protected_tokens(fixed_text):
        reasons.append("protected_tokens_mismatch")
        return False, {}, reasons
    quality = local_quality_validator.validate_text(fixed_text)
    if quality["high_issue_count"] > 0:
        reasons.append("remaining_high_issues")
    if quality["medium_issue_count"] > 0:
        reasons.append("remaining_medium_issues")
    if fixed_text.count('"') % 2:
        reasons.append("unbalanced_quotes")
    if not rules:
        reasons.append("no_rules")
    return not reasons, quality, reasons or ["validator_clean"]


def apply_confirmations(conn, accepted: list[dict[str, Any]]) -> None:
    timestamp = now()
    conn.executemany(
        """
        INSERT INTO segment_confirmations (
            segment_id, confirmation_level, confirmed_text, confirmation_source,
            confirmation_label, locked, confidence_score, reviewer, confirmed_at, updated_at
        )
        VALUES (?, 'auto_confirmed', ?, 'composite_autofix', ?, 0, ?, 'composite_autofix', ?, ?)
        ON CONFLICT(segment_id) DO UPDATE SET
            confirmation_level = CASE WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmation_level ELSE 'auto_confirmed' END,
            confirmed_text = CASE WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmed_text ELSE excluded.confirmed_text END,
            confirmation_source = CASE WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmation_source ELSE excluded.confirmation_source END,
            confirmation_label = CASE WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confirmation_label ELSE excluded.confirmation_label END,
            confidence_score = CASE WHEN segment_confirmations.locked = 1 THEN segment_confirmations.confidence_score ELSE excluded.confidence_score END,
            reviewer = CASE WHEN segment_confirmations.locked = 1 THEN segment_confirmations.reviewer ELSE excluded.reviewer END,
            updated_at = ?
        """,
        [
            (
                item["segment_id"],
                item["fixed_text"],
                "+".join(item["rules"]),
                AUTO_SCORE,
                timestamp,
                timestamp,
                timestamp,
            )
            for item in accepted
        ],
    )


def sample_text(value: str | None, limit: int = 220) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t")
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def build_report(
    started_at: datetime,
    run_id: int,
    apply: bool,
    inspected: int,
    accepted: list[dict[str, Any]],
    skipped: Counter[str],
    rule_hits: Counter[str],
    limit: int | None,
    path_like: str | None,
) -> list[str]:
    elapsed = datetime.now() - started_at
    package_counts = Counter(item["relative_path"] for item in accepted)
    lines = [
        "Apply composite autofix report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Apply: {apply}",
        f"Limit: {limit or 'none'}",
        f"Path filter: {path_like or 'none'}",
        "",
        "Summary:",
        f"- Candidates inspected: {inspected}",
        f"- Auto-confirmable composite fixes: {len(accepted)}",
        f"- Applied confirmations: {len(accepted) if apply else 0}",
        "",
        "Rule hits:",
        *[f"- {rule}: {count}" for rule, count in rule_hits.most_common()],
        "",
        "Skipped:",
        *[f"- {reason}: {count}" for reason, count in skipped.most_common()],
        "",
        "Top packages:",
        *[f"- {path}: {count}" for path, count in package_counts.most_common(30)],
        "",
        "Preview:",
    ]
    for item in accepted[:40]:
        lines.extend(
            [
                f"- segment {item['segment_id']} | {item['relative_path']}::{item['source_key']}",
                f"  rules: {', '.join(item['rules'])}",
                f"  before: {sample_text(item['candidate_text'], 260)}",
                f"  after:  {sample_text(item['fixed_text'], 260)}",
            ]
        )
    if not accepted:
        lines.append("- No accepted fixes")
    return lines


def main(
    run_id: int | None = None,
    limit: int | None = None,
    path_like: str | None = None,
    apply: bool = False,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[apply_composite_autofix] Starting composite autofix")
    print(f"[apply_composite_autofix] Rule version: {RULE_VERSION}")
    print(f"[apply_composite_autofix] Apply: {apply}")
    print(f"[apply_composite_autofix] Limit: {limit or 'none'}")
    print(f"[apply_composite_autofix] Path filter: {path_like or 'none'}")
    print(f"[apply_composite_autofix] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_run_id = run_id if run_id is not None else latest_run_id(conn)
        if selected_run_id is None:
            raise RuntimeError("No learned_validation_runs found. Run learned-report first.")
        rows = fetch_candidates(conn, selected_run_id, limit, path_like)
        accepted: list[dict[str, Any]] = []
        skipped: Counter[str] = Counter()
        rule_hits: Counter[str] = Counter()
        for item in rows:
            fixed_text, rules = transform_text(item["candidate_text"] or "")
            ok, quality, reasons = validate_candidate(item, fixed_text, rules)
            if ok:
                item["fixed_text"] = fixed_text
                item["rules"] = rules
                item["quality"] = quality
                accepted.append(item)
                rule_hits.update(rules)
            else:
                skipped.update(reasons)
        if apply:
            apply_confirmations(conn, accepted)
            conn.commit()

    report = build_report(
        started_at=started_at,
        run_id=selected_run_id,
        apply=apply,
        inspected=len(rows),
        accepted=accepted,
        skipped=skipped,
        rule_hits=rule_hits,
        limit=limit,
        path_like=path_like,
    )
    report_path = db.write_report(settings, "apply_composite_autofix", report)
    print(f"[apply_composite_autofix] Run id: {selected_run_id}")
    print(f"[apply_composite_autofix] Candidates inspected: {len(rows)}")
    print(f"[apply_composite_autofix] Auto-confirmable composite fixes: {len(accepted)}")
    print(f"[apply_composite_autofix] Applied confirmations: {len(accepted) if apply else 0}")
    print(f"[apply_composite_autofix] Report: {report_path}")
    print("[apply_composite_autofix] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apply token-safe composite offline fixes from learned validation items.")
    parser.add_argument("--run-id", type=int, default=None, help="learned_validation_runs id. Default is latest.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum candidates inspected.")
    parser.add_argument("--path-like", default=None, help="Optional SQL LIKE filter for relative_path.")
    parser.add_argument("--apply", action="store_true", help="Write auto confirmations. Default is dry-run.")
    args = parser.parse_args()
    main(run_id=args.run_id, limit=args.limit, path_like=args.path_like, apply=args.apply)
