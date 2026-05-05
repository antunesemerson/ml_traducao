from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher

import db


RULE_VERSION = "segment_quality_v5"
BATCH_SIZE = 5000

SPANISH_RESIDUE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"\bgalard[o\u00f3]n(?:es)?\b",
        r"\bdisponible(?:s)?\b",
        r"\bno puedes\b",
        r"\bpuedes\b",
        r"\btiene(?:s|n)?\b",
        r"\bsea\b",
        r"\bson\b",
        r"\bya\b",
        r"\belegir\b",
        r"\belige\b",
        r"\bnuevo\b",
        r"\bnueva\b",
        r"\bnombrar\b",
        r"\baumentar[a\u00e1]\b",
        r"\bpartida(?:s)?\b",
        r"\bguardar\b",
        r"\bcargar\b",
        r"\bborrar\b",
        r"\bfichero\b",
        r"\bjugador(?:es)?\b",
        r"\bvasallo(?:s)?\b",
        r"\bheredero(?:s)?\b",
        r"\bconsejo\b",
        r"\bse\u00f1or(?:a|es|as)?\b",
        r"\breino(?:s)?\b",
        r"\bguerra(?:s)?\b",
        r"\bdel\b",
        r"\bal\b",
        r"\bun\b",
        r"\buna\b",
        r"\blos\b",
        r"\blas\b",
    ]
]

SPANISH_ACCENT_PATTERN = re.compile(r"[\u00f1\u00bf\u00a1]")
WORD_PATTERN = re.compile(r"[A-Za-z\u00c0-\u00ff]+", re.UNICODE)
PROTECTED_TOKEN_PATTERN = re.compile(
    r"\$[^$\s]+\$|\[[^\]]+\]|#[A-Za-z0-9_]+|#!|@[A-Za-z0-9_]+!|\\n"
)
LOCALIZATION_COMMAND_PATTERN = re.compile(r"\[[A-Za-z0-9_.$|:()'\" /-]+\]")
MACRO_ONLY_PATTERN = re.compile(r"^[\s$A-Z0-9_|\[\].:#@!\\/-]+$")
PORTUGUESE_HINT_PATTERN = re.compile(
    r"[\u00e3\u00f5\u00e7]|\b(do|da|dos|das|ao|aos|uma|para|com|voc\u00ea|"
    r"seu|sua|seus|suas|est\u00e1|est\u00e3o|tem|tenha|pode|podem|"
    r"escolha|criar|crie|aumenta|t\u00edtulo|honor\u00edfico|ex\u00e9rcito|"
    r"dispon\u00edveis|aclama\u00e7\u00e3o|distin\u00e7\u00f5es)\b",
    re.IGNORECASE,
)


def normalize_for_compare(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.casefold().split())


def is_blank(value: str | None) -> bool:
    return value is None or value.strip() == ""


def similarity(left: str | None, right: str | None) -> float:
    left_norm = normalize_for_compare(left)
    right_norm = normalize_for_compare(right)
    if not left_norm and not right_norm:
        return 1.0
    if not left_norm or not right_norm:
        return 0.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def protected_tokens(value: str | None) -> Counter:
    if not value:
        return Counter()
    return Counter(PROTECTED_TOKEN_PATTERN.findall(value))


def strip_protected_tokens(value: str | None) -> str:
    if not value:
        return ""
    stripped = PROTECTED_TOKEN_PATTERN.sub(" ", value)
    stripped = re.sub(r"[$#@!|_:\[\].()0-9\\/-]+", " ", stripped)
    return " ".join(stripped.split())


def word_count(value: str | None) -> int:
    return len(WORD_PATTERN.findall(value or ""))


def plain_text(value: str | None) -> str:
    return strip_protected_tokens(value)


def has_portuguese_hint(value: str | None) -> bool:
    return bool(value and PORTUGUESE_HINT_PATTERN.search(value))


def is_technical_segment(value: str | None) -> bool:
    if not value:
        return False
    normalized = normalize_for_compare(value)
    human_text = strip_protected_tokens(value)
    if not human_text:
        return True
    if MACRO_ONLY_PATTERN.match(value):
        return True
    if word_count(human_text) <= 2 and (
        "$" in value
        or "[" in value
        or "#" in value
        or "@" in value
        or LOCALIZATION_COMMAND_PATTERN.search(value)
    ):
        return True
    if normalized.startswith("#t ") or normalized.startswith("#s "):
        return True
    return False


def is_human_translatable_segment(value: str | None) -> bool:
    if not value:
        return False
    return not is_technical_segment(value) and word_count(strip_protected_tokens(value)) >= 2


def is_short_human_segment(value: str | None) -> bool:
    text = plain_text(value)
    return bool(text) and word_count(text) <= 3 and len(text) <= 40


def is_proper_name_like(value: str | None) -> bool:
    text = plain_text(value)
    if not text:
        return False
    words = WORD_PATTERN.findall(text)
    if not words or len(words) > 5:
        return False
    lowercase_words = [word for word in words if word[:1].islower()]
    return len(lowercase_words) == 0


def spanish_marker_count(value: str | None) -> int:
    if not value:
        return 0
    count = sum(1 for pattern in SPANISH_RESIDUE_PATTERNS if pattern.search(value))
    if SPANISH_ACCENT_PATTERN.search(value):
        count += 2
    return count


def text_length_ratio(source: str | None, target: str | None) -> float:
    source_len = len(source or "")
    target_len = len(target or "")
    if source_len == 0 and target_len == 0:
        return 1.0
    if source_len == 0 or target_len == 0:
        return 0.0
    shorter = min(source_len, target_len)
    longer = max(source_len, target_len)
    return shorter / longer


def classify(score: float) -> str:
    if score >= 0.9:
        return "trusted"
    if score >= 0.75:
        return "review_light"
    if score >= 0.5:
        return "review_needed"
    return "rejected"


def analyze_row(row) -> tuple[float, str, list[dict]]:
    spanish_text = row["spanish_text"]
    english_text = row["english_text"]
    old_text = row["old_text"]
    portuguese_text = row["portuguese_text"]
    approved_text = row["approved_text"] if "approved_text" in row.keys() else None

    candidate = old_text
    reasons: list[dict] = []
    score = 1.0

    spanish_blank = is_blank(spanish_text)
    english_blank = is_blank(english_text)
    old_blank = is_blank(old_text)
    output_blank = is_blank(portuguese_text)

    if spanish_blank and old_blank and output_blank:
        reasons.append(
            {
                "rule": "empty_segment_preserved",
                "weight": 0,
                "message": "Spanish source, old translation, and output are empty; segment is structurally preserved.",
            }
        )
        if not english_blank:
            reasons.append(
                {
                    "rule": "empty_spanish_with_english_reference",
                    "weight": 0,
                    "message": "English has reference text, but Spanish source is empty; Spanish source remains the structural mirror.",
                }
            )
        return 1.0, "trusted", reasons

    if not spanish_blank and output_blank:
        reasons.append(
            {
                "rule": "empty_output_text",
                "weight": -0.4,
                "message": "Output Portuguese text is empty while Spanish source has content.",
            }
        )
        if not old_blank:
            return 0.6, "review_needed", reasons

    # Spanish source is the structural mirror for the mod. English is only a
    # semantic reference for human-readable text.
    source_tokens = protected_tokens(spanish_text)
    old_tokens = protected_tokens(candidate)
    output_tokens = protected_tokens(portuguese_text)
    technical_segment = is_technical_segment(spanish_text)
    human_segment = is_human_translatable_segment(spanish_text)
    short_human_segment = is_short_human_segment(spanish_text)
    proper_name_like = is_proper_name_like(spanish_text)

    if (
        not is_blank(approved_text)
        and not is_blank(portuguese_text)
        and normalize_for_compare(approved_text) == normalize_for_compare(portuguese_text)
        and source_tokens == output_tokens
    ):
        reasons.append(
            {
                "rule": "manual_feedback_applied",
                "weight": 0,
                "message": "Current output matches accepted or edited human feedback and preserves Spanish source tokens.",
            }
        )
        return 1.0, "trusted", reasons

    if old_blank:
        score -= 0.65
        reasons.append(
            {
                "rule": "empty_old_text",
                "weight": -0.65,
                "message": "Old translation is empty or missing.",
            }
        )

    if technical_segment:
        if source_tokens == old_tokens:
            reasons.append(
                {
                    "rule": "technical_segment_preserved",
                    "weight": 0,
                    "message": "Segment is mostly CK3 syntax, macro, or localization command and protected tokens are preserved.",
                }
            )
            if portuguese_text is None or source_tokens == output_tokens:
                return 1.0, "trusted", reasons
        else:
            score -= 0.45
            missing = list((source_tokens - old_tokens).elements())
            extra = list((old_tokens - source_tokens).elements())
            reasons.append(
                {
                    "rule": "technical_segment_token_mismatch",
                    "weight": -0.45,
                    "missing": missing[:20],
                    "extra": extra[:20],
                    "message": "Technical segment has protected token differences.",
                }
            )

    marker_count = spanish_marker_count(candidate)

    if (
        candidate
        and short_human_segment
        and source_tokens == old_tokens
        and marker_count == 0
    ):
        reasons.append(
            {
                "rule": "short_human_text_accepted",
                "weight": 0,
                "message": "Short human-readable segment has no Spanish-specific residue and preserves structure.",
            }
        )
        return 1.0, "trusted", reasons

    if (
        candidate
        and proper_name_like
        and source_tokens == old_tokens
        and marker_count == 0
    ):
        reasons.append(
            {
                "rule": "proper_name_like_accepted",
                "weight": 0,
                "message": "Proper-name-like segment is preserved without Spanish-specific residue.",
            }
        )
        return 1.0, "trusted", reasons

    if human_segment and not row["has_english"]:
        score -= 0.04
        reasons.append(
            {
                "rule": "missing_english_reference",
                "weight": -0.04,
                "message": "English reference is missing for this segment.",
            }
        )

    spanish_similarity = similarity(spanish_text, candidate)
    if (
        spanish_similarity >= 0.98
        and normalize_for_compare(spanish_text)
        and not technical_segment
    ):
        score -= 0.55
        reasons.append(
            {
                "rule": "same_as_spanish_source",
                "weight": -0.55,
                "value": round(spanish_similarity, 4),
                "message": "Old translation is effectively identical to Spanish source.",
            }
        )
    elif (
        spanish_similarity >= 0.9
        and len(candidate or "") > 20
        and not technical_segment
        and not has_portuguese_hint(candidate)
    ):
        score -= 0.25
        reasons.append(
            {
                "rule": "very_close_to_spanish_source",
                "weight": -0.25,
                "value": round(spanish_similarity, 4),
                "message": "Old translation is very close to Spanish source.",
            }
        )

    if marker_count >= 8:
        score -= 0.35
        reasons.append(
            {
                "rule": "strong_spanish_residue",
                "weight": -0.35,
                "value": marker_count,
                "message": "Old translation contains many Spanish markers.",
            }
        )
    elif marker_count >= 3:
        score -= 0.18
        reasons.append(
            {
                "rule": "possible_spanish_residue",
                "weight": -0.18,
                "value": marker_count,
                "message": "Old translation contains possible Spanish residue.",
            }
        )

    if source_tokens != old_tokens:
        missing = list((source_tokens - old_tokens).elements())
        extra = list((old_tokens - source_tokens).elements())
        severity = -0.45 if missing else -0.2
        score += severity
        reasons.append(
            {
                "rule": "protected_token_mismatch",
                "weight": severity,
                "missing": missing[:20],
                "extra": extra[:20],
                "message": "Protected tokens differ between Spanish source and old translation.",
            }
        )

    if portuguese_text is not None:
        if source_tokens != output_tokens:
            missing = list((source_tokens - output_tokens).elements())
            extra = list((output_tokens - source_tokens).elements())
            score -= 0.15
            reasons.append(
                {
                    "rule": "output_token_mismatch",
                    "weight": -0.15,
                    "missing": missing[:20],
                    "extra": extra[:20],
                    "message": "Current output tokens differ from Spanish source.",
                }
            )

    ratio = text_length_ratio(spanish_text, candidate)
    if ratio < 0.35 and len(spanish_text or "") > 20:
        score -= 0.22
        reasons.append(
            {
                "rule": "length_ratio_outlier",
                "weight": -0.22,
                "value": round(ratio, 4),
                "message": "Old translation length differs too much from Spanish source.",
            }
        )

    if human_segment and english_text and candidate:
        english_similarity = similarity(english_text, candidate)
        if english_similarity >= 0.98:
            score -= 0.2
            reasons.append(
                {
                    "rule": "same_as_english_reference",
                    "weight": -0.2,
                    "value": round(english_similarity, 4),
                    "message": "Old translation appears to still be English.",
                }
            )

    if candidate and len(WORD_PATTERN.findall(candidate)) <= 2 and len(candidate) <= 18:
        score += 0.04
        reasons.append(
            {
                "rule": "short_ui_text",
                "weight": 0.04,
                "message": "Short UI labels are less risky after token checks.",
            }
        )

    score = max(0.0, min(1.0, score))
    return score, classify(score), reasons


def upsert_analysis(conn, segment_id: int, score: float, classification: str, reasons: list[dict]) -> None:
    conn.execute(
        """
        INSERT INTO segment_analysis (
            segment_id,
            confidence_score,
            classification,
            reasons_json,
            rule_version,
            analyzed_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(segment_id) DO UPDATE SET
            confidence_score = excluded.confidence_score,
            classification = excluded.classification,
            reasons_json = excluded.reasons_json,
            rule_version = excluded.rule_version,
            analyzed_at = excluded.analyzed_at
        """,
        (
            segment_id,
            score,
            classification,
            json.dumps(reasons, ensure_ascii=False),
            RULE_VERSION,
            db.utc_now(),
        ),
    )


def main() -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[analyze_segments] Starting segment analysis")
    print(f"[analyze_segments] Rule version: {RULE_VERSION}")
    print(f"[analyze_segments] Database: {db.get_database_path(settings)}")

    total = 0
    processed = 0
    classification_counts: Counter = Counter()
    rule_counts: Counter = Counter()
    lowest_scores: list[tuple[float, str, str, str]] = []

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        total = conn.execute(
            "SELECT COUNT(*) AS total FROM source_segments WHERE is_active = 1"
        ).fetchone()["total"]
        print(f"[analyze_segments] Active segments: {total}")

        offset = 0
        while True:
            rows = conn.execute(
                """
                SELECT
                    s.id,
                    s.relative_path,
                    s.source_line_number,
                    s.source_key,
                    s.spanish_text,
                    s.english_text,
                    s.old_text,
                    s.has_english,
                    o.portuguese_text,
                    COALESCE(
                        (
                            SELECT
                                CASE
                                    WHEN f.decision = 'edited' THEN f.corrected_text
                                    WHEN f.decision = 'accepted_old' THEN s.old_text
                                    ELSE f.suggested_text
                                END
                            FROM suggestion_feedback f
                            WHERE f.segment_id = s.id
                              AND (
                                  f.decision IN ('accepted', 'edited', 'accepted_old')
                              )
                            ORDER BY f.updated_at DESC, f.id DESC
                            LIMIT 1
                        ),
                        NULL
                    ) AS approved_text
                FROM source_segments s
                LEFT JOIN output_segments o ON o.segment_id = s.id
                WHERE s.is_active = 1
                ORDER BY s.id
                LIMIT ? OFFSET ?
                """,
                (BATCH_SIZE, offset),
            ).fetchall()
            if not rows:
                break

            for row in rows:
                score, classification, reasons = analyze_row(row)
                upsert_analysis(conn, row["id"], score, classification, reasons)
                classification_counts[classification] += 1
                for reason in reasons:
                    rule_counts[reason["rule"]] += 1
                if len(lowest_scores) < 50 or score < lowest_scores[-1][0]:
                    lowest_scores.append(
                        (
                            score,
                            row["relative_path"],
                            str(row["source_line_number"]),
                            row["source_key"],
                        )
                    )
                    lowest_scores.sort(key=lambda item: item[0])
                    lowest_scores = lowest_scores[:50]
                processed += 1

            conn.commit()
            offset += len(rows)
            if processed == len(rows) or processed % (BATCH_SIZE * 2) == 0 or processed == total:
                print(
                    "[analyze_segments] "
                    f"{processed}/{total} segments analyzed "
                    f"({processed / total:.1%})"
                )

    elapsed = datetime.now() - started_at
    report_lines = [
        "Segment analysis report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        "",
        "Summary:",
        f"- Active segments analyzed: {processed}",
        "",
        "Classifications:",
    ]
    for name, count in sorted(classification_counts.items()):
        percent = count / processed if processed else 0
        report_lines.append(f"- {name}: {count} ({percent:.2%})")

    report_lines.extend(["", "Rule hits:"])
    for rule, count in rule_counts.most_common():
        report_lines.append(f"- {rule}: {count}")

    report_lines.extend(["", "Lowest confidence sample:"])
    for score, relative_path, line_number, source_key in lowest_scores[:50]:
        report_lines.append(f"- {score:.3f} | {relative_path}:{line_number} | {source_key}")

    report_path = db.write_report(settings, "analyze_segments", report_lines)
    print(f"[analyze_segments] Segments analyzed: {processed}")
    for name, count in sorted(classification_counts.items()):
        print(f"[analyze_segments] {name}: {count}")
    print(f"[analyze_segments] Report: {report_path}")
    print("[analyze_segments] Done")


if __name__ == "__main__":
    main()
