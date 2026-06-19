from __future__ import annotations

import argparse
import json
import re
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


RULE_VERSION = "issue_short_label_compact_ui_semantic_assisted_review_v1"
DEFAULT_REVIEWER = "codex_short_label_compact_ui_semantic_assisted"

EVENT_CONTEXT_KEY = re.compile(r"\.(?:a|b|c|d|e|f|g|h|i|j|success|failure|toast)(?:\.|$)", re.IGNORECASE)
EVENT_CONTEXT_PATH = re.compile(r"(?:^|/)event_localization/|_events?_l_spanish\.yml", re.IGNORECASE)
QUOTE_OR_DIALOGUE = re.compile(r"^\s*(?:\"|«|“)|(?:\"|»|”)\s*$")
CONTEXTUAL_PRONOUN = re.compile(r"\b(?:eu|meu|minha|voc[êe]|tu|te|seu|sua|ele|ela|eles|elas)\b", re.IGNORECASE)
RELATION_OR_NICKNAME_KEY = re.compile(r"(?:relation|nick_|nickname|family|parent|child|spouse|cousin|uncle|aunt)", re.IGNORECASE)
DOMAIN_MICROAGENT_KEY = re.compile(r"(?:activity|coronation|hunt|pilgrimage|travel|struggle|succession|law|doctrine)", re.IGNORECASE)


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


def text_payload(row: dict[str, Any]) -> str:
    texts = row.get("texts") if isinstance(row.get("texts"), dict) else {}
    return str(texts.get("confirmed_text") or texts.get("evidence_text") or row.get("evidence_text") or "")


def evidence(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("evidence")
    return value if isinstance(value, dict) else {}


def has_bad_language_surface(text: str) -> str | None:
    if re.search(r"(?:Ãƒ.|Ã‚.|Ã¢â‚¬|ï¿½)", text):
        return "mojibake_surface"
    hits = search_patterns(text, SPANISH_OR_BAD_PATTERNS)
    if hits:
        return "spanish_or_bad_literal:" + ",".join(hits[:3])
    visible = text_without_ck3_references(text)
    hits = search_patterns(visible, ENGLISH_PATTERNS)
    if hits:
        return "english_or_placeholder_literal:" + ",".join(hits[:3])
    hits = search_patterns(text, AWKWARD_PTBR_PATTERNS)
    if hits:
        return "awkward_ptbr_literal:" + ",".join(hits[:3])
    validator = local_validator_reason(text)
    if validator:
        return "local_quality_validator:" + validator
    return None


def classify(row: dict[str, Any]) -> tuple[str, str]:
    text = text_payload(row)
    source_key = str(row.get("source_key") or "")
    relative_path = str(row.get("relative_path") or "")
    queue_bucket = str(row.get("queue_bucket") or "")
    ev = evidence(row)

    if not text.strip():
        return "needs_domain_context", "empty_surface"
    bad_reason = has_bad_language_surface(text)
    if bad_reason:
        return "needs_repair", bad_reason
    if ev.get("active_token_status") not in (None, "ok") or ev.get("candidate_token_status") not in (None, "ok"):
        return "needs_repair", "token_status_not_ok"
    if ev.get("issue_codes"):
        return "needs_repair", "quality_issue_codes_present"
    if RELATION_OR_NICKNAME_KEY.search(source_key) or "nicknames" in relative_path.lower():
        return "needs_new_microagent", "relation_or_nickname_short_label_requires_domain_policy"
    if DOMAIN_MICROAGENT_KEY.search(source_key) and "domain_general" not in queue_bucket:
        return "needs_new_microagent", "domain_specific_short_label_requires_dedicated_policy"
    if QUOTE_OR_DIALOGUE.search(text):
        return "needs_domain_context", "quoted_or_dialogue_surface_requires_event_context"
    if EVENT_CONTEXT_PATH.search(relative_path) and EVENT_CONTEXT_KEY.search(source_key):
        return "needs_domain_context", "event_option_or_toast_requires_event_context"
    if CONTEXTUAL_PRONOUN.search(text) and "triggers/" not in relative_path.replace("\\", "/").lower():
        return "needs_domain_context", "pronoun_surface_requires_context"

    suggested = str(row.get("suggested_decision") or "")
    if suggested == "false_positive_reopen":
        return "false_positive_reopen", "confirmed_output_is_clean_compact_ui_surface"
    return "needs_domain_context", "not_preselected_as_false_positive_reopen"


def main(*, queue_jsonl: str, reviewer: str = DEFAULT_REVIEWER) -> dict[str, Any]:
    settings = db.load_settings()
    source_path = db.project_path(queue_jsonl)
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    rows = load_jsonl(source_path)
    decisions_path, report_path = output_paths(settings, source_path, reviewer)

    counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    bucket_counts: Counter[str] = Counter()
    samples: list[str] = []

    with decisions_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            decision, reason = classify(row)
            counts[decision] += 1
            reason_counts[reason.split(":", 1)[0]] += 1
            bucket = str(row.get("queue_bucket") or "unknown")
            bucket_counts[f"{bucket}|{decision}"] += 1
            payload = {
                "queue_run_id": row.get("queue_run_id"),
                "queue_item_id": row.get("queue_item_id"),
                "ledger_item_id": row.get("ledger_item_id"),
                "segment_id": row.get("segment_id"),
                "decision": decision,
                "corrected_text": "",
                "notes": f"{RULE_VERSION}; {reason}; queue_bucket={bucket}; source_key={row.get('source_key')}",
                "reviewer": reviewer,
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            if len(samples) < 80:
                samples.append(
                    f"- {decision} | segment={row.get('segment_id')} {row.get('relative_path')}::{row.get('source_key')} "
                    f"| {reason} | PT={short(text_payload(row), 110)}"
                )

    lines = [
        "Short-label compact UI semantic assisted review",
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
        "Decision by bucket:",
        *[f"- {key}: {value:,}" for key, value in bucket_counts.most_common(40)],
        "",
        "Samples:",
        *samples,
        "",
        "Safety note:",
        "- Assisted draft only.",
        "- No source/output writes.",
        "- False-positive decisions are issue-level evidence, not segment closure.",
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("[issue_short_label_compact_ui_semantic_assisted_review] Draft generated")
    print(f"[issue_short_label_compact_ui_semantic_assisted_review] Rows: {len(rows):,}")
    for label, count in counts.most_common():
        print(f"[issue_short_label_compact_ui_semantic_assisted_review] {label}: {count:,}")
    print(f"[issue_short_label_compact_ui_semantic_assisted_review] Decisions: {decisions_path}")
    print(f"[issue_short_label_compact_ui_semantic_assisted_review] Report: {report_path}")
    return {"decisions_path": str(decisions_path), "report_path": str(report_path), "counts": dict(counts)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Draft assisted decisions for compact UI semantic short-label queue.")
    parser.add_argument("--queue-jsonl", required=True)
    parser.add_argument("--reviewer", default=DEFAULT_REVIEWER)
    args = parser.parse_args()
    main(queue_jsonl=args.queue_jsonl, reviewer=args.reviewer)
