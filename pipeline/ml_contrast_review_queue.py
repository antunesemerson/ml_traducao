from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator
import ml_train_risk


RULE_VERSION = "ml_contrast_review_queue_v1"
QUEUE_SOURCE = "ml_group_candidate_queue"

TOKEN_PATTERN = local_quality_validator.PROTECTED_TOKEN_PATTERN
WORD_PATTERN = re.compile(r"[^\W\d_]+", re.UNICODE)

ENGLISH_RESIDUE_WORDS = {
    "battlefield",
    "claim",
    "conversion",
    "county",
    "duchy",
    "faith",
    "invasion",
    "kingdom",
    "lifestyle",
    "monthly",
    "realm",
    "speed",
    "throne",
    "war",
}

SPANISH_FORMATTED_WORDS = {
    "alto",
    "baja",
    "bajo",
    "media",
    "medio",
    "muy",
}

SAFE_FORMATTED_PT_WORDS = {
    "alto",
    "baixa",
    "baixo",
    "igual",
    "invertida",
    "lenta",
    "r\u00e1pida",
}

LATIN_HINT_WORDS = {
    "aeternum",
    "alius",
    "cras",
    "dies",
    "dominus",
    "est",
    "gloria",
    "lux",
    "mors",
    "semper",
}

TECHNICAL_PATH_PREFIXES = (
    "names/",
    "dynasties/",
)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def normalize_space(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def visible_text(text: str | None) -> str:
    without_tokens = TOKEN_PATTERN.sub(" ", text or "")
    return normalize_space(without_tokens)


def words(text: str | None) -> list[str]:
    return [word.lower() for word in WORD_PATTERN.findall(visible_text(text))]


def is_name_path(relative_path: str) -> bool:
    return relative_path.startswith(TECHNICAL_PATH_PREFIXES)


def is_mostly_identifier(text: str | None) -> bool:
    value = visible_text(text)
    if not value:
        return True
    if "_" in value and " " not in value:
        return True
    return bool(re.fullmatch(r"[A-Za-z0-9_.:-]+", value)) and value.isupper()


def has_review(conn, segment_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM local_learning_candidates c
        JOIN local_learning_runs r ON r.id = c.run_id
        WHERE c.segment_id = ?
          AND r.mode LIKE 'human_review%'
        LIMIT 1
        """,
        (segment_id,),
    ).fetchone()
    return row is not None


def classify_candidate(row: dict[str, Any]) -> tuple[str, str] | None:
    relative_path = row["relative_path"]
    candidate = row.get("candidate_text") or ""
    english = row.get("english_text") or ""
    spanish = row.get("spanish_text") or ""
    candidate_visible = visible_text(candidate)
    english_visible = visible_text(english)
    spanish_visible = visible_text(spanish)
    candidate_words = set(words(candidate))

    if not candidate_visible or is_mostly_identifier(candidate):
        return None

    if not is_name_path(relative_path) and candidate_visible == english_visible and candidate_visible != spanish_visible:
        return (
            "exact_english_visible",
            "Candidate visible text is identical to English but differs from Spanish; likely untranslated UI text.",
        )

    english_hits = sorted(candidate_words & ENGLISH_RESIDUE_WORDS)
    if not is_name_path(relative_path) and english_hits and candidate_visible != spanish_visible:
        return (
            "english_residue_words",
            f"Candidate contains English-looking UI words: {', '.join(english_hits)}.",
        )

    formatted_spanish_hits = sorted(
        {
            match.group(1).lower()
            for match in re.finditer(r"#[A-Za-z0-9_]+\s+([^\W\d_]+)#!", candidate)
            if match.group(1).lower() in SPANISH_FORMATTED_WORDS
        }
    )
    if formatted_spanish_hits:
        return (
            "spanish_inside_formatting",
            f"Formatted visible text may still be Spanish: {', '.join(formatted_spanish_hits)}.",
        )

    validation = local_quality_validator.validate_text(candidate)
    issue_codes = {issue["code"] for issue in validation["issues"]}
    if issue_codes & {"spanish_residue_in_literal", "spanish_residue", "english_residue"}:
        return (
            "validator_language_residue",
            "Local validator found language residue in visible text.",
        )

    return None


def classify_positive_candidate(row: dict[str, Any]) -> tuple[str, str] | None:
    relative_path = row["relative_path"]
    candidate = row.get("candidate_text") or ""
    english = row.get("english_text") or ""
    spanish = row.get("spanish_text") or ""
    candidate_visible = visible_text(candidate)
    english_visible = visible_text(english)
    spanish_visible = visible_text(spanish)
    candidate_words = set(words(candidate))

    if not candidate_visible:
        return None

    validation = local_quality_validator.validate_text(candidate)
    if validation["auto_approval_blocked"]:
        return None

    if is_name_path(relative_path) and candidate_visible == english_visible == spanish_visible:
        return (
            "safe_proper_name",
            "Proper name/dynasty text is intentionally preserved across languages.",
        )

    if relative_path.startswith("mottos/") and candidate_visible == english_visible:
        if candidate_words & LATIN_HINT_WORDS:
            return (
                "safe_latin_motto",
                "Latin motto is intentionally preserved, not an English residue.",
            )

    formatted_safe_hits = sorted(
        {
            match.group(1).lower()
            for match in re.finditer(r"#[A-Za-z0-9_]+\s+([^\W\d_]+)#!", candidate)
            if match.group(1).lower() in SAFE_FORMATTED_PT_WORDS
        }
    )
    if formatted_safe_hits:
        return (
            "safe_formatted_pt",
            f"Formatted visible text is valid Portuguese despite overlap with Spanish: {', '.join(formatted_safe_hits)}.",
        )

    if candidate_visible == spanish_visible and candidate_visible == english_visible:
        return (
            "safe_shared_literal",
            "Visible text is intentionally shared across all source languages.",
        )

    return None


def classify_language_candidate(row: dict[str, Any]) -> tuple[str, str] | None:
    features = set(ml_train_risk.language_features(row))
    validation = local_quality_validator.validate_text(row.get("candidate_text") or "")
    issue_codes = {issue["code"] for issue in validation["issues"]}

    risk_rules = [
        (
            "language_exact_english_visible",
            "RISK_EXACT_ENGLISH_VISIBLE",
            "Visible text matches English but differs from Spanish; possible untranslated UI/localization text.",
        ),
        (
            "language_spanish_localization_helper",
            "RISK_SPANISH_LOCALIZATION_HELPER",
            "Candidate still references Spanish-specific localization helpers such as Loc_ES_ or Custom('ES_...).",
        ),
        (
            "language_spanish_formatted_literal",
            "RISK_SPANISH_FORMATTED_LITERAL",
            "Formatted visible literal looks Spanish inside CK3 formatting.",
        ),
    ]
    for bucket, feature, reason in risk_rules:
        if feature in features:
            return bucket, reason

    if issue_codes & {"spanish_residue_in_literal", "spanish_residue", "english_residue"}:
        return (
            "language_validator_residue",
            "Local validator found visible language residue; useful diagnostic sample for the language layer.",
        )

    safe_rules = [
        (
            "language_safe_shared_name_or_motto",
            "SAFE_SHARED_NAME_OR_MOTTO",
            "Shared visible text is probably intentional for names, dynasties, or mottos.",
        ),
        (
            "language_safe_formatted_portuguese",
            "SAFE_FORMATTED_PORTUGUESE_LITERAL",
            "Formatted literal is valid Portuguese and should not be punished as Spanish residue.",
        ),
        (
            "language_safe_shared_literal",
            "VISIBLE_EQUALS_BOTH_SOURCES=True",
            "Visible text is shared by English, Spanish, and candidate; likely an intentional literal or identifier.",
        ),
    ]
    for bucket, feature, reason in safe_rules:
        if feature in features:
            return bucket, reason

    return None


def next_lote_number(conn) -> int:
    rows = conn.execute(
        """
        SELECT mode
        FROM local_learning_runs
        WHERE mode LIKE 'human_review%lote%'
        """
    ).fetchall()
    highest = 0
    for row in rows:
        match = re.search(r"lote(\d+)", row["mode"] or "")
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def fetch_rows(conn, scan_limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH token_counts AS (
            SELECT segment_id, COUNT(*) AS token_count
            FROM protected_tokens
            GROUP BY segment_id
        )
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS current_output_text,
            coalesce(o.portuguese_text, s.old_text, s.spanish_text, '') AS candidate_text,
            coalesce(tc.token_count, 0) AS token_count
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        LEFT JOIN token_counts tc ON tc.segment_id = s.id
        WHERE s.is_active = 1
          AND coalesce(o.portuguese_text, s.old_text, s.spanish_text, '') <> ''
          AND NOT EXISTS (
              SELECT 1
              FROM local_learning_candidates c
              JOIN local_learning_runs r ON r.id = c.run_id
              WHERE c.segment_id = s.id
                AND r.mode LIKE 'human_review%'
          )
        ORDER BY
            CASE
                WHEN s.relative_path LIKE 'game_rules%' THEN 0
                WHEN s.relative_path LIKE 'wars%' THEN 1
                WHEN s.relative_path LIKE 'schemes%' THEN 2
                WHEN s.relative_path LIKE 'triggers/%' THEN 3
                WHEN s.relative_path LIKE 'buildings%' THEN 4
                ELSE 5
            END,
            s.id
        LIMIT ?
        """,
        (scan_limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def candidate_payload(row: dict[str, Any], bucket: str, reason: str) -> dict[str, Any]:
    return {
        "segment_id": row["segment_id"],
        "relative_path": row["relative_path"],
        "source_key": row["source_key"],
        "source_line_number": row["source_line_number"],
        "source_section": "ML contrast review queue",
        "focus_group": bucket,
        "group_name": bucket,
        "candidate_kind": "contrast_review",
        "final_action": "needs_human",
        "risk_class": "contrast",
        "model_safe_probability": 0,
        "issue_count": 0,
        "token_status": "unknown",
        "english_text": row["english_text"],
        "spanish_text": row["spanish_text"],
        "old_text": row["old_text"],
        "current_output_text": row["current_output_text"],
        "suggested_text": row["candidate_text"],
        "human_label": "pending",
        "corrected_text": None,
        "reason": "",
        "contrast_reason": reason,
    }


def write_report(settings: dict, output_path: Path, selected: list[dict[str, Any]]) -> Path:
    counts = Counter(candidate["focus_group"] for candidate in selected)
    path_counts = Counter(candidate["relative_path"] for candidate in selected)
    lines = [
        "ML contrast review queue",
        f"Started at: {now()}",
        f"Rule version: {RULE_VERSION}",
        f"JSON: {output_path}",
        "",
        "Summary:",
        f"- Candidates: {len(selected)}",
        "",
        "By focus group:",
        *[f"- {key}: {value}" for key, value in sorted(counts.items())],
        "",
        "Top paths:",
        *[f"- {path}: {value}" for path, value in path_counts.most_common(20)],
        "",
        "Review guidance:",
        "- Mark visible English/Spanish residue as semantic_error or residual_spanish.",
        "- Mark intentional names, mottos, identifiers, or untranslatable proper nouns as contextual_exception.",
        "- Use minor_fix when the translation is basically right but needs wording/capitalization polish.",
        "- This queue does not train, apply output, or promote models by itself.",
    ]
    return db.write_report(settings, "ml_contrast_review_queue", lines)


def main(
    limit: int = 40,
    batch_size: int = 20,
    scan_limit: int = 80000,
    output: str | None = None,
    mode: str = "risk",
) -> None:
    settings = db.load_settings()
    print("[ml_contrast_review_queue] Starting contrast review queue")
    print(f"[ml_contrast_review_queue] Rule version: {RULE_VERSION}")
    print(f"[ml_contrast_review_queue] Mode: {mode}")
    selected: list[dict[str, Any]] = []
    seen_buckets: Counter[str] = Counter()

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        first_lote = next_lote_number(conn)
        for row in fetch_rows(conn, scan_limit):
            if len(selected) >= limit:
                break
            if has_review(conn, int(row["segment_id"])):
                continue
            if mode == "positive":
                classification = classify_positive_candidate(row)
            elif mode == "language":
                classification = classify_language_candidate(row)
            else:
                classification = classify_candidate(row)
            if classification is None:
                continue
            bucket, reason = classification
            if seen_buckets[bucket] >= max(8, limit // 3):
                continue
            seen_buckets[bucket] += 1
            selected.append(candidate_payload(row, bucket, reason))

    batches = []
    for offset in range(0, len(selected), batch_size):
        candidates = selected[offset : offset + batch_size]
        if not candidates:
            continue
        batches.append(
            {
                "lote_number": first_lote + len(batches),
                "source_section": "ML contrast review queue",
                "focus_group": candidates[0]["focus_group"],
                "candidates": candidates,
            }
        )

    payload = {
        "rule_version": "parallel_review_loop_v1",
        "prepared_at": now(),
        "source_type": QUEUE_SOURCE,
        "score_run_id": None,
        "source_report": None,
        "batch_size": batch_size,
        "batches": batches,
        "instructions": {
            "valid_labels": [
                "contextual_exception",
                "correct",
                "major_fix",
                "minor_fix",
                "rejected",
                "rejected_suggestion",
                "residual_spanish",
                "semantic_error",
                "structure_error",
                "token_mismatch",
            ],
            "recommended_labels": [
                "semantic_error",
                "residual_spanish",
                "minor_fix",
                "contextual_exception",
                "correct",
            ],
            "fill": ["human_label", "corrected_text when useful", "reason"],
            "do_not_run": ["learn-feedback", "ml-dataset", "ml-train-risk", "ml-score", "ml-score-audit"],
        },
    }
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    output_path = Path(output) if output else reports_dir / f"{timestamp()}_ml_{mode}_contrast_review_decisions_template.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path = write_report(settings, output_path, selected)

    print(f"[ml_contrast_review_queue] Candidates: {len(selected)}")
    print(f"[ml_contrast_review_queue] Decision template: {output_path}")
    print(f"[ml_contrast_review_queue] Report: {report_path}")
    print("[ml_contrast_review_queue] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build an active contrast-review queue for ML training.")
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--scan-limit", type=int, default=80000)
    parser.add_argument("--mode", choices=["risk", "positive", "language"], default="risk")
    parser.add_argument("--output")
    args = parser.parse_args()
    main(limit=args.limit, batch_size=args.batch_size, scan_limit=args.scan_limit, output=args.output, mode=args.mode)
