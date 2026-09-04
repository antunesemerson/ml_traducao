from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

import db
from apply_safe_output_updates import protected_tokens


RULE_VERSION = "pending_diagnostic_v2_ptbr_quotes"
WORD_PATTERN = re.compile(r"[A-Za-z\u00c0-\u00ff]+", re.UNICODE)
TOKEN_JOINED_TO_WORD_PATTERN = re.compile(r"(\]|\$[A-Za-z0-9_]+\$|#!)(?=[A-Za-z\u00c0-\u00ff])")
WORD_JOINED_TO_TOKEN_PATTERN = re.compile(r"(?<=[A-Za-z\u00c0-\u00ff])(\$[A-Za-z0-9_]+\$|#(?:EMP|V|P|N|bold|weak)\b)")
GENDER_TOKEN_SUFFIX_PATTERN = re.compile(r"\[[^\]]*Custom\(\s*['\"]ES_(?:OA|AO)['\"]\s*\)\](?=[ao]\b)", re.IGNORECASE)
GENDER_TOKEN_PREFIX_PATTERN = re.compile(r"(?<=[A-Za-z\u00c0-\u00ff])[ao]\[[^\]]*Custom\(\s*['\"]ES_(?:OA|AO)['\"]\s*\)\]", re.IGNORECASE)
INLINE_LITERAL_RESIDUE_PATTERN = re.compile(
    r"\[[^\]]*(?:Select_CString|Concept|LocalPlayerString)[^\]]*"
    r"['\"](?:[^'\"]*\b(?:un|una|gran|cortesanos|situaciones|decisiones|consejo|cr\u00eda|cr\u00edo|poetisa|poeta|tu|su|vuestra|vuestro)\b[^'\"]*)['\"]",
    re.IGNORECASE,
)
MOJIBAKE_PATTERN = re.compile(r"[A-Za-z\u00c0-\u00ff]\?[A-Za-z\u00c0-\u00ff]|\ufffd|Ã|Â")
RESIDUE_PATTERN = re.compile(
    r"\b("
    r"cortesanos?|cortesanas?|situaciones?|decisiones?|consejo|rechaza|rechazar|"
    r"se\u00f1or(?:a|es)?|vuestr[ao]s?|nuestr[ao]s?|vuestro|vuestra|"
    r"cr\u00eda|cr\u00edo|mendig[ao]|retorci\u00e9ndose|inmundicia|"
    r"robaste|rob\u00f3|asaltaste|asalt\u00f3|salvaste|salv\u00f3|dejaste|dej\u00f3|"
    r"decidiste|decidi\u00f3|conseguiste|consigui\u00f3|"
    r"hacendado|gobernantes?|invitados?|invitadas?|"
    r"de verdad|un d\u00eda|a la|al|del"
    r")\b",
    re.IGNORECASE,
)
SPANISH_PUNCTUATION = ("\u00bf", "\u00a1")
SPANISH_ANGULAR_QUOTES = ("\u00ab", "\u00bb", "\u00c2\u00ab", "\u00c2\u00bb")


def percent(part: int, total: int) -> float:
    if total == 0:
        return 0.0
    return part / total * 100


def issue_codes(item: dict[str, Any]) -> set[str]:
    return {issue.get("code", "") for issue in item.get("issues", [])}


def classify_bucket(item: dict[str, Any]) -> str:
    codes = issue_codes(item)
    action = item["action"]
    candidate_source = item["candidate_source"]
    word_count = int(item["word_count"] or 0)

    if item["token_status"] != "ok" or action == "blocked_structure":
        return "structure_blocked"
    if candidate_source in {"empty", "spanish_source"}:
        return "translation_required"
    if "mojibake_or_unexpected_script" in codes or "replacement_question_mark_mojibake" in codes:
        return "encoding_repair"
    if "spanish_residue_in_literal" in codes:
        return "inline_literal_residue"
    if codes and codes <= {
        "spanish_punctuation",
        "spanish_angular_quotes",
        "missing_space_after_token",
        "missing_space_before_token",
        "gender_token_extra_suffix",
        "gender_token_joined_to_word",
        "space_before_punctuation",
        "stray_leading_question_mark",
    }:
        return "mechanical_autofix"
    if "spanish_residue" in codes:
        if word_count >= 25:
            return "residual_spanish_long_text"
        return "residual_spanish_short_text"
    if action in {"auto_safe", "auto_safe_audit"}:
        return "likely_confirmable"
    if word_count >= 70:
        return "long_semantic_review"
    if word_count <= 8:
        return "short_ambiguous"
    return "medium_human_review"


def is_offline_translation_candidate(item: dict[str, Any]) -> bool:
    bucket = classify_bucket(item)
    return bucket in {
        "translation_required",
        "residual_spanish_long_text",
        "residual_spanish_short_text",
        "inline_literal_residue",
    }


def sample_text(value: str | None, limit: int = 260) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t")
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def strip_tokens(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\$[^$\s]+\$|\[[^\]]+\]|#[A-Za-z0-9_]+|#!|@[A-Za-z0-9_]+!|\\n", " ", value)


def word_count(value: str | None) -> int:
    return len(WORD_PATTERN.findall(strip_tokens(value)))


def choose_candidate(row: dict[str, Any]) -> tuple[str, str]:
    old = row.get("old_text")
    if old:
        return "old_text", str(old)
    spanish = row.get("spanish_text")
    if spanish:
        return "spanish_source", str(spanish)
    return "empty", ""


def fast_classify_item(row: dict[str, Any]) -> dict[str, Any]:
    candidate_source, candidate_text = choose_candidate(row)
    issues: list[dict[str, Any]] = []
    words = word_count(candidate_text)
    token_status = "ok"
    try:
        token_status = "ok" if protected_tokens(row.get("spanish_text")) == protected_tokens(candidate_text) else "mismatch"
    except Exception:
        token_status = "unknown"

    if token_status == "mismatch":
        issues.append({"code": "token_mismatch", "severity": "high"})
    if candidate_source in {"empty", "spanish_source"}:
        issues.append({"code": "needs_translation", "severity": "high"})
    if any(mark in candidate_text for mark in SPANISH_PUNCTUATION):
        issues.append({"code": "spanish_punctuation", "severity": "high"})
    if any(mark in candidate_text for mark in SPANISH_ANGULAR_QUOTES):
        issues.append({"code": "spanish_angular_quotes", "severity": "medium"})
    if MOJIBAKE_PATTERN.search(candidate_text):
        issues.append({"code": "mojibake", "severity": "high"})
    if INLINE_LITERAL_RESIDUE_PATTERN.search(candidate_text):
        issues.append({"code": "spanish_residue_in_literal", "severity": "high"})
    if TOKEN_JOINED_TO_WORD_PATTERN.search(candidate_text):
        issues.append({"code": "missing_space_after_token", "severity": "medium"})
    if WORD_JOINED_TO_TOKEN_PATTERN.search(candidate_text):
        issues.append({"code": "missing_space_before_token", "severity": "medium"})
    if GENDER_TOKEN_SUFFIX_PATTERN.search(candidate_text):
        issues.append({"code": "gender_token_extra_suffix", "severity": "high"})
    if GENDER_TOKEN_PREFIX_PATTERN.search(candidate_text):
        issues.append({"code": "gender_token_extra_prefix", "severity": "high"})
    if RESIDUE_PATTERN.search(strip_tokens(candidate_text)):
        issues.append({"code": "spanish_residue", "severity": "medium"})

    high_count = sum(1 for issue in issues if issue["severity"] == "high")
    medium_count = sum(1 for issue in issues if issue["severity"] == "medium")

    if token_status == "mismatch":
        action = "blocked_structure"
        risk_class = "critical"
        score = 0.25
    elif candidate_source in {"empty", "spanish_source"} or any(i["code"] == "needs_translation" for i in issues):
        action = "needs_suggestion"
        risk_class = "high"
        score = 0.20
    elif high_count or medium_count:
        action = "needs_autofix" if all(
            issue["code"]
            in {
                "spanish_punctuation",
                "spanish_angular_quotes",
                "missing_space_after_token",
                "missing_space_before_token",
                "gender_token_extra_suffix",
                "spanish_residue_in_literal",
                "mojibake",
            }
            for issue in issues
        ) else "needs_suggestion"
        risk_class = "high" if high_count else "medium"
        score = 0.55 if high_count else 0.68
    elif words >= 70:
        action = "needs_human"
        risk_class = "medium"
        score = 0.86
    elif words <= 30:
        action = "auto_safe_audit"
        risk_class = "low"
        score = 0.94
    else:
        action = "auto_safe_audit"
        risk_class = "medium"
        score = 0.88

    return {
        "segment_id": row["segment_id"],
        "relative_path": row["relative_path"],
        "source_key": row["source_key"],
        "source_line_number": row["source_line_number"],
        "candidate_source": candidate_source,
        "candidate_text": candidate_text,
        "action": action,
        "risk_class": risk_class,
        "confidence_score": score,
        "issue_count": len(issues),
        "high_issue_count": high_count,
        "medium_issue_count": medium_count,
        "word_count": words,
        "token_status": token_status,
        "reasons": ["fast_proxy"],
        "issues": issues,
    }


def fetch_pending_rows(conn, limit: int | None, path_like: str | None) -> list[dict[str, Any]]:
    path_sql = "AND s.relative_path LIKE ?" if path_like else ""
    limit_sql = "LIMIT ?" if limit else ""
    params: list[Any] = []
    if path_like:
        params.append(path_like)
    if limit:
        params.append(limit)

    rows = conn.execute(
        f"""
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.spanish_text,
            s.english_text,
            s.old_text,
            s.has_old,
            a.classification,
            a.confidence_score AS analysis_confidence,
            NULL AS suggestion_id,
            NULL AS suggested_text,
            NULL AS suggestion_status,
            NULL AS match_score,
            NULL AS suggestion_token_status
        FROM source_segments s
        LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
        LEFT JOIN segment_analysis a ON a.segment_id = s.id
        WHERE s.is_active = 1
          AND sc.segment_id IS NULL
          {path_sql}
        ORDER BY
            CASE WHEN a.classification = 'trusted' THEN 0 ELSE 1 END,
            length(coalesce(s.old_text, s.spanish_text, '')) ASC,
            s.id ASC
        {limit_sql}
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def build_report_lines(
    started_at: datetime,
    elapsed,
    active_segments: int,
    pending_total: int,
    items: list[dict[str, Any]],
    path_like: str | None,
    limit: int | None,
    sample_limit: int,
) -> list[str]:
    bucket_counts = Counter(classify_bucket(item) for item in items)
    action_counts = Counter(item["action"] for item in items)
    risk_counts = Counter(item["risk_class"] for item in items)
    source_counts = Counter(item["candidate_source"] for item in items)
    issue_counts: Counter[str] = Counter()
    package_counts: dict[str, Counter[str]] = defaultdict(Counter)
    translator_candidates = 0

    for item in items:
        bucket = classify_bucket(item)
        package_counts[item["relative_path"]][bucket] += 1
        if is_offline_translation_candidate(item):
            translator_candidates += 1
        for issue in item["issues"]:
            issue_counts[issue["code"]] += 1

    lines = [
        "Pending diagnostic report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Path filter: {path_like or 'none'}",
        f"Limit: {limit or 'none'}",
        "",
        "Coverage:",
        f"- Active segments: {active_segments}",
        f"- Pending unconfirmed segments total: {pending_total}",
        f"- Pending segments inspected: {len(items)}",
        f"- Offline translation/correction candidates: {translator_candidates} ({percent(translator_candidates, len(items)):.2f}%)",
        "",
        "Buckets:",
        *[
            f"- {bucket}: {count} ({percent(count, len(items)):.2f}%)"
            for bucket, count in bucket_counts.most_common()
        ],
        "",
        "Learned actions:",
        *[
            f"- {action}: {count} ({percent(count, len(items)):.2f}%)"
            for action, count in action_counts.most_common()
        ],
        "",
        "Risk classes:",
        *[
            f"- {risk}: {count} ({percent(count, len(items)):.2f}%)"
            for risk, count in risk_counts.most_common()
        ],
        "",
        "Candidate sources:",
        *[f"- {source}: {count}" for source, count in source_counts.most_common()],
        "",
        "Top issue codes:",
        *[f"- {code}: {count}" for code, count in issue_counts.most_common(30)],
        "",
        "Interpretation:",
        "- likely_confirmable: current validator believes these can be promoted or audited in bulk.",
        "- mechanical_autofix / encoding_repair: deterministic repair should be attempted before human review.",
        "- inline_literal_residue: translated text is mostly usable, but literals inside CK3 commands need targeted correction.",
        "- residual_spanish_* / translation_required: best candidates for an offline translator plus strict validation.",
        "- long_semantic_review / medium_human_review / short_ambiguous: use memory/glossary first, then sample human review.",
        "",
        "Recommended next stages:",
        "1. Run deterministic repairs on mechanical_autofix, encoding_repair and inline_literal_residue.",
        "2. Build an offline proposal layer for translation_required and residual_spanish_*.",
        "3. Validate translator proposals with token parity, residue scan, quote policy and glossary consistency.",
        "4. Auto-confirm only high-confidence proposals; queue only disagreements and high-risk long text.",
        "",
        "Top packages by unresolved useful work:",
    ]

    def package_sort(item: tuple[str, Counter[str]]) -> tuple[int, int, str]:
        path, counts = item
        high_value = (
            counts.get("translation_required", 0) * 5
            + counts.get("residual_spanish_long_text", 0) * 5
            + counts.get("inline_literal_residue", 0) * 4
            + counts.get("residual_spanish_short_text", 0) * 3
            + counts.get("mechanical_autofix", 0) * 2
            + counts.get("encoding_repair", 0) * 2
            + counts.get("long_semantic_review", 0)
        )
        return (-high_value, -sum(counts.values()), path)

    for path, counts in sorted(package_counts.items(), key=package_sort)[:40]:
        total = sum(counts.values())
        detail = ", ".join(f"{bucket}:{count}" for bucket, count in counts.most_common())
        lines.append(f"- {path}: {total} ({detail})")

    lines.append("")
    lines.append("Samples:")
    sample_order = [
        "translation_required",
        "residual_spanish_long_text",
        "inline_literal_residue",
        "mechanical_autofix",
        "encoding_repair",
        "structure_blocked",
        "long_semantic_review",
        "short_ambiguous",
        "likely_confirmable",
    ]
    for bucket in sample_order:
        bucket_items = [item for item in items if classify_bucket(item) == bucket][:sample_limit]
        if not bucket_items:
            continue
        lines.append(f"{bucket}:")
        for item in bucket_items:
            issues = ", ".join(issue["code"] for issue in item["issues"][:5]) or "none"
            lines.append(
                f"- segment {item['segment_id']} | score {item['confidence_score']:.3f} | "
                f"{item['relative_path']}::{item['source_key']} | issues: {issues}"
            )
            lines.append(f"  candidate: {sample_text(item['candidate_text'])}")
    return lines


def main(limit: int | None = None, path_like: str | None = None, sample_limit: int = 8) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[pending_diagnostic] Starting pending diagnostic")
    print(f"[pending_diagnostic] Rule version: {RULE_VERSION}")
    print(f"[pending_diagnostic] Limit: {limit or 'none'}")
    print(f"[pending_diagnostic] Path filter: {path_like or 'none'}")
    print(f"[pending_diagnostic] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        active_segments = int(
            conn.execute("SELECT COUNT(*) FROM source_segments WHERE is_active = 1").fetchone()[0] or 0
        )
        pending_total = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM source_segments s
                LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
                WHERE s.is_active = 1
                  AND sc.segment_id IS NULL
                """
            ).fetchone()[0]
            or 0
        )
        rows = fetch_pending_rows(conn, limit, path_like)

    items = [fast_classify_item(row) for row in rows]
    bucket_counts = Counter(classify_bucket(item) for item in items)
    action_counts = Counter(item["action"] for item in items)
    translator_candidates = sum(1 for item in items if is_offline_translation_candidate(item))

    elapsed = datetime.now() - started_at
    report_lines = build_report_lines(
        started_at=started_at,
        elapsed=elapsed,
        active_segments=active_segments,
        pending_total=pending_total,
        items=items,
        path_like=path_like,
        limit=limit,
        sample_limit=sample_limit,
    )
    report_path = db.write_report(settings, "pending_diagnostic", report_lines)

    print(f"[pending_diagnostic] Pending total: {pending_total}")
    print(f"[pending_diagnostic] Items inspected: {len(items)}")
    print(
        "[pending_diagnostic] Offline translation/correction candidates: "
        f"{translator_candidates} ({percent(translator_candidates, len(items)):.2f}%)"
    )
    for bucket, count in bucket_counts.most_common():
        print(f"[pending_diagnostic] bucket {bucket}: {count}")
    for action, count in action_counts.most_common():
        print(f"[pending_diagnostic] action {action}: {count}")
    print(f"[pending_diagnostic] Report: {report_path}")
    print("[pending_diagnostic] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Diagnose unresolved CK3 localization segments.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum pending rows to inspect.")
    parser.add_argument("--path-like", default=None, help="Optional SQL LIKE filter for source relative_path.")
    parser.add_argument("--sample-limit", type=int, default=8, help="Samples per bucket in the report.")
    args = parser.parse_args()
    main(limit=args.limit, path_like=args.path_like, sample_limit=args.sample_limit)
