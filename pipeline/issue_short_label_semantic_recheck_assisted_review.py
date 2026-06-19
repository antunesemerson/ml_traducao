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


RULE_VERSION = "issue_short_label_semantic_recheck_assisted_review_v1"
DEFAULT_REVIEWER = "codex_short_label_semantic_composition_assisted"

SPANISH_OR_BAD_PATTERNS = [
    r"\bacept[oó]\b",
    r"\bapoy[ao]\b",
    r"\bdecid(i[oó]|iste)\b",
    r"\bdesactivar\b",
    r"\bdisfrut[oó]\b",
    r"\bgan(ar[aá]s|aste|[oó])\b",
    r"\bincineraci[oó]n\b",
    r"\binundaciones\b",
    r"\bse[nñ]or\b",
    r"\bsoledad\b",
    r"\bpuede(s)?\b",
    r"\bpierdes\b",
    r"\bprobabilidad de [eé]xito\b",
    r"#bold\s+no#!",
]

ENGLISH_RESIDUE_PATTERNS = [
    r"\bactivate\b",
    r"\bdeactivate\b",
    r"\bdismiss\b",
    r"\bfloods?\b",
    r"\bgold\b",
    r"\bgrunting\b",
    r"\bin addition\b",
    r"\bis hostile\b",
    r"\bliege\b",
    r"\bmurder\b",
    r"\bsolitude\b",
    r"\bvictory\b",
    r"\bthe\b",
    r"\bwill\b",
    r"\byou\b",
    r"\bof\b",
    r"\band\b",
    r"\bto\b",
]

DYNAMIC_MARKERS = (
    "Select_CString",
    "SelectLocalization",
    "Custom(",
    "Concept(",
    "LocalPlayerString",
    "GetScriptedGui",
)

CONTEXT_KEY_HINTS = (
    "_desc",
    ".desc",
    "_tooltip",
    "_tt",
    "RandomConflictDescriptor",
    "death_",
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


def short(value: str | None, limit: int = 180) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


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


def visible_text(text: str) -> str:
    masked = re.sub(r"\[[^\]]+\]", " ", text)
    masked = re.sub(r"\$[^$]+\$", " ", masked)
    masked = re.sub(r"#[A-Za-z0-9_]+|#!|@[A-Za-z0-9_]+!", " ", masked)
    return re.sub(r"\s+", " ", masked).strip()


def preservation_exception(row: dict[str, Any], visible: str) -> bool:
    source_key = str(row.get("source_key") or "")
    relative_path = str(row.get("relative_path") or "")
    if relative_path.endswith("custom_localization/regional_custom_loc_l_spanish.yml") and source_key.startswith("dog_type_"):
        return True
    if source_key.startswith("dog_type_"):
        return True
    # A few glossary-like rows are intentionally only CK3 tokens; they have no visible text after masking.
    return not visible.strip()


def validation_issue_codes(text: str) -> tuple[list[str], list[str]]:
    result = validate_text(text)
    issues = result.get("issues", []) if isinstance(result, dict) else getattr(result, "issues", [])
    high_codes: list[str] = []
    medium_codes: list[str] = []
    for issue in issues:
        if isinstance(issue, dict):
            code = str(issue.get("code") or "")
            severity = str(issue.get("severity") or "")
            matches = issue.get("matches") or []
        else:
            code = str(getattr(issue, "code", "") or "")
            severity = str(getattr(issue, "severity", "") or "")
            matches = getattr(issue, "matches", []) or []
        if severity in {"high", "error", "critical"}:
            high_codes.append(code or severity)
            continue
        if severity == "medium":
            # The current validator intentionally overflags short PT-BR plurals ending in "es".
            if code == "spanish_residue" and matches == ["es"]:
                continue
            medium_codes.append(code or severity)
    return high_codes, medium_codes


def classify(row: dict[str, Any]) -> tuple[str, str]:
    text = str(row.get("confirmed_text") or row.get("evidence_text") or "")
    english = str(row.get("english_text") or "")
    source_key = str(row.get("source_key") or "")
    queue_bucket = str(row.get("queue_bucket") or "")
    coverage_state = str(row.get("coverage_state") or "")
    review_state = str(row.get("review_state") or "")
    try:
        total_issues = int(row.get("total_issue_count") or 0)
        covered_issues = int(row.get("covered_issue_count") or 0)
        open_issues = int(row.get("open_issue_count") or 0)
        word_count = int(row.get("word_count") or 0)
    except (TypeError, ValueError):
        return "needs_domain_context", "invalid_issue_or_word_count"

    if not text.strip():
        return "needs_domain_context", "empty_current_text"
    if coverage_state != "full" or open_issues != 0 or covered_issues < total_issues:
        return "needs_domain_context", "not_fully_covered_by_issue_neurons"
    if review_state not in {"review_none", "", "none"}:
        return "needs_domain_context", "existing_review_state_requires_manual_context"

    high_codes, medium_codes = validation_issue_codes(text)
    if high_codes:
        return "needs_repair", "local_quality_validator_high:" + ",".join(high_codes[:4])

    raw_spanish_hits = search_patterns(text, SPANISH_OR_BAD_PATTERNS)
    if raw_spanish_hits:
        return "needs_repair", "spanish_or_bad_surface:" + ",".join(raw_spanish_hits[:3])

    clean_visible = visible_text(text)
    english_visible = visible_text(english)
    if (
        clean_visible
        and english_visible
        and clean_visible.casefold() == english_visible.casefold()
        and not preservation_exception(row, clean_visible)
    ):
        return "needs_repair", "visible_text_matches_english_reference"
    english_hits = search_patterns(clean_visible, ENGLISH_RESIDUE_PATTERNS)
    if english_hits and clean_visible.casefold() != english.casefold():
        return "needs_repair", "english_residue_surface:" + ",".join(english_hits[:3])

    if any(marker in text for marker in DYNAMIC_MARKERS):
        return "needs_new_microagent", "dynamic_marker_requires_specific_composition_policy"
    if any(hint in source_key for hint in CONTEXT_KEY_HINTS) and word_count > 8:
        return "needs_domain_context", "sentence_or_tooltip_context_hint"
    if "long" in queue_bucket:
        return "needs_domain_context", "long_surface_not_owned_by_tier1_short_compact_policy"
    if medium_codes and word_count > 8:
        return "needs_domain_context", "medium_validator_issue_on_longer_surface:" + ",".join(medium_codes[:4])
    return "composition_ready", "fully_covered_short_semantic_tier1_passed_local_guards"


def main(*, queue_jsonl: str, reviewer: str = DEFAULT_REVIEWER) -> dict[str, Any]:
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
            if len(samples) < 100:
                samples.append(
                    f"- {decision} | {bucket} | segment={row.get('segment_id')} "
                    f"{row.get('relative_path')}::{row.get('source_key')} | {reason} | {short(row.get('confirmed_text'), 110)}"
                )

    report_lines = [
        "Short-label + semantic whole-segment assisted recheck",
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
        "- This draft creates learning evidence only.",
        "- It does not write source/output and does not promote lifecycle closure.",
        "- `composition_ready` only means this queue item passed conservative local guards.",
    ]
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")

    print("[issue_short_label_semantic_recheck_assisted_review] Draft generated")
    print(f"[issue_short_label_semantic_recheck_assisted_review] Rule version: {RULE_VERSION}")
    print(f"[issue_short_label_semantic_recheck_assisted_review] Rows: {len(rows):,}")
    for key, value in counts.most_common():
        print(f"[issue_short_label_semantic_recheck_assisted_review] {key}: {value:,}")
    print(f"[issue_short_label_semantic_recheck_assisted_review] Decisions: {decisions_path}")
    print(f"[issue_short_label_semantic_recheck_assisted_review] Report: {report_path}")
    return {
        "decisions_path": str(decisions_path),
        "report_path": str(report_path),
        "rows": len(rows),
        "counts": dict(counts),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Create a conservative assisted decision draft for short-label + semantic composition recheck queues."
    )
    parser.add_argument("--queue-jsonl", required=True)
    parser.add_argument("--reviewer", default=DEFAULT_REVIEWER)
    args = parser.parse_args()
    main(queue_jsonl=args.queue_jsonl, reviewer=args.reviewer)
