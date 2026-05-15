from __future__ import annotations

import argparse
import re
from collections import Counter
from datetime import datetime

import db
import local_quality_validator
import suggest_translations
from auto_validate_segments import upsert_auto_confirmation


RULE_VERSION = "relationship_reason_rules_v1"
AUTO_SCORE = 0.994
ACTIONABLE_CODES = {
    "spanish_residue",
    "spanish_residue_in_literal",
    "missing_space_after_token",
    "missing_space_before_token",
    "gender_token_extra_suffix",
    "gender_token_joined_to_word",
}

STRING_LITERAL_PATTERN = re.compile(r"'([^']*)'|\"([^\"]*)\"")
GENDER_TOKEN_EXTRA_SUFFIX_PATTERN = re.compile(
    r"(\[[^\]]*Custom\(\s*['\"]ES_(?:OA|AO)['\"]\s*\)\])([ao])\b",
    re.IGNORECASE,
)
GENDER_TOKEN_JOINED_PATTERN = re.compile(
    r"(\[[^\]]*Custom\(\s*['\"]ES_(?:LeLa|LoLa|DelDela)['\"]\s*\)\])(?=[A-Za-zÀ-ÿ])",
    re.IGNORECASE,
)
MOSTROU_DEMONSTROU_PATTERN = re.compile(
    r"(\[[^\]]*['\"]mostrou['\"][^\]]*['\"]mostrou['\"][^\]]*\])\s+demonstrou\s+compreens(?:ão|Ã£o)",
    re.IGNORECASE,
)
BLOCKED_CANDIDATE_PATTERNS = (
    ("awkward_court_of_you", re.compile(r"\bA corte de \[[^\]]*['\"]voc[êÃª]", re.IGNORECASE)),
    ("awkward_let_free", re.compile(r"['\"]deixou['\"][^\]]*\]\s+libertar\b", re.IGNORECASE)),
    ("awkward_let_escape", re.compile(r"['\"]deixou['\"][^\]]*\]\s+escapar\s+de\b", re.IGNORECASE)),
    ("awkward_let_win", re.compile(r"['\"]deixou['\"][^\]]*\]\s+fazer\s+ganhar\b", re.IGNORECASE)),
    ("awkward_let_win_direct", re.compile(r"['\"]deixou['\"][^\]]*\]\s+vencer\b", re.IGNORECASE)),
    ("awkward_let_ruined", re.compile(r"['\"]deixou['\"][^\]]*\]\s+arruinou\b", re.IGNORECASE)),
    ("awkward_distracting_relations", re.compile(r"['\"]se entregou['\"][^\]]*\]\s+em rela[çÃ§][õÃµ]es distra[íÃ­]das", re.IGNORECASE)),
    ("awkward_saved_life_of_you", re.compile(r"['\"]salvou['\"][^\]]*\]\s+a\s+vida\s+de\s+\[[^\]]*['\"]voc[êÃª]", re.IGNORECASE)),
)

LITERAL_REPLACEMENTS = {
    "ti": "você",
    "tu persona": "você",
    "tu personaje": "seu personagem",
    "tu pueblo": "seu povo",
    "tu residencia": "sua residência",
    "tu familia": "sua família",
    "tu plantación": "sua plantação",
    "tu dominio": "seu domínio",
    "tus": "seus",
    "los": "os",
    "su": "sua",
    "tuya": "sua",
    "tuyo": "seu",
    "tuyas": "suas",
    "tuyos": "seus",
    "suya": "sua",
    "suyo": "seu",
    "suyas": "suas",
    "suyos": "seus",
    "confiaste": "confiou",
    "confió": "confiou",
    "curaste": "curou",
    "curó": "curou",
    "demostraste": "demonstrou",
    "demostró": "demonstrou",
    "cambiaste": "trocou",
    "cambió": "trocou",
    "mejoraste": "melhorou",
    "mejoró": "melhorou",
    "hiciste": "fez",
    "hizo": "fez",
    "salvaste": "salvou",
    "salvó": "salvou",
    "recibiste": "recebeu",
    "recibió": "recebeu",
    "abusaste": "abusou",
    "abusó": "abusou",
    "comprendiste y aceptaste": "compreendeu e aceitou",
    "comprendió y aceptó": "compreendeu e aceitou",
    "expusiste": "expôs",
    "expuso": "expôs",
    "dejaste": "deixou",
    "dejó": "deixou",
    "interferiste": "interferiu",
    "interfirió": "interferiu",
    "ayudaste": "ajudou",
    "ayudó": "ajudou",
    "sacrificaste": "sacrificou",
    "sacrificó": "sacrificou",
    "te deshiciste": "eliminou",
    "se deshizo": "eliminou",
    "te entregaste": "se entregou",
    "se entregó": "se entregou",
    "cuidaste": "cuidou",
    "cuidó": "cuidou",
    "pagaste": "pagou",
    "pagó": "pagou",
    "rescataste": "resgatou",
    "rescató": "resgatou",
    "intentaste": "tentou",
    "intentó": "tentou",
    "perdiste": "perdeu",
    "perdió": "perdeu",
    "regañaste": "repreendeu",
    "regañó": "repreendeu",
    "revelaste": "revelou",
    "reveló": "revelou",
    "impediste": "impediu",
    "impidió": "impediu",
    "encarcelaste": "prendeu",
    "encarceló": "prendeu",
    "exterminaste": "exterminou",
    "exterminó": "exterminou",
    "coaccionaste": "coagiu",
    "coaccionó": "coagiu",
    "tomaste": "tomou",
    "tomó": "tomou",
    "incitaste": "incitou",
    "incitó": "incitou",
    "sedujiste": "seduziu",
    "sedujo": "seduziu",
    "desarrollaste": "desenvolveu",
    "desarrolló": "desenvolveu",
    "secuestraste": "sequestrou",
    "secuestró": "sequestrou",
    "desterraste": "baniu",
    "desterró": "baniu",
    "abofeteaste": "esbofeteou",
    "abofeteó": "esbofeteou",
    "nombraste": "nomeou",
    "nombró": "nomeou",
    "abochornaste": "envergonhou",
    "abochornó": "envergonhou",
    "mostraste": "mostrou",
    "mostró": "mostrou",
    "elegiste": "escolheu",
    "eligió": "escolheu",
    "animaste": "incentivou",
    "animó": "incentivou",
    "compraste": "comprou",
    "compró": "comprou",
    "llegaste": "chegou",
    "llegó": "chegou",
    "adoptaste": "adotou",
    "adoptó": "adotou",
    "harías": "faria",
    "haría": "faria",
    "rezaste": "rezou",
    "rezó": "rezou",
    "enfureciste": "enfureceu",
    "enfureció": "enfureceu",
    "estás": "está",
    "está": "está",
    "íste": "",
    "yó": "",
    "iste": "",
    "ió": "",
    "es": "",
    "e": "",
}

DUPLICATE_POST_TOKEN_VERBS = (
    "confiou",
    "curou",
    "melhorou",
    "repreendeu",
    "revelou",
    "impediu",
    "prendeu",
    "exterminou",
    "coagiu",
    "tomou",
    "incitou",
    "seduziu",
    "desenvolveu",
    "sequestrou",
    "baniu",
    "esbofeteou",
    "nomeou",
    "envergonhou",
    "comprou",
    "chegou",
    "fez",
    "salvou",
    "recebeu",
    "abusou",
    "compreendeu e aceitou",
    "expôs",
    "deixou",
    "interferiu",
    "ajudou",
    "sacrificou",
    "eliminou",
    "se entregou",
    "cuidou",
    "pagou",
    "resgatou",
    "tentou",
)

PHRASE_REPLACEMENTS = {
    " en ": " em ",
    " al ": " ao ",
    " El tutor de ": " O tutor de ",
    " puso a ": " pôs ",
    " contra ": " contra ",
    " odia a ": " odeia ",
    " por favorecer a otros herederos": " por favorecer outros herdeiros",
    " manipuló astutamente a ": " manipulou astutamente ",
    " para que se enamorase locamente de ": " para que se apaixonasse loucamente por ",
    " abochornó a ": " envergonhou ",
    " interés por ": " interesse por ",
    " interÃ©s por ": " interesse por ",
    " reconociÃ³ pÃºblicamente a ": " reconheceu publicamente ",
    " reconoció públicamente a ": " reconheceu publicamente ",
    " cosas terribles para mantener a ": " coisas terríveis para manter ",
    " cosas terribles para mantener ": " coisas terríveis para manter ",
    " obrigou a ": " obrigou ",
    " desapareciÃ³ en acciÃ³n al servicio de ": " desapareceu em ação a serviço de ",
    " desapareció en acción al servicio de ": " desapareceu em ação a serviço de ",
    " admiraciÃ³n absoluta por ": " admiração absoluta por ",
    " admiración absoluta por ": " admiração absoluta por ",
}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def percent(part: int, total: int) -> float:
    if total == 0:
        return 0.0
    return part / total * 100


def short(value: str | None, limit: int = 180) -> str:
    text = (value or "").replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def issue_codes(validation: dict) -> Counter:
    return Counter(issue["code"] for issue in validation["issues"])


def actionable_total(codes: Counter) -> int:
    return sum(total for code, total in codes.items() if code in ACTIONABLE_CODES)


def replace_literal(match: re.Match) -> str:
    quote = "'" if match.group(1) is not None else '"'
    literal = match.group(1) if match.group(1) is not None else match.group(2)
    replacement = LITERAL_REPLACEMENTS.get(literal.casefold())
    if replacement is None:
        return match.group(0)
    if literal[:1].isupper():
        replacement = replacement[:1].upper() + replacement[1:]
    return f"{quote}{replacement}{quote}"


def apply_literal_replacements(value: str) -> tuple[str, int]:
    return STRING_LITERAL_PATTERN.subn(replace_literal, value)


def apply_phrase_replacements(value: str) -> tuple[str, int]:
    updated = value
    total = 0
    for source, target in PHRASE_REPLACEMENTS.items():
        updated, count = re.subn(re.escape(source), target, updated)
        total += count
    return updated, total


def apply_gender_token_fixes(value: str) -> tuple[str, int]:
    updated, suffix_count = GENDER_TOKEN_EXTRA_SUFFIX_PATTERN.subn(r"\1", value)
    updated, joined_count = GENDER_TOKEN_JOINED_PATTERN.subn(r"\1 ", updated)
    return updated, suffix_count + joined_count


def apply_contextual_fixes(value: str) -> tuple[str, int]:
    updated, title_count = re.subn(
        r"(títulos baratos) a (\[[^\]]*LocalPlayerString\(\s*['\"])sua pessoa(['\"]\s*,)",
        r"\1 de \2você\3",
        value,
        flags=re.IGNORECASE,
    )
    updated, showed_count = MOSTROU_DEMONSTROU_PATTERN.subn(r"\1 compreensão", updated)
    updated, saved_count = re.subn(
        r"(\[[^\]]*['\"]salvou['\"][^\]]*['\"]salvou['\"][^\]]*\])\s+de\s+(\[[^\]]+\])\s+de ser",
        r"\1 \2 de ser",
        updated,
        flags=re.IGNORECASE,
    )
    updated, eliminated_count = re.subn(
        r"(\[[^\]]*['\"]eliminou['\"][^\]]*['\"]eliminou['\"][^\]]*\])\s+de todos",
        r"\1 todos",
        updated,
        flags=re.IGNORECASE,
    )
    return updated, title_count + showed_count + saved_count + eliminated_count

    updated, count = re.subn(
        r"(tÃ­tulos baratos) a (\[[^\]]*LocalPlayerString\(\s*['\"])sua pessoa(['\"]\s*,)",
        r"\1 de \2vocÃª\3",
        value,
        flags=re.IGNORECASE,
    )
    updated, count_utf = re.subn(
        r"(títulos baratos) a (\[[^\]]*LocalPlayerString\(\s*['\"])sua pessoa(['\"]\s*,)",
        r"\1 de \2você\3",
        updated,
        flags=re.IGNORECASE,
    )
    updated, showed_count = MOSTROU_DEMONSTROU_PATTERN.subn(r"\1 compreensão", updated)
    return updated, count + count_utf + showed_count


def blocked_candidate_reason(value: str) -> str | None:
    for reason, pattern in BLOCKED_CANDIDATE_PATTERNS:
        if pattern.search(value):
            return reason
    return None


def apply_duplicate_post_token_verb_fixes(value: str) -> tuple[str, int]:
    updated = value
    total = 0
    for verb in DUPLICATE_POST_TOKEN_VERBS:
        escaped = re.escape(verb)
        pattern = re.compile(
            rf"(\[[^\]]*['\"]{escaped}['\"][^\]]*['\"]{escaped}['\"][^\]]*\])\s+{escaped}\b\s*",
            re.IGNORECASE,
        )
        updated, count = pattern.subn(r"\1 ", updated)
        total += count
    return updated, total


def build_candidate(row) -> tuple[str | None, list[str]]:
    base_text = row["confirmed_text"] or row["old_text"] or row["spanish_text"]
    if not base_text:
        return None, []

    updated = str(base_text)
    rules: list[str] = []

    updated, count = apply_literal_replacements(updated)
    if count:
        rules.append(f"literal:{count}")

    updated, count = apply_phrase_replacements(updated)
    if count:
        rules.append(f"phrase:{count}")

    updated, count = apply_gender_token_fixes(updated)
    if count:
        rules.append(f"gender_token:{count}")

    updated, count = apply_contextual_fixes(updated)
    if count:
        rules.append(f"context:{count}")

    updated, count = apply_duplicate_post_token_verb_fixes(updated)
    if count:
        rules.append(f"duplicate_verb:{count}")

    if updated == base_text:
        return None, []
    return updated, rules


def load_rows(conn, start_after: int | None = None) -> list:
    return conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.spanish_text,
            s.english_text,
            s.old_text,
            sc.confirmed_text,
            sc.confirmation_level,
            COALESCE(sc.locked, 0) AS locked
        FROM source_segments s
        LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
        WHERE s.is_active = 1
          AND (? IS NULL OR s.id > ?)
          AND COALESCE(sc.locked, 0) = 0
          AND COALESCE(sc.confirmed_text, s.old_text, s.spanish_text, '') != ''
          AND s.relative_path LIKE 'relationship_reasons%'
        ORDER BY s.id
        """,
        (start_after, start_after),
    ).fetchall()


def purge_invalid_confirmations(conn, apply: bool) -> int:
    rows = conn.execute(
        """
        SELECT sc.segment_id, sc.confirmed_text
        FROM segment_confirmations sc
        JOIN source_segments s ON s.id = sc.segment_id
        WHERE sc.confirmation_source = 'relationship_reason_rule'
          AND sc.locked = 0
          AND s.relative_path LIKE 'relationship_reasons%'
        """
    ).fetchall()
    invalid_ids: list[int] = []
    for row in rows:
        validation = local_quality_validator.validate_text(row["confirmed_text"])
        if actionable_total(issue_codes(validation)) > 0:
            invalid_ids.append(row["segment_id"])

    if apply and invalid_ids:
        placeholders = ", ".join("?" for _ in invalid_ids)
        conn.execute(
            f"""
            DELETE FROM segment_confirmations
            WHERE confirmation_source = 'relationship_reason_rule'
              AND locked = 0
              AND segment_id IN ({placeholders})
            """,
            invalid_ids,
        )
    return len(invalid_ids)


def main(
    limit: int | None = None,
    apply: bool = False,
    scan_limit: int | None = None,
    start_after: int | None = None,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[apply_relationship_reason_rules] Starting relationship reason rule pass")
    print(f"[apply_relationship_reason_rules] Rule version: {RULE_VERSION}")
    print(f"[apply_relationship_reason_rules] Validator version: {local_quality_validator.RULE_VERSION}")
    print(f"[apply_relationship_reason_rules] Apply: {apply}")
    print(f"[apply_relationship_reason_rules] Limit: {limit if limit is not None else 'none'}")
    print(f"[apply_relationship_reason_rules] Scan limit: {scan_limit if scan_limit is not None else 'none'}")
    print(f"[apply_relationship_reason_rules] Start after: {start_after if start_after is not None else 'none'}")

    inspected = 0
    changed = 0
    accepted = 0
    skipped = Counter()
    rule_counts = Counter()
    before_issue_counts = Counter()
    after_issue_counts = Counter()
    preview: list[tuple[dict, str, list[str], Counter, Counter]] = []

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        invalid_confirmations = purge_invalid_confirmations(conn, apply)
        if invalid_confirmations and apply:
            conn.commit()

        for row in load_rows(conn, start_after):
            inspected += 1
            if scan_limit is not None and inspected > scan_limit:
                skipped["scan_limit_reached"] += 1
                break

            candidate, rules = build_candidate(row)
            if candidate is None:
                skipped["no_rule_change"] += 1
                continue
            changed += 1

            token_state, _ = suggest_translations.token_status(row["spanish_text"], candidate)
            if token_state != "ok":
                skipped[f"token_{token_state}"] += 1
                continue

            base_text = row["confirmed_text"] or row["old_text"] or row["spanish_text"]
            before_validation = local_quality_validator.validate_text(base_text)
            after_validation = local_quality_validator.validate_text(candidate)
            before_codes = issue_codes(before_validation)
            after_codes = issue_codes(after_validation)
            before_actionable = actionable_total(before_codes)
            after_actionable = actionable_total(after_codes)

            if after_actionable >= before_actionable:
                skipped["no_actionable_improvement"] += 1
                continue
            if after_actionable > 0:
                skipped["partial_fix_remaining_issues"] += 1
                continue
            blocked_reason = blocked_candidate_reason(candidate)
            if blocked_reason is not None:
                skipped[blocked_reason] += 1
                continue

            item = {
                "segment_id": row["segment_id"],
                "candidate_text": candidate,
                "candidate_source": "relationship_reason_rule",
                "feedback_id": None,
            }
            accepted += 1
            rule_counts.update(rule.split(":", 1)[0] for rule in rules)
            before_issue_counts.update(before_codes)
            after_issue_counts.update(after_codes)
            if len(preview) < 50:
                preview.append((dict(row), candidate, rules, before_codes, after_codes))
            if apply:
                upsert_auto_confirmation(conn, item, AUTO_SCORE)

            if limit is not None and accepted >= limit:
                break

        if apply:
            conn.commit()

        total_segments = int(
            conn.execute("SELECT COUNT(*) FROM source_segments WHERE is_active = 1").fetchone()[0] or 0
        )
        confirmed_rows = conn.execute(
            """
            SELECT confirmation_level, COUNT(*) AS total
            FROM segment_confirmations
            GROUP BY confirmation_level
            """
        ).fetchall()

    confirmed = {row["confirmation_level"]: int(row["total"] or 0) for row in confirmed_rows}
    total_confirmed = sum(confirmed.values())
    elapsed = datetime.now() - started_at
    report_lines = [
        "Relationship reason rule pass report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Validator version: {local_quality_validator.RULE_VERSION}",
        f"Apply: {apply}",
        f"Limit: {limit if limit is not None else 'none'}",
        f"Scan limit: {scan_limit if scan_limit is not None else 'none'}",
        f"Start after: {start_after if start_after is not None else 'none'}",
        "",
        "Summary:",
        f"- Segments inspected: {inspected}",
        f"- Segments changed by rules: {changed}",
        f"- Auto-confirmable fixes: {accepted}",
        f"- Applied confirmations: {accepted if apply else 0}",
        f"- Invalid prior confirmations {'purged' if apply else 'found'}: {invalid_confirmations}",
        f"- Active segments: {total_segments}",
        f"- Confirmed after run: {total_confirmed} ({percent(total_confirmed, total_segments):.4f}%)",
        f"- Human confirmed: {confirmed.get('human_confirmed', 0)}",
        f"- Auto confirmed: {confirmed.get('auto_confirmed', 0)}",
        "",
        "Rule hits accepted:",
    ]
    report_lines.extend(f"- {rule}: {count}" for rule, count in rule_counts.most_common())
    if not rule_counts:
        report_lines.append("- none")

    report_lines.extend(["", "Before issue counts on accepted fixes:"])
    report_lines.extend(f"- {code}: {count}" for code, count in before_issue_counts.most_common())
    if not before_issue_counts:
        report_lines.append("- none")

    report_lines.extend(["", "After issue counts on accepted fixes:"])
    report_lines.extend(f"- {code}: {count}" for code, count in after_issue_counts.most_common())
    if not after_issue_counts:
        report_lines.append("- none")

    report_lines.extend(["", "Skipped reasons:"])
    report_lines.extend(f"- {reason}: {count}" for reason, count in skipped.most_common())
    if not skipped:
        report_lines.append("- none")

    report_lines.extend(["", "Preview:"])
    if not preview:
        report_lines.append("- No accepted fixes")
    for row, candidate, rules, before_codes, after_codes in preview:
        report_lines.extend(
            [
                f"- segment {row['segment_id']} | {row['relative_path']}::{row['source_key']}",
                f"  rules: {', '.join(rules)}",
                f"  before issues: {dict(before_codes)}",
                f"  after issues: {dict(after_codes)}",
                f"  before: {short(row['confirmed_text'] or row['old_text'] or row['spanish_text'])}",
                f"  after:  {short(candidate)}",
            ]
        )

    report_path = db.write_report(settings, "apply_relationship_reason_rules", report_lines)
    print(f"[apply_relationship_reason_rules] Segments inspected: {inspected}")
    print(f"[apply_relationship_reason_rules] Segments changed by rules: {changed}")
    print(f"[apply_relationship_reason_rules] Auto-confirmable fixes: {accepted}")
    print(f"[apply_relationship_reason_rules] Applied confirmations: {accepted if apply else 0}")
    print(
        "[apply_relationship_reason_rules] Invalid prior confirmations "
        f"{'purged' if apply else 'found'}: {invalid_confirmations}"
    )
    print(f"[apply_relationship_reason_rules] Report: {report_path}")
    print("[apply_relationship_reason_rules] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Preview or apply relationship reason residue fixes.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum accepted fixes to preview/apply.")
    parser.add_argument("--scan-limit", type=int, default=None, help="Maximum rows to inspect before writing a report.")
    parser.add_argument("--start-after", type=int, default=None, help="Only inspect rows with source segment id greater than this value.")
    parser.add_argument("--apply", action="store_true", help="Write auto_confirmed rows. Default is report only.")
    args = parser.parse_args()
    main(limit=args.limit, apply=args.apply, scan_limit=args.scan_limit, start_after=args.start_after)
