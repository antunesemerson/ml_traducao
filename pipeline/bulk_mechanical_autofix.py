from __future__ import annotations

import argparse
import re
from collections import Counter
from datetime import datetime
from typing import Any

import db
import local_quality_validator
from apply_safe_output_updates import protected_tokens
from pending_diagnostic import classify_bucket, fast_classify_item, fetch_pending_rows, sample_text


RULE_VERSION = "bulk_mechanical_autofix_v1"
DEFAULT_LABEL = "bulk_mechanical_autofix"
AUTO_SCORE = 0.965

INVERTED_PUNCTUATION = str.maketrans({"¿": "", "¡": ""})
MOJIBAKE_INVERTED_PUNCTUATION = ("Â¿", "Â¡")
ANGLED_QUOTES = {
    "«": '"',
    "»": '"',
    "Â«": '"',
    "Â»": '"',
}
GENDER_TOKEN_EXTRA_SUFFIX_PATTERN = re.compile(
    r"(\[[^\]]*Custom\(\s*['\"]ES_(?:OA|AO)['\"]\s*\)\])([ao])\b",
    re.IGNORECASE,
)
GENDER_TOKEN_JOINED_PATTERN = re.compile(
    r"(\[[^\]]*Custom\(\s*['\"]ES_(?:LeLa|LoLa|DelDela)['\"]\s*\)\])(?=[^\W\d_])",
    re.IGNORECASE,
)
STYLE_TOKEN_JOINED_TO_WORD_PATTERN = re.compile(r"(#!|\$[A-Za-z0-9_]+\$)(?=[^\W\d_])")
BRACKET_TOKEN_JOINED_TO_WORD_PATTERN = re.compile(
    r"(\](?![aos]\b|as\b|os\b))(?=[^\W\d_])",
    re.IGNORECASE,
)
WORD_JOINED_TO_STYLE_PATTERN = re.compile(r"(?<=[^\W\d_])(\$[A-Za-z0-9_]+\$|#(?:EMP|V|P|N|bold|weak)\b)")
SPACE_BEFORE_PUNCTUATION_PATTERN = re.compile(r"\s+([,.;:!?])")
EXTRA_SPANISH_RESIDUE_PATTERN = re.compile(
    r"\b("
    r"sin|posesi[oó]n|superar[eé]|llama|sab[ií]a|malnacid[ao]?|"
    r"lo|la|los|las|una|uno|unos|unas|este|esta|estos|estas|"
    r"aqu[eé]l|aquella|aquellos|aquellas|"
    r"pues|pero|porque|aunque|mientras|durante|desde"
    r")\b",
    re.IGNORECASE,
)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def percent(part: int, total: int) -> float:
    if total == 0:
        return 0.0
    return part / total * 100


def mechanical_fix(text: str) -> tuple[str, list[str]]:
    fixed = text
    rules: list[str] = []

    translated = fixed.translate(INVERTED_PUNCTUATION)
    for marker in MOJIBAKE_INVERTED_PUNCTUATION:
        translated = translated.replace(marker, "")
    if translated != fixed:
        fixed = translated
        rules.append("remove_inverted_punctuation")

    for old, new in ANGLED_QUOTES.items():
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

    fixed2, count = STYLE_TOKEN_JOINED_TO_WORD_PATTERN.subn(r"\1 ", fixed)
    fixed3, count2 = BRACKET_TOKEN_JOINED_TO_WORD_PATTERN.subn(r"\1 ", fixed2)
    if count + count2:
        fixed = fixed3
        rules.append("space_after_token")

    fixed2, count = WORD_JOINED_TO_STYLE_PATTERN.subn(r" \1", fixed)
    if count:
        fixed = fixed2
        rules.append("space_before_token")

    fixed2, count = SPACE_BEFORE_PUNCTUATION_PATTERN.subn(r"\1", fixed)
    if count:
        fixed = fixed2
        rules.append("remove_space_before_punctuation")

    return fixed, rules


def can_accept(original_item: dict[str, Any], fixed_text: str) -> tuple[bool, dict[str, Any], list[str]]:
    reasons: list[str] = []
    if fixed_text == original_item["candidate_text"]:
        return False, original_item, ["no_change"]
    if protected_tokens(original_item["candidate_text"]) != protected_tokens(fixed_text):
        return False, original_item, ["candidate_token_mismatch"]
    if protected_tokens(original_item.get("spanish_text")) != protected_tokens(fixed_text):
        return False, original_item, ["source_token_mismatch"]

    recheck_row = dict(original_item)
    recheck_row["old_text"] = fixed_text
    recheck_row["has_old"] = 1
    rechecked = fast_classify_item(recheck_row)
    if rechecked["token_status"] != "ok":
        reasons.append("recheck_token_not_ok")
    if rechecked["issue_count"]:
        reasons.append("remaining_issues")
    if classify_bucket(rechecked) != "likely_confirmable":
        reasons.append(f"recheck_bucket:{classify_bucket(rechecked)}")
    quality = local_quality_validator.validate_text(fixed_text)
    if quality["issue_count"]:
        reasons.append("local_quality_issues")
    if EXTRA_SPANISH_RESIDUE_PATTERN.search(fixed_text):
        reasons.append("extra_spanish_residue")

    return not reasons, rechecked, reasons


def build_candidates(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Counter[str]]:
    accepted: list[dict[str, Any]] = []
    rejects: Counter[str] = Counter()
    for row in rows:
        item = fast_classify_item(row)
        bucket = classify_bucket(item)
        if bucket not in {"mechanical_autofix"}:
            continue
        fixed_text, rules = mechanical_fix(item["candidate_text"])
        ok, rechecked, reasons = can_accept({**item, "spanish_text": row.get("spanish_text")}, fixed_text)
        if not ok:
            for reason in reasons:
                rejects[reason] += 1
            continue
        accepted.append({**rechecked, "candidate_text": fixed_text, "rules": rules})
    return accepted, rejects


def apply_confirmations(conn, candidates: list[dict[str, Any]], reviewer: str, label: str) -> None:
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
        VALUES (?, 'auto_confirmed', ?, ?, ?, 0, ?, NULL, NULL, ?, ?, ?)
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
                "bulk_mechanical_autofix",
                label,
                AUTO_SCORE,
                reviewer,
                timestamp,
                timestamp,
                timestamp,
            )
            for item in candidates
        ],
    )


def build_report_lines(
    started_at: datetime,
    elapsed,
    apply: bool,
    inspected: int,
    candidates: list[dict[str, Any]],
    rejects: Counter[str],
    limit: int | None,
    sample_limit: int,
) -> list[str]:
    package_counts = Counter(item["relative_path"] for item in candidates)
    rule_counts: Counter[str] = Counter()
    for item in candidates:
        rule_counts.update(item.get("rules") or [])
    lines = [
        "Bulk mechanical autofix report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Apply: {apply}",
        f"Limit: {limit or 'none'}",
        "",
        "Summary:",
        f"- Pending inspected: {inspected}",
        f"- Auto-fix confirmations selected: {len(candidates)} ({percent(len(candidates), inspected):.2f}%)",
        f"- Confirmations written: {len(candidates) if apply else 0}",
        "",
        "Rules:",
        *[f"- {rule}: {count}" for rule, count in rule_counts.most_common()],
        "",
        "Rejected reasons:",
        *[f"- {reason}: {count}" for reason, count in rejects.most_common()],
        "",
        "Top packages:",
        *[f"- {path}: {count}" for path, count in package_counts.most_common(40)],
        "",
        "Samples:",
    ]
    for item in candidates[:sample_limit]:
        rules = ",".join(item.get("rules") or [])
        lines.append(
            f"- segment {item['segment_id']} | {rules} | "
            f"{item['relative_path']}::{item['source_key']} | {sample_text(item['candidate_text'], 180)}"
        )
    if not candidates:
        lines.append("- No candidates selected")
    return lines


def main(limit: int | None = None, sample_limit: int = 50, apply: bool = False) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[bulk_mechanical_autofix] Starting bulk mechanical autofix")
    print(f"[bulk_mechanical_autofix] Rule version: {RULE_VERSION}")
    print(f"[bulk_mechanical_autofix] Apply: {apply}")
    print(f"[bulk_mechanical_autofix] Limit: {limit or 'none'}")
    print(f"[bulk_mechanical_autofix] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        rows = fetch_pending_rows(conn, limit, None)
        candidates, rejects = build_candidates(rows)
        if apply:
            apply_confirmations(conn, candidates, reviewer="bulk_auto", label=DEFAULT_LABEL)
            conn.commit()

    elapsed = datetime.now() - started_at
    lines = build_report_lines(
        started_at=started_at,
        elapsed=elapsed,
        apply=apply,
        inspected=len(rows),
        candidates=candidates,
        rejects=rejects,
        limit=limit,
        sample_limit=sample_limit,
    )
    report_path = db.write_report(settings, "bulk_mechanical_autofix", lines)

    print(f"[bulk_mechanical_autofix] Pending inspected: {len(rows)}")
    print(f"[bulk_mechanical_autofix] Auto-fix confirmations selected: {len(candidates)}")
    print(f"[bulk_mechanical_autofix] Confirmations written: {len(candidates) if apply else 0}")
    print(f"[bulk_mechanical_autofix] Report: {report_path}")
    print("[bulk_mechanical_autofix] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bulk-apply strict mechanical localization fixes.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum pending rows inspected.")
    parser.add_argument("--sample-limit", type=int, default=50, help="Preview sample rows in report.")
    parser.add_argument("--apply", action="store_true", help="Write auto confirmations.")
    args = parser.parse_args()
    main(limit=args.limit, sample_limit=args.sample_limit, apply=args.apply)
