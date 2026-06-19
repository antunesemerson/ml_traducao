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
from issue_semantic_short_label_pair_assisted_review import (
    AWKWARD_PTBR_PATTERNS,
    ENGLISH_PATTERNS,
    SPANISH_OR_BAD_PATTERNS,
    local_validator_reason,
    search_patterns,
    text_without_ck3_references,
)


RULE_VERSION = "issue_semantic_short_label_blocked_context_assisted_review_v1"
DEFAULT_REVIEWER = "codex_semantic_short_label_blocked_context_assisted"

DOMAIN_CONTEXT_KEY_PARTS = (
    ".desc",
    "_desc",
    "_story",
    "story_",
    "_memory",
    "memory_",
    "_opening",
    ".opening",
    "_flavor",
    ".flavor",
    "RandomConflictDescriptor",
    "death_",
)

SAFE_SHORT_KEY_PARTS = (
    "_opinion",
    "_attribute",
    "_modifier",
    "_frequency",
    "_status",
    "_title",
    "_name",
    "_filter",
    "_sort",
    "_prompt",
    "_button",
    "_trait",
    "_perk",
    "_type",
)

BAD_SEMANTIC_PAIRS = (
    ("qualifier replacement", "substitui"),
    ("reneged", "prêmio"),
    ("elephant rider", "montados"),
    ("house paragon", "cavaleiro"),
    ("farrier", "ferreiro"),
    ("weaving interest", "tecelagem"),
)


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


def output_paths(settings: dict[str, Any], source_path: Path, reviewer: str) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_reviewer = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in reviewer)
    base_name = source_path.stem.replace("_decisions_template", "")
    return (
        reports_dir / f"{stamp}_{base_name}_{safe_reviewer}_reviewed.jsonl",
        reports_dir / f"{stamp}_{base_name}_{safe_reviewer}_reviewed.txt",
    )


def short(value: str | None, limit: int = 180) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def normalize(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def word_set(value: str | None) -> set[str]:
    stop = {
        "a",
        "o",
        "os",
        "as",
        "de",
        "da",
        "do",
        "das",
        "dos",
        "em",
        "por",
        "para",
        "com",
        "the",
        "of",
        "in",
        "to",
        "and",
        "el",
        "la",
        "los",
        "las",
        "del",
        "de",
    }
    return {word for word in normalize(value).split() if len(word) >= 4 and word not in stop}


def has_clear_token_or_markup(text: str) -> bool:
    return any(marker in text for marker in ("[", "]", "$", "#", "@"))


def has_domain_context_key(source_key: str) -> bool:
    key = source_key.lower()
    return any(part.lower() in key for part in DOMAIN_CONTEXT_KEY_PARTS)


def has_safe_short_key(source_key: str) -> bool:
    key = source_key.lower()
    return any(part in key for part in SAFE_SHORT_KEY_PARTS)


def semantic_overlap_score(english_text: str, spanish_text: str, current_text: str) -> float:
    current = word_set(current_text)
    if not current:
        return 0.0
    reference = word_set(english_text) | word_set(spanish_text)
    if not reference:
        return 0.0
    return len(current & reference) / max(len(current), 1)


def classify(row: dict[str, Any]) -> tuple[str, str]:
    text = str(row.get("evidence_text") or row.get("confirmed_text") or "")
    english_text = str(row.get("english_text") or "")
    spanish_text = str(row.get("spanish_text") or "")
    source_key = str(row.get("source_key") or "")
    try:
        char_count = int(row.get("char_count") or len(text))
    except (TypeError, ValueError):
        char_count = len(text)
    try:
        token_count = int(row.get("token_count") or 0)
    except (TypeError, ValueError):
        token_count = 0

    if not text.strip():
        return "needs_domain_context", "empty_text"
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
    validator = local_validator_reason(text)
    if validator:
        return "needs_repair", "local_quality_validator:" + validator
    if any(marker in text for marker in ("Select_CString", "SelectLocalization", "Custom(", "Concept(")):
        return "needs_new_microagent", "dynamic_expression_requires_specific_microagent"

    normalized_surface = f"{normalize(english_text)} || {normalize(text)}"
    normalized_text = normalize(text)
    for english_hint, pt_hint in BAD_SEMANTIC_PAIRS:
        if normalize(english_hint) in normalized_surface and normalize(pt_hint) in normalized_text:
            return "needs_domain_context", "known_semantic_nuance_requires_review:" + english_hint

    if has_domain_context_key(source_key) and char_count > 45:
        return "needs_domain_context", "domain_context_key"
    if "." in source_key and re.search(r"\.\d+\.[a-z](?:\.|$)", source_key, re.IGNORECASE):
        return "needs_domain_context", "event_surface_key"
    if char_count > 80 or token_count > 0:
        return "needs_domain_context", "outside_no_token_short_label_scope"

    overlap = semantic_overlap_score(english_text, spanish_text, text)
    if has_safe_short_key(source_key) and char_count <= 70:
        if overlap >= 0.15 or has_clear_token_or_markup(text) or len(word_set(text)) <= 3:
            return "safe_short_label", f"safe_blocked_context_short_label;overlap={overlap:.2f}"

    if source_key.isupper() and char_count <= 55:
        return "safe_short_label", f"safe_uppercase_key_short_label;overlap={overlap:.2f}"

    return "needs_domain_context", f"conservative_blocked_context;overlap={overlap:.2f}"


def main(*, queue_jsonl: str, reviewer: str = DEFAULT_REVIEWER) -> dict[str, Any]:
    settings = db.load_settings()
    source_path = db.project_path(queue_jsonl)
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    rows = load_jsonl(source_path)
    decisions_path, report_path = output_paths(settings, source_path, reviewer)
    counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    package_counts: Counter[str] = Counter()
    samples: list[str] = []

    with decisions_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            decision, reason = classify(row)
            counts[decision] += 1
            reason_counts[reason.split(":", 1)[0]] += 1
            package = str(row.get("package") or "unknown")
            package_counts[f"{decision}|{package}"] += 1
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
            if len(samples) < 100:
                samples.append(
                    f"- {decision} | segment={row.get('segment_id')} {row.get('relative_path')}::{row.get('source_key')} "
                    f"| {reason} | PT={short(row.get('evidence_text'), 80)} | EN={short(row.get('english_text'), 80)}"
                )

    lines = [
        "Semantic short-label blocked-context assisted review",
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
        "Reason counts:",
        *[f"- {key}: {value:,}" for key, value in reason_counts.most_common()],
        "",
        "Decision by package:",
        *[f"- {key}: {value:,}" for key, value in package_counts.most_common(30)],
        "",
        "Samples:",
        *samples,
        "",
        "Safety note:",
        "- This draft creates review evidence only.",
        "- It does not write source/output and does not promote the guard.",
        "- Safe decisions from blocked candidates must pass checkpoint and coverage recalculation before lifecycle closure.",
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("[issue_semantic_short_label_blocked_context_assisted_review] Draft generated")
    print(f"[issue_semantic_short_label_blocked_context_assisted_review] Rows: {len(rows):,}")
    for key, value in counts.most_common():
        print(f"[issue_semantic_short_label_blocked_context_assisted_review] {key}: {value:,}")
    print(f"[issue_semantic_short_label_blocked_context_assisted_review] Decisions: {decisions_path}")
    print(f"[issue_semantic_short_label_blocked_context_assisted_review] Report: {report_path}")
    return {
        "decisions_path": str(decisions_path),
        "report_path": str(report_path),
        "rows": len(rows),
        "counts": dict(counts),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create assisted decisions for blocked semantic short-label context queues.")
    parser.add_argument("--queue-jsonl", required=True)
    parser.add_argument("--reviewer", default=DEFAULT_REVIEWER)
    args = parser.parse_args()
    main(queue_jsonl=args.queue_jsonl, reviewer=args.reviewer)
