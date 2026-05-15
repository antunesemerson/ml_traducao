from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime

import db
import local_quality_validator
from apply_safe_output_updates import protected_tokens


RULE_VERSION = "package_autofix_v1"
DEFAULT_FOCUS_GROUP = "high_impact_v1"

PROTECTED_TOKEN_PATTERN = re.compile(
    r"\$[^$\s]+\$|\[[^\]]+\]|#[A-Za-z0-9_]+|#!|@[A-Za-z0-9_]+!|\\n"
)
STRING_LITERAL_PATTERN = re.compile(r"'([^']*)'|\"([^\"]*)\"")
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
SPANISH_INVERTED_PUNCTUATION = str.maketrans({"¿": "", "¡": ""})
ANGLED_QUOTES = {"«": '"', "»": '"'}

SAFE_LITERAL_REPLACEMENTS = {
    "ávida cazadora": "ávida caçadora",
    "ávido cazador": "ávido caçador",
    "distancia bastante corta": "distância bastante curta",
    "distancia corta": "distância curta",
    "distancia intermedia": "distância intermediária",
    "distancia respetable": "distância respeitável",
    "extraordinariamente lejos": "extraordinariamente longe",
    "bastante lejos": "bastante longe",
    "muy lejos": "muito longe",
    "lejos": "longe",
    "coste vagamente aumentado": "custo levemente aumentado",
    "coste ligeramente aumentado": "custo ligeiramente aumentado",
    "coste intermedio": "custo intermediário",
    "coste elevado": "custo elevado",
    "coste": "custo",
    "cortesano": "cortesão",
    "cortesanos": "cortesões",
    "cortesana": "cortesã",
    "cortesanas": "cortesãs",
    "situacion": "situação",
    "situación": "situação",
    "situaciones": "situações",
    "decision": "decisão",
    "decisión": "decisão",
    "decisiones": "decisões",
    "rechaza": "rejeita",
    "rechazar": "rejeitar",
    "rechazado": "rejeitado",
    "rechazada": "rejeitada",
    "consejo": "conselho",
    "deudas": "dívidas",
    "en la cárcel": "na prisão",
    "nota en el examen": "nota no exame",
    "con tierras": "com terras",
    "trueque": "escambo",
    "tu persona": "você",
    "a tu persona": "a você",
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
    "inmensamente": "imensamente",
    "ligeramente": "ligeiramente",
    "mínimamente": "minimamente",
    "minimamente": "minimamente",
    "muy": "muito",
    "pequeño": "pequeno",
    "pequeña": "pequena",
    "extraordinaria": "extraordinária",
}

DATE_REPLACEMENTS = {
    "Fecha:": "Data:",
    "fecha:": "data:",
    " d. C.": " d.C.",
    " d.C.": " d.C.",
    " a. C.": " a.C.",
    " a.C.": " a.C.",
}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_key(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def base_token_name(token: str) -> str:
    command_name = token[1:].split("(", 1)[0].split("|", 1)[0].strip()
    return command_name.split(".")[-1]


def is_literal_token_translatable(base_name: str) -> bool:
    return (
        base_name in {
            "Select_CString",
            "SelectLocalization",
            "LocalPlayerString",
            "PlayerString",
            "GetString",
        }
        or base_name.startswith("SelectLocalization")
        or base_name.endswith("String")
    )


def replace_safe_literals_visible(text: str) -> tuple[str, int]:
    result = text
    total = 0
    for source, target in sorted(SAFE_LITERAL_REPLACEMENTS.items(), key=lambda item: len(item[0]), reverse=True):
        pattern = re.compile(rf"(?<![A-Za-zÀ-ÿ]){re.escape(source)}(?![A-Za-zÀ-ÿ])", re.IGNORECASE)
        result, count = pattern.subn(target, result)
        total += count
    return result, total


def replace_date_literals(text: str) -> tuple[str, int]:
    result = text
    total = 0
    for source, target in DATE_REPLACEMENTS.items():
        count = result.count(source)
        if count:
            result = result.replace(source, target)
            total += count
    return result, total


def fix_string_literals_in_token(token: str) -> tuple[str, int]:
    if not (token.startswith("[") and token.endswith("]")):
        return token, 0

    base_name = base_token_name(token)
    literal_index = 0
    total = 0

    def repl(match: re.Match) -> str:
        nonlocal literal_index, total
        literal_index += 1
        quote = "'" if match.group(1) is not None else '"'
        value = match.group(1) if match.group(1) is not None else match.group(2)
        if value is None:
            return match.group(0)
        if local_quality_validator.is_technical_literal(value):
            return match.group(0)
        if base_name == "Concept" and literal_index == 1:
            return match.group(0)
        if base_name != "Concept" and not is_literal_token_translatable(base_name):
            return match.group(0)
        fixed, count = replace_safe_literals_visible(value)
        fixed, date_count = replace_date_literals(fixed)
        count += date_count
        if count:
            total += count
            return f"{quote}{fixed}{quote}"
        return match.group(0)

    fixed = STRING_LITERAL_PATTERN.sub(repl, token)
    return fixed, total


def replace_safe_literals_token_aware(text: str) -> tuple[str, int]:
    parts: list[str] = []
    total = 0
    position = 0
    for match in PROTECTED_TOKEN_PATTERN.finditer(text):
        outside = text[position : match.start()]
        outside, count = replace_safe_literals_visible(outside)
        outside, date_count = replace_date_literals(outside)
        total += count + date_count
        parts.append(outside)

        token, count = fix_string_literals_in_token(match.group(0))
        total += count
        parts.append(token)
        position = match.end()

    outside = text[position:]
    outside, count = replace_safe_literals_visible(outside)
    outside, date_count = replace_date_literals(outside)
    total += count + date_count
    parts.append(outside)
    return "".join(parts), total


def mechanical_fix(text: str, english_text: str | None = None) -> tuple[str, list[str]]:
    fixed = text or ""
    rules: list[str] = []

    translated = fixed.translate(SPANISH_INVERTED_PUNCTUATION)
    if translated != fixed:
        fixed = translated
        rules.append("remove_inverted_punctuation")

    quote_changed = False
    for source, target in ANGLED_QUOTES.items():
        if source in fixed:
            fixed = fixed.replace(source, target)
            quote_changed = True
    if quote_changed:
        rules.append("replace_angled_quotes")

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

    fixed2, count = replace_safe_literals_token_aware(fixed)
    if count:
        fixed = fixed2
        rules.append("safe_literal_replacement")

    return fixed, rules


def fetch_focus_packages(conn, focus_group: str, package_limit: int, path_like: str | None) -> list[dict]:
    params: list[object] = [focus_group]
    path_sql = ""
    if path_like:
        path_sql = "AND relative_path LIKE ?"
        params.append(path_like)
    params.append(package_limit)
    rows = conn.execute(
        f"""
        SELECT relative_path, priority_score, pending_segments, total_segments, confirmed_segments
        FROM package_focus_queue
        WHERE focus_group = ?
          AND status <> 'resolved'
          {path_sql}
        ORDER BY priority_score DESC, pending_segments DESC, relative_path
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_pending_segments(conn, package_paths: list[str], segment_limit: int | None) -> list[dict]:
    if not package_paths:
        return []
    placeholders = ",".join("?" for _ in package_paths)
    params: list[object] = list(package_paths)
    per_package_limit = None
    if segment_limit is not None:
        per_package_limit = max(1, segment_limit // len(package_paths))
    rows = conn.execute(
        f"""
        WITH pending AS (
            SELECT
                s.id AS segment_id,
                s.relative_path,
                s.source_line_number,
                s.source_key,
                s.english_text,
                s.spanish_text,
                s.old_text,
                ROW_NUMBER() OVER (
                    PARTITION BY s.relative_path
                    ORDER BY s.source_line_number, s.id
                ) AS package_row_number
            FROM source_segments s
            LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
            WHERE s.is_active = 1
              AND sc.segment_id IS NULL
              AND s.relative_path IN ({placeholders})
        )
        SELECT
            segment_id,
            relative_path,
            source_line_number,
            source_key,
            english_text,
            spanish_text,
            old_text
        FROM pending
        WHERE (? IS NULL OR package_row_number <= ?)
        ORDER BY relative_path, source_line_number, segment_id
        """,
        tuple(params + [per_package_limit, per_package_limit]),
    ).fetchall()
    return [dict(row) for row in rows]


def load_exact_memory(conn) -> dict[tuple[str, str], str]:
    rows = conn.execute(
        """
        SELECT s.english_text, s.spanish_text, sc.confirmed_text
        FROM segment_confirmations sc
        JOIN source_segments s ON s.id = sc.segment_id
        WHERE s.is_active = 1
          AND sc.confirmed_text IS NOT NULL
          AND LENGTH(COALESCE(s.english_text, '')) > 0
          AND LENGTH(COALESCE(s.spanish_text, '')) > 0
        ORDER BY sc.locked DESC, sc.confirmation_level = 'human_confirmed' DESC, sc.updated_at DESC
        """
    ).fetchall()
    memory: dict[tuple[str, str], str] = {}
    conflicts: set[tuple[str, str]] = set()
    for row in rows:
        key = (normalize_key(row["english_text"]), normalize_key(row["spanish_text"]))
        confirmed = row["confirmed_text"]
        existing = memory.get(key)
        if existing is None and key not in conflicts:
            memory[key] = confirmed
        elif existing != confirmed:
            memory.pop(key, None)
            conflicts.add(key)
    return memory


def validate_candidate(row: dict, text: str, allow_unchanged: bool, max_words: int) -> tuple[bool, dict, list[str]]:
    reasons: list[str] = []
    source_tokens = protected_tokens(row["spanish_text"])
    candidate_tokens = protected_tokens(text)
    if source_tokens != candidate_tokens:
        reasons.append("protected_tokens_mismatch")
        return False, {}, reasons

    quality = local_quality_validator.validate_text(text)
    if quality["high_issue_count"] or quality["medium_issue_count"]:
        reasons.append("validator_high_or_medium_issue")
        return False, quality, reasons
    if quality["auto_approval_blocked"]:
        reasons.append("validator_auto_blocked")
        return False, quality, reasons
    if quality["word_count"] > max_words:
        reasons.append("too_long_for_auto_package_fix")
        return False, quality, reasons
    if not allow_unchanged and text == (row["old_text"] or row["spanish_text"] or ""):
        reasons.append("no_change")
        return False, quality, reasons
    reasons.append("validator_clean")
    return True, quality, reasons


def is_event_narrative_row(row: dict) -> bool:
    path = row["relative_path"] or ""
    key = (row["source_key"] or "").casefold()
    if not path.startswith("event_localization/"):
        return False
    narrative_markers = (
        ".desc",
        "_desc",
        ".opening",
        "_opening",
        ".body",
        "_body",
        ".intro",
        "_intro",
        ".outro",
        "_outro",
    )
    return any(marker in key for marker in narrative_markers)


def is_event_localization_row(row: dict) -> bool:
    return (row["relative_path"] or "").startswith("event_localization/")


def choose_candidate(row: dict, memory: dict[tuple[str, str], str]) -> dict:
    base_text = row["old_text"] if row["old_text"] not in (None, "") else row["spanish_text"]
    key = (normalize_key(row["english_text"]), normalize_key(row["spanish_text"]))

    if key in memory:
        candidate = memory[key]
        accepted, quality, reasons = validate_candidate(row, candidate, allow_unchanged=True, max_words=120)
        return {
            "segment_id": row["segment_id"],
            "relative_path": row["relative_path"],
            "source_key": row["source_key"],
            "candidate_text": candidate,
            "rule": "exact_confirmed_reuse",
            "score": 0.997,
            "accepted": accepted,
            "quality": quality,
            "reasons": reasons,
        }

    fixed, rules = mechanical_fix(base_text, row["english_text"])
    if rules:
        if is_event_narrative_row(row):
            quality = local_quality_validator.validate_text(fixed)
            return {
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "candidate_text": fixed,
                "rule": "+".join(rules),
                "score": 0.0,
                "accepted": False,
                "quality": quality,
                "reasons": ["event_narrative_requires_specific_fix"],
            }
        accepted, quality, reasons = validate_candidate(row, fixed, allow_unchanged=False, max_words=70)
        return {
            "segment_id": row["segment_id"],
            "relative_path": row["relative_path"],
            "source_key": row["source_key"],
            "candidate_text": fixed,
            "rule": "+".join(rules),
            "score": 0.986,
            "accepted": accepted,
            "quality": quality,
            "reasons": reasons,
        }

    if is_event_localization_row(row):
        quality = local_quality_validator.validate_text(base_text)
        return {
            "segment_id": row["segment_id"],
            "relative_path": row["relative_path"],
            "source_key": row["source_key"],
            "candidate_text": base_text,
            "rule": "clean_short_existing",
            "score": 0.0,
            "accepted": False,
            "quality": quality,
            "reasons": ["event_localization_requires_specific_fix"],
        }

    accepted, quality, reasons = validate_candidate(row, base_text, allow_unchanged=True, max_words=18)
    return {
        "segment_id": row["segment_id"],
        "relative_path": row["relative_path"],
        "source_key": row["source_key"],
        "candidate_text": base_text,
        "rule": "clean_short_existing",
        "score": 0.982,
        "accepted": accepted,
        "quality": quality,
        "reasons": reasons,
    }


def apply_confirmations(conn, accepted: list[dict]) -> None:
    timestamp = now()
    conn.executemany(
        """
        INSERT INTO segment_confirmations (
            segment_id,
            confirmation_level,
            confirmed_text,
            confirmation_source,
            confirmation_label,
            locked,
            confidence_score,
            candidate_id,
            feedback_id,
            reviewer,
            confirmed_at,
            updated_at
        )
        VALUES (?, 'auto_confirmed', ?, 'package_autofix', ?, 0, ?, NULL, NULL, 'package_autofix', ?, ?)
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
                item["candidate_text"],
                item["rule"],
                item["score"],
                timestamp,
                timestamp,
                timestamp,
            )
            for item in accepted
        ],
    )


def refresh_focus_rows(conn, focus_group: str, package_paths: list[str]) -> None:
    timestamp = now()
    for path in package_paths:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_segments,
                SUM(CASE WHEN sc.segment_id IS NOT NULL THEN 1 ELSE 0 END) AS confirmed_segments,
                SUM(CASE WHEN sc.confirmation_level = 'human_confirmed' THEN 1 ELSE 0 END) AS human_confirmed_segments,
                SUM(CASE WHEN sc.confirmation_level = 'auto_confirmed' THEN 1 ELSE 0 END) AS auto_confirmed_segments
            FROM source_segments s
            LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
            WHERE s.is_active = 1
              AND s.relative_path = ?
            """,
            (path,),
        ).fetchone()
        total = int(row["total_segments"] or 0)
        confirmed = int(row["confirmed_segments"] or 0)
        pending = total - confirmed
        conn.execute(
            """
            UPDATE package_focus_queue
            SET total_segments = ?,
                confirmed_segments = ?,
                pending_segments = ?,
                human_confirmed_segments = ?,
                auto_confirmed_segments = ?,
                status = ?,
                updated_at = ?
            WHERE focus_group = ?
              AND relative_path = ?
            """,
            (
                total,
                confirmed,
                pending,
                int(row["human_confirmed_segments"] or 0),
                int(row["auto_confirmed_segments"] or 0),
                "resolved" if pending == 0 else "pending",
                timestamp,
                focus_group,
                path,
            ),
        )


def focus_summary(conn, focus_group: str) -> dict:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total_packages,
            SUM(CASE WHEN status = 'resolved' THEN 1 ELSE 0 END) AS resolved_packages,
            SUM(total_segments) AS total_segments,
            SUM(confirmed_segments) AS confirmed_segments,
            SUM(pending_segments) AS pending_segments
        FROM package_focus_queue
        WHERE focus_group = ?
        """,
        (focus_group,),
    ).fetchone()
    return dict(row)


def make_report(
    started_at: datetime,
    settings: dict,
    focus_group: str,
    packages: list[dict],
    candidates: list[dict],
    results: list[dict],
    apply: bool,
    summary: dict,
) -> list[str]:
    accepted = [item for item in results if item["accepted"]]
    blocked = [item for item in results if not item["accepted"]]
    by_rule = Counter(item["rule"] for item in accepted)
    blocked_reasons = Counter(reason for item in blocked for reason in item["reasons"])
    by_package = defaultdict(lambda: {"accepted": 0, "blocked": 0})
    for item in results:
        key = item["relative_path"]
        by_package[key]["accepted" if item["accepted"] else "blocked"] += 1

    total_packages = int(summary.get("total_packages") or 0)
    resolved_packages = int(summary.get("resolved_packages") or 0)
    resolved_pct = resolved_packages / total_packages * 100 if total_packages else 0
    elapsed = datetime.now() - started_at

    lines = [
        "Package autofix report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Focus group: {focus_group}",
        f"Mode: {'apply' if apply else 'dry-run'}",
        "",
        "Scope:",
        f"- Packages inspected: {len(packages)}",
        f"- Pending segments inspected: {len(candidates)}",
        "",
        "Result:",
        f"- Accepted candidates: {len(accepted)}",
        f"- Blocked candidates: {len(blocked)}",
        f"- Focus packages closed: {resolved_packages} / {total_packages} ({resolved_pct:.4f}%)",
        f"- Focus segments confirmed: {summary.get('confirmed_segments') or 0} / {summary.get('total_segments') or 0}",
        f"- Focus segments pending: {summary.get('pending_segments') or 0}",
        "",
        "Accepted by rule:",
        *[f"- {rule}: {count}" for rule, count in by_rule.most_common()],
        "",
        "Blocked reasons:",
        *[f"- {reason}: {count}" for reason, count in blocked_reasons.most_common()],
        "",
        "Package impact:",
        *[
            f"- accepted={counts['accepted']} blocked={counts['blocked']} | {path}"
            for path, counts in sorted(by_package.items(), key=lambda item: (-item[1]["accepted"], item[0]))
        ],
        "",
        "Accepted examples:",
        *[
            (
                f"- #{item['segment_id']} | {item['rule']} | {item['relative_path']} | "
                f"{item['source_key']} => {json.dumps(item['candidate_text'][:180], ensure_ascii=False)}"
            )
            for item in accepted[:20]
        ],
        "",
        "Blocked examples:",
        *[
            (
                f"- #{item['segment_id']} | {','.join(item['reasons'])} | {item['relative_path']} | "
                f"{item['source_key']}"
            )
            for item in blocked[:20]
        ],
    ]
    return lines


def main(
    focus_group: str = DEFAULT_FOCUS_GROUP,
    package_limit: int = 10,
    segment_limit: int | None = 1000,
    path_like: str | None = None,
    apply: bool = False,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()

    print("[package_autofix] Starting package autofix")
    print(f"[package_autofix] Rule version: {RULE_VERSION}")
    print(f"[package_autofix] Focus group: {focus_group}")
    print(f"[package_autofix] Package limit: {package_limit}")
    print(f"[package_autofix] Segment limit: {segment_limit or 'none'}")
    print(f"[package_autofix] Mode: {'apply' if apply else 'dry-run'}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        packages = fetch_focus_packages(conn, focus_group, package_limit, path_like)
        package_paths = [item["relative_path"] for item in packages]
        print(f"[package_autofix] Packages selected: {len(package_paths)}")
        for package in packages:
            print(
                "[package_autofix] Target: "
                f"pending={package['pending_segments']} / {package['total_segments']} | "
                f"{package['relative_path']}"
            )

        candidates = fetch_pending_segments(conn, package_paths, segment_limit)
        print(f"[package_autofix] Pending segments inspected: {len(candidates)}")
        memory = load_exact_memory(conn)
        print(f"[package_autofix] Exact confirmed memory keys: {len(memory)}")

        results = [choose_candidate(row, memory) for row in candidates]
        accepted = [item for item in results if item["accepted"]]
        print(f"[package_autofix] Accepted candidates: {len(accepted)}")
        print(f"[package_autofix] Blocked candidates: {len(results) - len(accepted)}")

        if apply and accepted:
            apply_confirmations(conn, accepted)
            refresh_focus_rows(conn, focus_group, package_paths)
            conn.commit()
            print(f"[package_autofix] Applied confirmations: {len(accepted)}")
        elif not apply:
            print("[package_autofix] Dry-run only; no database changes written")

        if not apply:
            refresh_focus_rows(conn, focus_group, package_paths)
        summary = focus_summary(conn, focus_group)

    report_lines = make_report(
        started_at,
        settings,
        focus_group,
        packages,
        candidates,
        results,
        apply,
        summary,
    )
    report_path = db.write_report(settings, "package_autofix", report_lines)
    print(f"[package_autofix] Report: {report_path}")
    print("[package_autofix] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Auto-fix high-impact package segments conservatively.")
    parser.add_argument("--focus-group", default=DEFAULT_FOCUS_GROUP)
    parser.add_argument("--package-limit", type=int, default=10)
    parser.add_argument("--segment-limit", type=int, default=1000)
    parser.add_argument("--path-like", default=None)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    main(
        focus_group=args.focus_group,
        package_limit=args.package_limit,
        segment_limit=args.segment_limit,
        path_like=args.path_like,
        apply=args.apply,
    )
