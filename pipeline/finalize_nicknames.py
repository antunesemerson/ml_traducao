from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime

import db
import local_quality_validator


RULE_VERSION = "finalize_nicknames_v1"
DEFAULT_LABEL = "nickname_mechanical_finalize"
AUTO_SCORE = 0.985

TOKEN_PATTERN = re.compile(r"\$[^$\s]+\$|\[[^\]]+\]|#[A-Za-z0-9_]+|#!|@[A-Za-z0-9_]+!|\\n")
STRING_LITERAL_PATTERN = re.compile(r"'[^']*'|\"[^\"]*\"")
QUOTED_LITERAL_PATTERN = re.compile(r"'([^']*)'|\"([^\"]*)\"")
SPANISH_PUNCTUATION = str.maketrans({"¿": "", "¡": ""})
ANGLED_QUOTES = {"«": '"', "»": '"'}

STYLE_TOKEN_JOINED_TO_WORD_PATTERN = re.compile(r"(#!|\$[A-Za-z0-9_]+\$)(?=[^\W\d_])")
BRACKET_TOKEN_JOINED_TO_WORD_PATTERN = re.compile(
    r"(\](?![aos]\b|as\b|os\b))(?=[^\W\d_])",
    re.IGNORECASE,
)
WORD_JOINED_TO_STYLE_PATTERN = re.compile(r"(?<=[^\W\d_])(\$[A-Za-z0-9_]+\$|#(?:EMP|V|P|N|bold|weak)\b)")
GENDER_TOKEN_EXTRA_SUFFIX_PATTERN = re.compile(
    r"(\[[^\]]*Custom\(\s*['\"]ES_(?:OA|AO)['\"]\s*\)\])([ao])\b",
    re.IGNORECASE,
)

LOCAL_PLAYER_REPLACEMENTS = [
    (
        re.compile(r"\[Select_CString\(\s*CHARACTER\.IsLocalPlayer\s*,\s*'tu'\s*,\s*'su'\s*\)(\|[^\]]+)?\]", re.I),
        "[Select_CString( CHARACTER.IsLocalPlayer, 'seu', 'seu' )\\1]",
    ),
    (
        re.compile(r"\[Select_CString\(\s*CHARACTER\.IsLocalPlayer\s*,\s*'Tu'\s*,\s*'Su'\s*\)(\|[^\]]+)?\]", re.I),
        "[Select_CString( CHARACTER.IsLocalPlayer, 'Seu', 'Seu' )\\1]",
    ),
    (
        re.compile(r"\[Select_CString\(\s*CHARACTER\.IsLocalPlayer\s*,\s*'tus'\s*,\s*'sus'\s*\)(\|[^\]]+)?\]", re.I),
        "[Select_CString( CHARACTER.IsLocalPlayer, 'seus', 'seus' )\\1]",
    ),
    (
        re.compile(r"\[Select_CString\(\s*CHARACTER\.IsLocalPlayer\s*,\s*'Tus'\s*,\s*'Sus'\s*\)(\|[^\]]+)?\]", re.I),
        "[Select_CString( CHARACTER.IsLocalPlayer, 'Seus', 'Seus' )\\1]",
    ),
    (
        re.compile(r"\[Select_CString\(\s*CHARACTER\.IsLocalPlayer\s*,\s*'eres'\s*,\s*'es'\s*\)(\|[^\]]+)?\]", re.I),
        "[Select_CString( CHARACTER.IsLocalPlayer, 'é', 'é' )\\1]",
    ),
    (
        re.compile(r"\[Select_CString\(\s*CHARACTER\.IsLocalPlayer\s*,\s*'ganaste'\s*,\s*'ganó'\s*\)(\|[^\]]+)?\]", re.I),
        "[Select_CString( CHARACTER.IsLocalPlayer, 'ganhou', 'ganhou' )\\1]",
    ),
    (
        re.compile(r"\[Select_CString\(\s*CHARACTER\.IsLocalPlayer\s*,\s*'recibes'\s*,\s*'recibe'\s*\)(\|[^\]]+)?\]", re.I),
        "[Select_CString( CHARACTER.IsLocalPlayer, 'recebe', 'recebe' )\\1]",
    ),
    (
        re.compile(r"\[Select_CString\(\s*CHARACTER\.IsLocalPlayer\s*,\s*'pusiste'\s*,\s*'puso'\s*\)(\|[^\]]+)?\]", re.I),
        "[Select_CString( CHARACTER.IsLocalPlayer, 'pôs', 'pôs' )\\1]",
    ),
    (
        re.compile(r"\[Select_CString\(\s*CHARACTER\.IsLocalPlayer\s*,\s*'respondes'\s*,\s*'responde'\s*\)(\|[^\]]+)?\]", re.I),
        "[Select_CString( CHARACTER.IsLocalPlayer, 'responde', 'responde' )\\1]",
    ),
    (
        re.compile(
            r"\[Select_CString\(\s*CHARACTER\.IsLocalPlayer\s*,\s*'se te concedió'\s*,\s*'se le concedió'\s*\)(\|[^\]]+)?\]",
            re.I,
        ),
        "[Select_CString( CHARACTER.IsLocalPlayer, 'recebeu', 'recebeu' )\\1]",
    ),
]

SAFE_LITERAL_REPLACEMENTS = {
    "venganza": "vingança",
    "agradable": "agradável",
    "alabanzas": "louvores",
    "mirada": "olhar",
    "patrono": "patrono",
    "epónimo": "epônimo",
    "eponimo": "epônimo",
    "Cósmico[CHARACTER.Custom('ES_OA')]": "Cósmic[CHARACTER.Custom('ES_OA')]",
    "Cosmico[CHARACTER.Custom('ES_OA')]": "Cosmic[CHARACTER.Custom('ES_OA')]",
}

BLOCKING_SPANISH_RE = re.compile(
    r"[¿¡«»]|"
    r"\b("
    r"tu|tus|su|sus|te|le|ti|tú|eres|has|ha|ganaste|ganó|recibes|recibe|pusiste|puso|"
    r"derrocaste|derrocó|luchaste|luchó|mataste|mató|matarás|matará|asumiste|asumió|"
    r"llegaste|llegó|entiendes|entiende|tienes|tiene|olvides|olvide|das|da|mantendrás|mantendrá|"
    r"abandonaste|abandonó|consideras|considera|caes|cae|abusaste|abusó|"
    r"venganza|agradable|alabanzas|bellac[oa]s?|poetisa|poeta|mirada|epónimo|eponimo|"
    r"señor|señora|llaman|concedió|son"
    r")\b|"
    r"tu persona",
    re.IGNORECASE,
)

UNSAFE_SELECT_RE = re.compile(
    r"\[Select_CString\(\s*CHARACTER\.IsLocalPlayer\s*,\s*'(?:te|le|ti|has|ha|tu persona)",
    re.IGNORECASE,
)
LOCAL_PLAYER_DUPLICATED_VERB_RE = re.compile(
    r"\[Select_CString\(\s*CHARACTER\.IsLocalPlayer\s*,\s*'(é|pôs|recebe|responde|ganhou|recebeu)'\s*,\s*'\1'\s*\)(?:\|[^\]]+)?\]\s+\1\b",
    re.IGNORECASE,
)

LOCAL_PLAYER_TOKEN_RE = re.compile(r"\[Select_CString\(\s*CHARACTER\.IsLocalPlayer\b[^\]]+\]", re.IGNORECASE)
ALLOWED_LOCAL_PLAYER_LITERALS = {
    "seu",
    "sua",
    "seus",
    "suas",
    "Seu",
    "Seus",
    "é",
    "ganhou",
    "recebe",
    "pôs",
    "responde",
    "recebeu",
}

FEMININE_POSSESSIVE_NOUNS = (
    "atitude",
    "fama",
    "forma",
    "ira",
    "opinião",
    "pontualidade",
    "rebelião",
    "vida",
)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalized_token_signature(text: str) -> list[str]:
    signature: list[str] = []
    for match in TOKEN_PATTERN.finditer(text or ""):
        token = match.group(0)
        if token.startswith("["):
            token = STRING_LITERAL_PATTERN.sub("''", token)
            token = re.sub(r"\s+", "", token)
        signature.append(token)
    return signature


def structural_signature(text: str) -> list[str]:
    signature: list[str] = []
    for match in TOKEN_PATTERN.finditer(text or ""):
        token = match.group(0)
        if token.startswith("["):
            name = token[1:].split("(", 1)[0].split("|", 1)[0].strip()
            literal_count = len(STRING_LITERAL_PATTERN.findall(token))
            pipe_suffix = token.rsplit("|", 1)[1] if "|" in token else ""
            signature.append(f"[{name}|literals={literal_count}|suffix={pipe_suffix}")
        else:
            signature.append(token)
    return signature


def fetch_candidates(conn, limit: int | None) -> list[dict]:
    params: list[object] = []
    limit_clause = ""
    if limit is not None:
        limit_clause = "LIMIT ?"
        params.append(limit)
    rows = conn.execute(
        f"""
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text
        FROM source_segments s
        JOIN finalization_queue q ON q.segment_id = s.id
        LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
        WHERE s.is_active = 1
          AND s.relative_path = 'nicknames_l_spanish.yml'
          AND q.closure_bucket = 'nicknames_batch'
          AND sc.segment_id IS NULL
        ORDER BY s.source_line_number, s.id
        {limit_clause}
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def normalize_spacing(text: str) -> tuple[str, list[str]]:
    fixed = text
    rules: list[str] = []
    fixed2, count = STYLE_TOKEN_JOINED_TO_WORD_PATTERN.subn(r"\1 ", fixed)
    if count:
        fixed = fixed2
        rules.append("space_after_style_token")
    fixed2, count = BRACKET_TOKEN_JOINED_TO_WORD_PATTERN.subn(r"\1 ", fixed)
    if count:
        fixed = fixed2
        rules.append("space_after_bracket_token")
    fixed2, count = WORD_JOINED_TO_STYLE_PATTERN.subn(r" \1", fixed)
    if count:
        fixed = fixed2
        rules.append("space_before_style_token")
    fixed2, count = re.subn(r"\s+([,.;:!?])", r"\1", fixed)
    if count:
        fixed = fixed2
        rules.append("remove_space_before_punctuation")
    fixed2, count = re.subn(r" {2,}", " ", fixed)
    if count:
        fixed = fixed2
        rules.append("collapse_spaces")
    return fixed, rules


def transform_text(text: str) -> tuple[str, list[str]]:
    fixed = text or ""
    rules: list[str] = []

    translated = fixed.translate(SPANISH_PUNCTUATION)
    if translated != fixed:
        fixed = translated
        rules.append("remove_inverted_punctuation")

    for old, new in ANGLED_QUOTES.items():
        if old in fixed:
            fixed = fixed.replace(old, new)
            rules.append("replace_angled_quotes")

    for pattern, replacement in LOCAL_PLAYER_REPLACEMENTS:
        fixed, count = pattern.subn(replacement, fixed)
        if count:
            rules.append(f"local_player_string:{replacement}")

    for source, target in sorted(SAFE_LITERAL_REPLACEMENTS.items(), key=lambda item: len(item[0]), reverse=True):
        fixed2, count = re.subn(rf"(?<![A-Za-zÀ-ÿ]){re.escape(source)}(?![A-Za-zÀ-ÿ])", target, fixed, flags=re.I)
        if count:
            fixed = fixed2
            rules.append(f"literal:{source}")

    for noun in FEMININE_POSSESSIVE_NOUNS:
        pattern = re.compile(
            rf"\[Select_CString\(\s*CHARACTER\.IsLocalPlayer\s*,\s*'seu'\s*,\s*'seu'\s*\)(\|[^\]]+)?\]\s*{re.escape(noun)}\b",
            re.IGNORECASE,
        )
        fixed2, count = pattern.subn(
            rf"[Select_CString( CHARACTER.IsLocalPlayer, 'sua', 'sua' )\1] {noun}",
            fixed,
        )
        if count:
            fixed = fixed2
            rules.append(f"possessive_feminine:{noun}")

    fixed2, count = GENDER_TOKEN_EXTRA_SUFFIX_PATTERN.subn(r"\1", fixed)
    if count:
        fixed = fixed2
        rules.append("remove_gender_token_extra_suffix")

    fixed, spacing_rules = normalize_spacing(fixed)
    rules.extend(spacing_rules)
    return fixed, rules


def validate(row: dict, candidate: str, rules: list[str]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not rules or candidate == (row["old_text"] or ""):
        reasons.append("no_change")
    if UNSAFE_SELECT_RE.search(candidate):
        reasons.append("unsafe_local_player_select")
    if LOCAL_PLAYER_DUPLICATED_VERB_RE.search(candidate):
        reasons.append("duplicated_local_player_verb")
    for match in LOCAL_PLAYER_TOKEN_RE.finditer(candidate):
        literals = [
            group1 if group1 is not None else group2
            for group1, group2 in QUOTED_LITERAL_PATTERN.findall(match.group(0))
        ]
        if any(literal not in ALLOWED_LOCAL_PLAYER_LITERALS for literal in literals):
            reasons.append("untranslated_local_player_literal")
            break
    if structural_signature(row["spanish_text"] or "") != structural_signature(candidate):
        reasons.append("token_signature_mismatch")
    if BLOCKING_SPANISH_RE.search(candidate):
        reasons.append("targeted_spanish_residue")
    quality = local_quality_validator.validate_text(candidate)
    if quality["high_issue_count"]:
        reasons.append("validator_high_issue")
    if quality["auto_approval_blocked"]:
        reasons.append("validator_auto_blocked")
    if len(candidate.split()) > 90:
        reasons.append("too_long_for_nickname_auto")
    return not reasons, reasons


def build_results(rows: list[dict]) -> list[dict]:
    results: list[dict] = []
    for row in rows:
        candidate, rules = transform_text(row["old_text"] or row["spanish_text"] or "")
        accepted, reasons = validate(row, candidate, rules)
        results.append(
            {
                **row,
                "candidate_text": candidate,
                "rules": rules,
                "accepted": accepted,
                "reasons": reasons,
            }
        )
    return results


def apply_confirmations(conn, accepted: list[dict]) -> None:
    timestamp = now()
    conn.executemany(
        """
        INSERT INTO segment_confirmations (
            segment_id, confirmation_level, confirmed_text, confirmation_source,
            confirmation_label, locked, confidence_score, candidate_id, feedback_id,
            reviewer, confirmed_at, updated_at
        )
        VALUES (?, 'auto_confirmed', ?, 'finalize_nicknames', ?, 0, ?, NULL, NULL, 'finalize_nicknames', ?, ?)
        ON CONFLICT(segment_id) DO UPDATE SET
            confirmation_level = CASE WHEN locked = 1 THEN confirmation_level ELSE 'auto_confirmed' END,
            confirmed_text = CASE WHEN locked = 1 THEN confirmed_text ELSE excluded.confirmed_text END,
            confirmation_source = CASE WHEN locked = 1 THEN confirmation_source ELSE excluded.confirmation_source END,
            confirmation_label = CASE WHEN locked = 1 THEN confirmation_label ELSE excluded.confirmation_label END,
            confidence_score = CASE WHEN locked = 1 THEN confidence_score ELSE excluded.confidence_score END,
            reviewer = CASE WHEN locked = 1 THEN reviewer ELSE excluded.reviewer END,
            updated_at = ?
        """,
        [
            (
                item["segment_id"],
                item["candidate_text"],
                DEFAULT_LABEL,
                AUTO_SCORE,
                timestamp,
                timestamp,
                timestamp,
            )
            for item in accepted
        ],
    )


def make_report(started_at: datetime, rows: list[dict], results: list[dict], apply: bool) -> list[str]:
    accepted = [item for item in results if item["accepted"]]
    blocked = [item for item in results if not item["accepted"]]
    rule_counts = Counter(rule for item in accepted for rule in item["rules"])
    block_counts = Counter(reason for item in blocked for reason in item["reasons"])
    elapsed = datetime.now() - started_at
    return [
        "Finalize nicknames report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Mode: {'apply' if apply else 'dry-run'}",
        "",
        "Summary:",
        f"- Pending inspected: {len(rows)}",
        f"- Accepted: {len(accepted)}",
        f"- Blocked: {len(blocked)}",
        "",
        "Accepted rules:",
        *[f"- {rule}: {count}" for rule, count in rule_counts.most_common()],
        "",
        "Blocked reasons:",
        *[f"- {reason}: {count}" for reason, count in block_counts.most_common()],
        "",
        "Accepted examples:",
        *[
            (
                f"- #{item['segment_id']} | {item['source_key']} | "
                f"{json.dumps(item['candidate_text'][:220], ensure_ascii=False)}"
            )
            for item in accepted[:25]
        ],
        "",
        "Blocked examples:",
        *[
            (
                f"- #{item['segment_id']} | {item['source_key']} | "
                f"{','.join(item['reasons'])} | {json.dumps(item['candidate_text'][:220], ensure_ascii=False)}"
            )
            for item in blocked[:30]
        ],
    ]


def main(limit: int | None = None, apply: bool = False) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[finalize_nicknames] Starting nickname finalization")
    print(f"[finalize_nicknames] Rule version: {RULE_VERSION}")
    print(f"[finalize_nicknames] Mode: {'apply' if apply else 'dry-run'}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        rows = fetch_candidates(conn, limit)
        results = build_results(rows)
        accepted = [item for item in results if item["accepted"]]
        blocked = [item for item in results if not item["accepted"]]
        print(f"[finalize_nicknames] Pending inspected: {len(rows)}")
        print(f"[finalize_nicknames] Accepted: {len(accepted)}")
        print(f"[finalize_nicknames] Blocked: {len(blocked)}")
        if apply and accepted:
            apply_confirmations(conn, accepted)
            conn.commit()
            print(f"[finalize_nicknames] Applied confirmations: {len(accepted)}")
        elif not apply:
            print("[finalize_nicknames] Dry-run only; no database changes written")

    report_lines = make_report(started_at, rows, results, apply)
    report_path = db.write_report(settings, "finalize_nicknames", report_lines)
    print(f"[finalize_nicknames] Report: {report_path}")
    print("[finalize_nicknames] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Finalize safe nickname localization residues.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    main(limit=args.limit, apply=args.apply)
