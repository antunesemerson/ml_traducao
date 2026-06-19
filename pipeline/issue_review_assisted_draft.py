from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from local_quality_validator import validate_text


RULE_VERSION = "issue_review_assisted_draft_v11_medium_ui_residual_guards"

SPANISH_PATTERNS = [
    r"\bconvertirse\b",
    r"\bhace imposible\b",
    r"\bse convertir[aá]\b",
]
RAW_SPANISH_PATTERNS = [
    r"#bold\s+no#!",
    r"\bapoya\b",
    r"\bapoyaste\b",
    r"\bapoyas\b",
    r"\bBajo\s+el\s+gobierno\b",
    r"\bdecidiste\b",
    r"\bdecidi[oó]\b",
    r"\bempiezo\b",
    r"\bencarcelados\b",
    r"\beres\b",
    r"\beras\b",
    r"\bes\s+humillad\b",
    r"\bhas\b",
    r"\bha\b",
    r"\bha\s+sido\b",
    r"\bhabla\b",
    r"\bhablas\b",
    r"\bheredar[aá]s\b",
    r"\bheredar[aá]\b",
    r"\bhacerlo\b",
    r"\bintentar[aá]s\b",
    r"\bintentar[aá]\b",
    r"\bla\s+señora\b",
    r"\bel\s+señor\b",
    r"\blogra\b",
    r"\blogras\b",
    r"\bmi\s+señorío\b",
    r"\bmisionera\b",
    r"\bmisionero\b",
    r"\bos\s+hicisteis\b",
    r"\bpasar[aá]n\b",
    r"\bpasan\b",
    r"\bperdiste\b",
    r"\bperdi[oó]\b",
    r"\bpuede\s+acabar\b",
    r"\bpuede\b",
    r"\bpuedes\b",
    r"\bprefiere\b",
    r"\bprefieres\b",
    r"\bproponerse\b",
    r"\bproponerte\b",
    r"\bsana\s+y\s+salva\b",
    r"\bsano\s+y\s+salvo\b",
    r"\bse\s+apoy[oó]\b",
    r"\bse\s+compromete\b",
    r"\bse\s+hicieron\b",
    r"\bse\s+olvida\b",
    r"\bse\s+opone\b",
    r"\bse\s+opuso\b",
    r"\bsois\b",
    r"\bson\b",
    r"\bte\s+apoyaste\b",
    r"\bte\s+comprometes\b",
    r"\bte\s+olvidas\b",
    r"\bte\s+opones\b",
    r"\bte\s+opusiste\b",
    r"\bcon\s+una\b",
    r"\bprobabilidad\s+de\s+[ée]xito\b",
    r"\bganaste\b",
    r"\bgan[oó]\b",
    r"\bganar[aá]\b",
    r"\bganar[aá]s\b",
    r"\bopini[oó]n\s+del\s+condado\b",
    r"\btu\s+persona\b",
    r"\bt[uú]\b",
    r"\bjurar\s+lealtad\b",
    r"\bvasallaje\b",
    r"\bvasalla\b",
    r"\bvasallo\b",
    r"\bganado#!",
    r"\bsonido\b",
    r"\bsi\s+pierdes\b",
    r"\brecuperaci\S*n\b",
    r"\bpor\s+a\s+\[",
    r"'debes'",
    r"'debe'",
]
MOJIBAKE_MARKERS = (
    "\ufffd",
    "\u00c3\u0192",
    "\u00c3\u201a",
    "\u00c3\u00a1",
    "\u00c3\u00a9",
    "\u00c3\u00ad",
    "\u00c3\u00b3",
    "\u00c3\u00ba",
    "\u00c3\u00a7",
    "\u00c3\u00a3",
    "\u00c3\u00b5",
    "\u00c3\u00aa",
    "\u00c3\u00b4",
    "voc\u0119",
    "s\u0103o",
    "n\u0103o",
)
DYNAMIC_CUSTOM_PATTERNS = (
    "Custom('ES_",
    'Custom("ES_',
    "Select_CString(",
    "SelectLocalization(",
)
GENDER_TOKEN_PATTERNS = (
    "Custom('ES_OA')",
    'Custom("ES_OA")',
    "Custom('ES_AO')",
    'Custom("ES_AO")',
    "Custom('ES_EA')",
    'Custom("ES_EA")',
)
LONG_CONTEXT_BUCKETS = {
    "domain_events_longform",
    "domain_interactions_activities",
    "package_dlc",
}
RAW_ENGLISH_PATTERNS = [
    r"^\s*the\s+[A-Za-z]",
    r"^\s*(?:invade|subjugate|usurp|revoke|conquer)\s+\[",
    r"\bin\s+[A-Za-z][A-Za-z' -]{2,}\s+itself\b",
    r"\bitself\b",
    r"\bloses?\s+nothing\s+to\b",
    r"\bgains?\s+nothing\b",
    r"\bwill\s+(?:lose|gain|receive|be|become|not)\b",
    r"\byou\s+(?:are|will|have|cannot|can)\b",
    r"\bculture\s+head\b",
]
PTBR_COMPACT_ISSUE_PATTERNS = [
    (r"\bapreciad\b", "truncated_gender_adjective_apreciad"),
    (r"\bCada\s+\[[^\]]+\]\s+que\s+n[aã]o\s+te\s+odeiam\b", "cada_subject_plural_verb_odeiam"),
]
SPANISH_FALSE_POSITIVE_PHRASES = (
    "my son",
)
TITLE_PTBR_SAFE_DIFF_MARKERS = {"khuzestao", "luristao"}
TRIGGER_KINSHIP_SOURCE_PATTERNS = (
    r"(^|_)IS_HEIR_",
    r"(^|_)IS_COUSIN_",
    r"(^|_)IS_TWIN_",
    r"(^|_)IS_GREAT_GRANDPARENT_",
    r"(^|_)IS_GRANDPARENT_",
    r"(^|_)IS_GRANDCHILD_",
    r"(^|_)IS_NIBLING_",
)
TRIGGER_ROLE_ARTICLE_SOURCE_KEYS = {
    "I_AM_A_VASSAL_OF",
    "IS_LANDLESS_ADVENTURER_TRIGGER",
    "NONE_ARE_LANDLESS_ADVENTURER_TRIGGER",
    "IS_A_COURTIER_TRIGGER",
    "THEY_ARE_CLAIMANT_TRIGGER",
}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"JSONL line {line_number} is not an object.")
        rows.append(payload)
    return rows


def short(value: str | None, limit: int = 180) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def visible_text(text: str) -> str:
    text = re.sub(r"\[[^\]]+\]", " ", text)
    text = re.sub(r"\$[^$]+\$", " ", text)
    text = re.sub(r"#[^#\s]+", " ", text)
    text = re.sub(r"@[A-Za-z0-9_]+!", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def has_actual_mojibake(text: str) -> bool:
    extra_markers = ("\ufffd", "\u0119", "\u0151", "\u0103", "\u0102", "\u0150", "\u0219", "\u021b")
    return any(marker in text for marker in (*MOJIBAKE_MARKERS, *extra_markers))


def spanish_hits(text: str) -> list[str]:
    raw = text.lower()
    cleaned = visible_text(text)
    for phrase in SPANISH_FALSE_POSITIVE_PHRASES:
        raw = raw.replace(phrase, " ")
        cleaned = cleaned.replace(phrase, " ")
    hits = []
    if cleaned in {"el", "la", "los", "las", "un", "una"}:
        hits.append(f"standalone_spanish_article:{cleaned}")
    for pattern in RAW_SPANISH_PATTERNS:
        if re.search(pattern, raw, flags=re.IGNORECASE):
            hits.append(pattern)
    for pattern in SPANISH_PATTERNS:
        if re.search(pattern, cleaned, flags=re.IGNORECASE):
            hits.append(pattern)
    return hits


def english_hits(text: str) -> list[str]:
    raw = text.lower()
    cleaned = visible_text(text)
    hits = []
    for pattern in RAW_ENGLISH_PATTERNS:
        if re.search(pattern, raw, flags=re.IGNORECASE) or re.search(pattern, cleaned, flags=re.IGNORECASE):
            hits.append(pattern)
    return hits


def ptbr_compact_issue_hits(text: str) -> list[str]:
    hits = []
    for pattern, code in PTBR_COMPACT_ISSUE_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            hits.append(code)
    return hits


def queue_text(row: dict[str, Any]) -> str:
    texts = row.get("texts") or {}
    return str(
        texts.get("confirmed_text")
        or texts.get("evidence_text")
        or row.get("confirmed_text")
        or row.get("evidence_text")
        or ""
    )


def normalize_ascii(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold()


def classify_title_preserved_frontier(row: dict[str, Any], text: str) -> tuple[str, str] | None:
    if row.get("relative_path") != "titles_l_spanish.yml":
        return None
    if row.get("active_action") != "auto_safe":
        return None
    if row.get("candidate_action") != "needs_autofix" or row.get("policy_action") != "needs_autofix":
        return None

    texts = row.get("texts") or {}
    spanish = str(texts.get("spanish_text") or "")
    source_key = str(row.get("source_key") or "")
    normalized = normalize_ascii(text)

    if text != spanish and any(marker in normalized for marker in TITLE_PTBR_SAFE_DIFF_MARKERS):
        return "false_positive_reopen", "title_ptbr_exonym_differs_from_spanish_candidate_overblocked"
    if re.search(r"(^|[\s-])este($|[\s-])", normalized):
        return "needs_repair", "title_direction_residual_este"
    if re.search(r"(^|[\s-])sur($|[\s-])", normalized):
        return "needs_repair", "title_direction_residual_sur"
    if "noreste" in normalized:
        return "needs_repair", "title_direction_residual_noreste"
    if "sureste" in normalized:
        return "needs_repair", "title_direction_residual_sureste"
    if "camino de" in normalized:
        return "needs_repair", "title_direction_residual_camino_de"
    if "ruta de" in normalized:
        return "needs_repair", "title_direction_residual_ruta_de"
    if re.search(r"(^|[\s-])bajo($|[\s-])|^bajo", normalized):
        return "needs_repair", "title_direction_residual_bajo"
    if source_key.endswith("_adj") and (normalized.startswith("oeste") or " oest" in normalized):
        return "needs_repair", "title_adjective_oeste_compound_residual"
    if source_key.endswith("_adj") and re.search(r"\b[^\W\d_]+és\b", text, flags=re.IGNORECASE):
        return "needs_repair", "title_adjective_spanish_acute_es_suffix"
    if text == spanish:
        return "needs_domain_context", "title_preserved_equals_spanish_requires_title_policy_review"
    return "needs_domain_context", "title_preserved_frontier_conservative_context"


def classify_trigger_frontier(row: dict[str, Any], text: str) -> tuple[str, str] | None:
    if row.get("relative_path") != "triggers/character_triggers_l_spanish.yml":
        return None
    if row.get("active_action") != "auto_safe":
        return None
    if row.get("candidate_action") != "needs_autofix" or row.get("policy_action") != "needs_autofix":
        return None

    source_key = str(row.get("source_key") or "")
    upper_key = source_key.upper()
    if any(re.search(pattern, upper_key) for pattern in TRIGGER_KINSHIP_SOURCE_PATTERNS):
        return "needs_new_microagent", "trigger_kinship_gender_surface_requires_family_gender_policy"
    if upper_key in TRIGGER_ROLE_ARTICLE_SOURCE_KEYS:
        return "needs_new_microagent", "trigger_role_article_gender_surface_requires_gender_policy"

    return None


def evidence(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("evidence")
    if isinstance(payload, dict):
        return payload
    evidence_json = row.get("evidence_json")
    if isinstance(evidence_json, str) and evidence_json.strip():
        try:
            loaded = json.loads(evidence_json)
        except json.JSONDecodeError:
            loaded = {}
        if isinstance(loaded, dict):
            return loaded
    fallback: dict[str, Any] = {}
    for key in ("token_count", "word_count", "text_length", "char_count"):
        if key in row:
            fallback[key] = row.get(key)
    if "char_count" in fallback and "text_length" not in fallback:
        fallback["text_length"] = fallback["char_count"]
    return fallback


def is_dynamic_queue(row: dict[str, Any]) -> bool:
    bucket = str(row.get("queue_bucket") or "")
    return row.get("agent_key") == "micro_dynamic_ck3_expression" or bucket.startswith("dynamic_")


def local_validator_issue_reason(text: str) -> str:
    result = validate_text(text)
    issues = result.get("issues") or []
    if not issues:
        return ""
    codes = []
    for issue in issues[:4]:
        code = str(issue.get("code") or "quality_issue")
        severity = str(issue.get("severity") or "unknown")
        codes.append(f"{code}:{severity}")
    return ",".join(codes)


def classify(row: dict[str, Any]) -> tuple[str, str]:
    text = queue_text(row)
    row_evidence = evidence(row)
    token_status = str(row.get("token_status") or "")
    token_impact = str(row.get("token_impact") or "")
    queue_bucket = str(row.get("queue_bucket") or "")
    active_action = str(row.get("active_action") or "")
    candidate_action = str(row.get("candidate_action") or "")
    policy_action = str(row.get("policy_action") or "")
    token_count = int(row_evidence.get("token_count") or 0)
    word_count = int(row_evidence.get("word_count") or 0)
    text_length = int(row_evidence.get("text_length") or len(text))

    if has_actual_mojibake(text):
        return "needs_repair", "actual_mojibake_marker_in_confirmed_text"

    spanish = spanish_hits(text)
    if spanish:
        return "needs_repair", "spanish_residual:" + ",".join(spanish[:4])

    english = english_hits(text)
    if english:
        return "needs_repair", "english_residual:" + ",".join(english[:4])

    ptbr_issues = ptbr_compact_issue_hits(text)
    if ptbr_issues:
        return "needs_repair", "ptbr_compact_issue:" + ",".join(ptbr_issues[:4])

    title_decision = classify_title_preserved_frontier(row, text)
    if title_decision is not None:
        return title_decision

    trigger_decision = classify_trigger_frontier(row, text)
    if trigger_decision is not None:
        return trigger_decision

    if any(pattern in text for pattern in GENDER_TOKEN_PATTERNS):
        return "needs_new_microagent", "gender_dynamic_token_should_be_owned_by_gender_microagent"

    if token_impact == "token_mismatch" or token_status == "mismatch":
        return "needs_domain_context", "token_sensitive_mismatch_requires_token_policy_or_context"

    if any(pattern in text for pattern in DYNAMIC_CUSTOM_PATTERNS):
        if text_length <= 120 and word_count <= 8:
            return "needs_new_microagent", "dynamic_ck3_expression_requires_specialist_delegate"
        return "needs_domain_context", "dynamic_ck3_expression_in_contextual_or_long_text"

    if queue_bucket in LONG_CONTEXT_BUCKETS or text_length > 180 or token_count >= 10:
        return "needs_domain_context", "long_or_multi_token_segment_not_owned_by_short_label_microagent"

    if active_action == "auto_safe" and candidate_action == "needs_autofix" and policy_action == "needs_autofix":
        return "false_positive_reopen", "active_safe_clean_short_label_candidate_overblocked"

    if is_dynamic_queue(row):
        return "needs_domain_context", "dynamic_expression_requires_human_validation_before_positive_ingest"

    validator_reason = local_validator_issue_reason(text)
    if validator_reason:
        return "needs_repair", "local_quality_validator:" + validator_reason

    if text_length <= 160 and token_count <= 8:
        return "safe_short_label", "clean_short_or_compact_label"

    return "needs_domain_context", "conservative_fallback_context_required"


def output_paths(settings: dict[str, Any], source_path: Path, reviewer: str) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_reviewer = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in reviewer)
    base_name = source_path.stem.replace("_decisions_template", "")
    return (
        reports_dir / f"{stamp}_{base_name}_{safe_reviewer}_reviewed.jsonl",
        reports_dir / f"{stamp}_{base_name}_{safe_reviewer}_reviewed.txt",
    )


def main(*, queue_jsonl: str, reviewer: str = "codex_assisted_review") -> dict[str, Any]:
    settings = db.load_settings()
    source_path = db.project_path(queue_jsonl)
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    rows = load_jsonl(source_path)
    decisions_path, report_path = output_paths(settings, source_path, reviewer)
    counts: Counter = Counter()
    bucket_counts: Counter = Counter()
    decision_bucket_counts: Counter = Counter()
    samples: list[str] = []

    with decisions_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            decision, reason = classify(row)
            counts[decision] += 1
            bucket = str(row.get("queue_bucket") or "unknown")
            bucket_counts[bucket] += 1
            decision_bucket_counts[f"{decision}|{bucket}"] += 1
            payload = {
                "queue_run_id": row.get("queue_run_id"),
                "ledger_item_id": row.get("ledger_item_id"),
                "segment_id": row.get("segment_id"),
                "decision": decision,
                "corrected_text": "",
                "notes": f"{RULE_VERSION}; {reason}; source_key={row.get('source_key')}",
                "reviewer": reviewer,
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            if len(samples) < 80:
                samples.append(
                    (
                        f"- {decision} | {bucket} | segment={row.get('segment_id')} "
                        f"{row.get('relative_path')}::{row.get('source_key')} | {reason} | {short(queue_text(row), 100)}"
                    )
                )

    report_lines = [
        "Issue review assisted draft",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Source queue: {source_path}",
        f"Reviewer: {reviewer}",
        f"Rows: {len(rows):,}",
        f"Reviewed decisions: {decisions_path}",
        "",
        "Decision counts:",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        "",
        "Bucket counts:",
        *[f"- {key}: {value:,}" for key, value in bucket_counts.most_common()],
        "",
        "Decision by bucket:",
        *[f"- {key}: {value:,}" for key, value in decision_bucket_counts.most_common()],
        "",
        "Samples:",
        *samples,
        "",
        "Safety note:",
        "- This draft creates review evidence only.",
        "- It does not write source/output and does not create corrections for production apply.",
        "- Safe decisions are intentionally conservative.",
    ]
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")

    print("[issue_review_assisted_draft] Draft generated")
    print(f"[issue_review_assisted_draft] Rule version: {RULE_VERSION}")
    print(f"[issue_review_assisted_draft] Rows: {len(rows):,}")
    for key, value in counts.most_common():
        print(f"[issue_review_assisted_draft] {key}: {value:,}")
    print(f"[issue_review_assisted_draft] Decisions: {decisions_path}")
    print(f"[issue_review_assisted_draft] Report: {report_path}")
    return {
        "decisions_path": str(decisions_path),
        "report_path": str(report_path),
        "rows": len(rows),
        "counts": dict(counts),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a conservative assisted decision draft for an issue review queue.")
    parser.add_argument("--queue-jsonl", required=True)
    parser.add_argument("--reviewer", default="codex_assisted_review")
    args = parser.parse_args()
    main(queue_jsonl=args.queue_jsonl, reviewer=args.reviewer)
