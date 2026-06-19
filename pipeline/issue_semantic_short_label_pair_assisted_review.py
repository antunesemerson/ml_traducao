from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from local_quality_validator import validate_text


RULE_VERSION = "issue_semantic_short_label_pair_assisted_review_v16"
AGENT_KEY = "coordinator_semantic_short_label_pair"

SPANISH_OR_BAD_PATTERNS = [
    r"\bDirecci[oó]n\b",
    r"\bProducci[oó]n\b",
    r"\bSeñor[ií]o\b",
    r"\bser[aá]n\b",
    r"\bde inmediato\b",
    r"\bpuede(s)?\b",
    r"\bganar[aá](s)?\b",
    r"\bpierdes\b",
    r"\blegitimidad\b",
    r"\bmi poder\b",
    r"\bAlmac",
    r"\bpeon",
    r"\bp.{0,2}rate\b",
    r"\bprobabilidad de [eé]xito\b",
    r"\bdiablill[oa]s?\b",
    r"#bold\s+No#!",
]

ENGLISH_PATTERNS = [
    r"\bthe\b",
    r"\bwill\b",
    r"\byou\b",
    r"\bof\b",
    r"\band\b",
    r"\bto\b",
    r"\bodds\b",
    r"\btrinket\b",
    r"\bsubjugate\b",
    r"\binvade\b",
    r"\bmigrate\b",
    r"\bcreated\b",
    r"\brenamed\b",
    r"\balways\b",
    r"\ballowed\b",
    r"\bFind Spouse\b",
    r"\bEnd Internal War\b",
    r"\bWest Francia in\b",
    r"\]\s+for\s+\[",
    r"\$[A-Za-z0-9_]+\$\s+for\s+\[",
    r"\bculture head\b",
    r"\bplaceholder\b",
    r"\binsights?\b",
    r"\btooltips?\b",
    r"\blevy\b",
    r"\bpaddocks?\b",
    r"\bUnlocks?\b",
    r"\bUnlocked\b",
    r"\bVictory\b",
    r"\bDefeat\b",
    r"\bFocus\b",
    r"\bIncrease\b",
    r"\bLowered\b",
    r"\bVassal\b",
    r"\bOpinion\b",
    r"\bIncrease County Opinion\b",
]

AWKWARD_PTBR_PATTERNS = [
    r"\bA embelezamento\b",
    r"\ba separador\b",
    r"\ba\(s\)\b",
    r"\b[A-Za-z]*enthusi",
    r"\blinx\b",
    r"\bseu placeholder\b",
    r"\b\w+ad\s+(?:a|o|as|os|aos)\b",
    r"\buma [aeiouáéíóúâêôãõ]",
]

DOMAIN_CONTEXT_KEY_HINTS = (
    "RandomConflictDescriptor",
    "death_",
    "_desc",
    ".desc",
    "_tooltip",
    "_tt",
)

EVENT_OPTION_KEY_RE = re.compile(r"\.\d+\.[a-z](?:\.|$)", flags=re.IGNORECASE)
BANE_NAME_RE = re.compile(r"bane\b", flags=re.IGNORECASE)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"JSONL line {line_number} is not an object")
        rows.append(payload)
    return rows


def short(value: str | None, limit: int = 180) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


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


def search_patterns(text: str, patterns: list[str]) -> list[str]:
    return [pattern for pattern in patterns if re.search(pattern, text, flags=re.IGNORECASE)]


def text_without_ck3_references(text: str) -> str:
    masked = re.sub(r"\[[^\]]+\]", " ", text)
    masked = re.sub(r"\$[^$]+\$", " ", masked)
    return masked


def local_validator_reason(text: str) -> str:
    try:
        result = validate_text(text)
    except TypeError:
        result = validate_text(text, "", "")
    issues = getattr(result, "issues", None) or []
    high = []
    for issue in issues:
        code = getattr(issue, "code", "") or ""
        severity = getattr(issue, "severity", "") or ""
        if severity in {"high", "error", "critical"}:
            high.append(code or severity)
    return ",".join(high[:4])


def classify(row: dict[str, Any]) -> tuple[str, str]:
    text = str(row.get("evidence_text") or "")
    source_key = str(row.get("source_key") or "")
    english_text = str(row.get("english_text") or "")
    bucket = str(row.get("queue_bucket") or "")
    if ":" in bucket:
        bucket = bucket.rsplit(":", 1)[-1]
    try:
        token_count = int(row.get("token_count") or 0)
    except (TypeError, ValueError):
        token_count = 0
    try:
        char_count = int(row.get("char_count") or len(text))
    except (TypeError, ValueError):
        char_count = len(text)

    if not text.strip():
        return "needs_domain_context", "empty_evidence_text"
    spanish_hits = search_patterns(text, SPANISH_OR_BAD_PATTERNS)
    if spanish_hits:
        return "needs_repair", "spanish_or_bad_literal:" + ",".join(spanish_hits[:3])
    visible_text = text_without_ck3_references(text)
    english_hits = search_patterns(visible_text, ENGLISH_PATTERNS)
    if english_hits:
        return "needs_repair", "english_or_placeholder_literal:" + ",".join(english_hits[:3])
    awkward_hits = search_patterns(text, AWKWARD_PTBR_PATTERNS)
    if awkward_hits:
        return "needs_repair", "awkward_ptbr_literal:" + ",".join(awkward_hits[:3])
    semantic_surface = f"{source_key} {english_text}".lower()
    if ("coerced" in semantic_surface or "forced" in semantic_surface) and re.search(r"\bvolunt", text, flags=re.IGNORECASE):
        return "needs_repair", "semantic_contradiction:coerced_or_forced_vs_voluntary"
    validator = local_validator_reason(text)
    if validator:
        return "needs_repair", "local_quality_validator:" + validator
    if any(marker in text for marker in ("Select_CString", "SelectLocalization", "Custom(", "Concept(")):
        return "needs_new_microagent", "dynamic_expression_requires_specific_microagent"
    if EVENT_OPTION_KEY_RE.search(source_key) and ".t" not in source_key[-3:].lower():
        return "needs_domain_context", "event_option_or_tooltip_surface_requires_event_microagent"
    if BANE_NAME_RE.search(english_text) or "bane" in source_key.lower():
        return "needs_domain_context", "artifact_name_bane_semantics_requires_review"
    if char_count > 120 or token_count > 4:
        return "needs_domain_context", "too_long_or_many_tokens_for_semantic_short_label_bridge"
    if any(hint in source_key for hint in DOMAIN_CONTEXT_KEY_HINTS) and char_count > 55:
        return "needs_domain_context", "domain_context_hint_on_sentence_like_label"
    if bucket == "pair_no_token_short_label" and char_count <= 80 and token_count == 0:
        return "safe_short_label", "clean_semantic_short_label_pair_no_token"
    if bucket == "pair_low_token_short_text" and char_count <= 100 and token_count <= 2:
        return "safe_short_label", "clean_semantic_short_label_pair_low_token"
    if bucket == "pair_medium_token_short_ui" and char_count <= 120 and token_count <= 4:
        return "safe_short_label", "clean_semantic_short_label_pair_medium_ui"
    return "needs_domain_context", "conservative_fallback"


def main(*, queue_jsonl: str, reviewer: str = "codex_semantic_short_label_assisted") -> dict[str, Any]:
    settings = db.load_settings()
    source_path = db.project_path(queue_jsonl)
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    rows = load_jsonl(source_path)
    decisions_path, report_path = output_paths(settings, source_path, reviewer)
    counts: Counter[str] = Counter()
    bucket_counts: Counter[str] = Counter()
    decision_bucket_counts: Counter[str] = Counter()
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
                "queue_item_id": row.get("queue_item_id"),
                "ledger_item_id": row.get("ledger_item_id"),
                "segment_id": row.get("segment_id"),
                "decision": decision,
                "corrected_text": "",
                "notes": f"{RULE_VERSION}; {reason}; source_key={row.get('source_key')}",
                "reviewer": reviewer,
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            if len(samples) < 90:
                samples.append(
                    f"- {decision} | {bucket} | segment={row.get('segment_id')} "
                    f"{row.get('relative_path')}::{row.get('source_key')} | {reason} | {short(row.get('evidence_text'), 110)}"
                )

    report_lines = [
        "Semantic + short-label assisted review",
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
        "- Safe decisions are conservative and should feed a guarded checkpoint before lifecycle closure.",
    ]
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")

    print("[issue_semantic_short_label_pair_assisted_review] Draft generated")
    print(f"[issue_semantic_short_label_pair_assisted_review] Rule version: {RULE_VERSION}")
    print(f"[issue_semantic_short_label_pair_assisted_review] Rows: {len(rows):,}")
    for key, value in counts.most_common():
        print(f"[issue_semantic_short_label_pair_assisted_review] {key}: {value:,}")
    print(f"[issue_semantic_short_label_pair_assisted_review] Decisions: {decisions_path}")
    print(f"[issue_semantic_short_label_pair_assisted_review] Report: {report_path}")
    return {
        "decisions_path": str(decisions_path),
        "report_path": str(report_path),
        "rows": len(rows),
        "counts": dict(counts),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a conservative assisted review for semantic + short-label pair queues.")
    parser.add_argument("--queue-jsonl", required=True)
    parser.add_argument("--reviewer", default="codex_semantic_short_label_assisted")
    args = parser.parse_args()
    main(queue_jsonl=args.queue_jsonl, reviewer=args.reviewer)
