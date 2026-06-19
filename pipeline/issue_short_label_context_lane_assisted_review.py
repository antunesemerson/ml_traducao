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


RULE_VERSION = "issue_short_label_context_lane_assisted_review_v1"
DEFAULT_REVIEWER = "codex_custom_localization_fragment_assisted"
CUSTOM_FRAGMENT_AWKWARD_PATTERNS = [
    pattern for pattern in AWKWARD_PTBR_PATTERNS if r"\buma [aeiou" not in pattern
]

CONTEXTUAL_STARTERS = (
    "e,",
    "mas ",
    "porém ",
    "contudo ",
    "entretanto ",
    "embora ",
    "quando ",
    "se ",
    "caso ",
    "porque ",
)

CUSTOM_FRAGMENT_SAFE_KEY_HINTS = (
    "_quirk",
    "body_of_water_",
    "terrain_",
    "creature_",
    "CoronationSite",
    "RandomCoronationObject",
    "weapon_",
    "insult",
    "compliment",
    "pet_",
)

CUSTOM_FRAGMENT_CONTEXT_KEY_HINTS = (
    "Descriptor",
    "Exchange",
    "Opinion",
    "sentence",
    "phrase",
    "poem",
    "poetry",
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


def has_mojibake(text: str) -> bool:
    return bool(re.search(r"(?:Ã.|Â.|â€|�)", text))


def starts_or_ends_like_context_fragment(text: str) -> bool:
    stripped = text.strip()
    lowered = stripped.lower()
    if not stripped:
        return True
    if stripped.endswith((",", ";", ":", "—", "-")):
        return True
    if stripped.startswith((",", ";", ":", "—", "-")):
        return True
    return any(lowered.startswith(starter) for starter in CONTEXTUAL_STARTERS)


def has_ck3_dynamic_surface(text: str) -> bool:
    return any(marker in text for marker in ("[", "]", "$", "#", "Select_CString", "SelectLocalization", "Custom(", "Concept("))


def safe_key_hint(source_key: str) -> bool:
    return any(hint in source_key for hint in CUSTOM_FRAGMENT_SAFE_KEY_HINTS)


def context_key_hint(source_key: str, relative_path: str) -> bool:
    key = source_key.lower()
    path = relative_path.lower()
    if any(hint.lower() in key for hint in CUSTOM_FRAGMENT_CONTEXT_KEY_HINTS):
        return True
    if "poetry" in path or "conversation_subjects" in path:
        return True
    return False


def looks_like_clean_noun_or_action_fragment(text: str, *, word_count: int, text_length: int) -> bool:
    if text_length > 80 or word_count > 12:
        return False
    if starts_or_ends_like_context_fragment(text):
        return False
    if re.search(r"[.!?]$", text):
        return False
    if re.search(r"\b(?:eu|você|tu|nós|eles|elas)\b", text, flags=re.IGNORECASE):
        return False
    return True


def classify(row: dict[str, Any]) -> tuple[str, str]:
    text = str(row.get("evidence_text") or "")
    source_key = str(row.get("source_key") or "")
    relative_path = str(row.get("relative_path") or "")
    try:
        word_count = int(row.get("word_count") or len([part for part in text.split() if part]))
    except (TypeError, ValueError):
        word_count = len([part for part in text.split() if part])
    try:
        text_length = int(row.get("text_length") or len(text))
    except (TypeError, ValueError):
        text_length = len(text)

    if not text.strip():
        return "needs_domain_context", "empty_fragment"
    if has_mojibake(text):
        return "needs_repair", "mojibake_surface"
    spanish_hits = search_patterns(text, SPANISH_OR_BAD_PATTERNS)
    if spanish_hits:
        return "needs_repair", "spanish_or_bad_literal:" + ",".join(spanish_hits[:3])
    visible_text = text_without_ck3_references(text)
    english_hits = search_patterns(visible_text, ENGLISH_PATTERNS)
    if english_hits:
        return "needs_repair", "english_or_placeholder_literal:" + ",".join(english_hits[:3])
    awkward_hits = search_patterns(text, CUSTOM_FRAGMENT_AWKWARD_PATTERNS)
    if awkward_hits:
        return "needs_repair", "awkward_ptbr_literal:" + ",".join(awkward_hits[:3])
    validator = local_validator_reason(text)
    if validator:
        return "needs_repair", "local_quality_validator:" + validator
    if has_ck3_dynamic_surface(text):
        return "needs_new_microagent", "dynamic_or_markup_fragment_requires_specific_policy"

    normalized_text = normalize(text)
    normalized_key = normalize(source_key)
    if ("khanal" in normalized_key or "khaganal" in normalized_key) and normalized_text in {"canal", "kaganal"}:
        return "needs_domain_context", "khan_or_khagan_adjective_requires_title_context"
    if normalized_text in {"de", "da", "do", "das", "dos", "em", "para", "por", "com"}:
        return "needs_domain_context", "bare_preposition_fragment"

    clean_fragment = looks_like_clean_noun_or_action_fragment(text, word_count=word_count, text_length=text_length)
    if safe_key_hint(source_key) and clean_fragment:
        return "safe_short_label", "safe_fragment:key_hint_clean_ptbr"

    if relative_path.endswith("appropriate_generic_words_l_spanish.yml") and clean_fragment and word_count <= 5:
        return "safe_short_label", "safe_fragment:generic_word_or_short_phrase"

    if context_key_hint(source_key, relative_path):
        if clean_fragment and text_length <= 45 and word_count <= 7:
            return "safe_short_label", "safe_fragment:contextual_key_but_clean_short_surface"
        return "needs_domain_context", "contextual_custom_fragment_requires_host_sentence"

    if clean_fragment and text_length <= 60:
        return "safe_short_label", "safe_fragment:clean_short_custom_localization"

    return "needs_domain_context", "conservative_custom_localization_fragment"


def main(*, queue_jsonl: str, reviewer: str = DEFAULT_REVIEWER) -> dict[str, Any]:
    settings = db.load_settings()
    source_path = db.project_path(queue_jsonl)
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    rows = load_jsonl(source_path)
    decisions_path, report_path = output_paths(settings, source_path, reviewer)
    counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    path_counts: Counter[str] = Counter()
    decision_path_counts: Counter[str] = Counter()
    samples: list[str] = []

    with decisions_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            decision, reason = classify(row)
            counts[decision] += 1
            reason_counts[reason.split(":", 1)[0]] += 1
            relative_path = str(row.get("relative_path") or "unknown")
            path_counts[relative_path] += 1
            decision_path_counts[f"{decision}|{relative_path}"] += 1
            payload = {
                "queue_run_id": row.get("queue_run_id"),
                "queue_item_id": row.get("queue_item_id"),
                "ledger_item_id": row.get("ledger_item_id"),
                "segment_id": row.get("segment_id"),
                "decision": decision,
                "corrected_text": "",
                "notes": f"{RULE_VERSION}; {reason}; fragment_role=custom_localization; source_key={row.get('source_key')}",
                "reviewer": reviewer,
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            if len(samples) < 100:
                samples.append(
                    f"- {decision} | segment={row.get('segment_id')} {row.get('relative_path')}::{row.get('source_key')} "
                    f"| {reason} | PT={short(row.get('evidence_text'), 90)}"
                )

    lines = [
        "Short-label context-lane assisted review",
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
        "Decision by file:",
        *[f"- {key}: {value:,}" for key, value in decision_path_counts.most_common(40)],
        "",
        "Queue file distribution:",
        *[f"- {key}: {value:,}" for key, value in path_counts.most_common(30)],
        "",
        "Samples:",
        *samples,
        "",
        "Safety note:",
        "- This draft creates review evidence only.",
        "- It does not write source/output and does not promote lifecycle authority.",
        "- Safe decisions mean the fragment surface is acceptable, not that an entire segment should be closed without composition checks.",
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("[issue_short_label_context_lane_assisted_review] Draft generated")
    print(f"[issue_short_label_context_lane_assisted_review] Rows: {len(rows):,}")
    for key, value in counts.most_common():
        print(f"[issue_short_label_context_lane_assisted_review] {key}: {value:,}")
    print(f"[issue_short_label_context_lane_assisted_review] Decisions: {decisions_path}")
    print(f"[issue_short_label_context_lane_assisted_review] Report: {report_path}")
    return {
        "decisions_path": str(decisions_path),
        "report_path": str(report_path),
        "rows": len(rows),
        "counts": dict(counts),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create assisted decisions for short-label context-lane queues.")
    parser.add_argument("--queue-jsonl", required=True)
    parser.add_argument("--reviewer", default=DEFAULT_REVIEWER)
    args = parser.parse_args()
    main(queue_jsonl=args.queue_jsonl, reviewer=args.reviewer)
