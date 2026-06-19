from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_autofix_unknown_domain_guarded_assisted_review_v1"

REPAIR_PATTERNS = [
    (re.compile(r"\bnada perfurante\b", re.IGNORECASE), "awkward_literal:nada_perfurante"),
    (re.compile(r"\btiming\b", re.IGNORECASE), "english_loanword:timing"),
    (re.compile(r"\b(?:ele/ela|ela/ele|meu/minha|seu/sua|teu/tua)\b", re.IGNORECASE), "slash_gender_or_possessive_surface"),
    (re.compile(r"\bal[ée]m de Alp Arslan\b", re.IGNORECASE), "achievement_exception_should_be_exceto_alp_arslan"),
    (re.compile(r"\bde tipo\b", re.IGNORECASE), "literal_phrase:de_tipo"),
    (re.compile(r"\boases?\b", re.IGNORECASE), "foreign_or_awkward_oasis_plural"),
    (re.compile(r"\bVais\b", re.IGNORECASE), "bad_ptbr_vales_as_vais"),
    (re.compile(r"Demais de algo bom", re.IGNORECASE), "literal_idiom:too_much_of_a_good_thing"),
    (re.compile(r"\buma reduto\b", re.IGNORECASE), "gender_agreement:uma_reduto"),
    (re.compile(r"\bpalacianos de templo\b", re.IGNORECASE), "awkward_literal:palacianos_de_templo"),
    (re.compile(r"\bo excesso deste\b", re.IGNORECASE), "awkward_literal:excesso_deste"),
    (re.compile(r"\bcomeçarão todas como \[governments\|lE\] \[administrative\|lE\]", re.IGNORECASE), "awkward_token_order:administrative_governments"),
    (re.compile(r"\buma nada\b", re.IGNORECASE), "broken_ptbr:uma_nada"),
]

CONTEXT_PATTERNS = [
    (re.compile(r"(^|/)(memories|historical_characters|regiment)_", re.IGNORECASE), "domain_flavor_or_memory_context"),
    (re.compile(r"(?:legend_|_flavor|_memory|\.desc\.|_desc_second_perspective)", re.IGNORECASE), "narrative_flavor_context"),
    (re.compile(r"^\"", re.IGNORECASE), "dialogue_quote_context"),
    (re.compile(r"\b(?:pato|fera selvagem|crocodilos|bestas inumanas)\b", re.IGNORECASE), "odd_story_context"),
]


def report_paths(settings: dict[str, Any], queue_path: Path, reviewer: str) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_reviewer = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in reviewer)
    base = reports_dir / f"{stamp}_{queue_path.stem}_{safe_reviewer}_reviewed"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def text_of(row: dict[str, Any]) -> str:
    texts = row.get("texts") if isinstance(row.get("texts"), dict) else {}
    return str(texts.get("confirmed_text") or texts.get("evidence_text") or "")


def path_key(row: dict[str, Any]) -> str:
    return f"{row.get('relative_path') or ''}::{row.get('source_key') or ''}"


def classify(row: dict[str, Any]) -> tuple[str, str]:
    text = text_of(row)
    path_and_key = path_key(row)
    bucket = str(row.get("queue_bucket") or "")
    length = int(row.get("text_length") or len(text))
    words = int(row.get("word_count") or 0)
    tokens = int(row.get("token_count") or 0)

    for pattern, reason in REPAIR_PATTERNS:
        if pattern.search(text) or pattern.search(path_and_key):
            return "needs_repair", reason

    for pattern, reason in CONTEXT_PATTERNS:
        if pattern.search(path_and_key) or pattern.search(text):
            return "needs_domain_context", reason

    if bucket == "plain_achievement_sentence_guarded":
        return "false_positive_reopen", "achievement_instruction_clean_and_token_safe"

    if bucket == "rule_effect_modifier_guarded":
        if tokens <= 6 and length <= 220 and words <= 34:
            return "false_positive_reopen", "modifier_or_rule_surface_clean_single_issue"
        return "needs_domain_context", "rule_modifier_surface_too_dense_for_auto_positive"

    if bucket == "building_description_guarded":
        if length <= 210 and words <= 34 and tokens <= 2:
            return "false_positive_reopen", "building_description_clean_compact_single_issue"
        return "needs_domain_context", "building_description_longer_sample_requires_domain_review"

    if bucket == "plain_general_sentence_guarded":
        if length <= 180 and words <= 28 and tokens <= 3:
            return "false_positive_reopen", "plain_sentence_clean_compact_single_issue"
        return "needs_domain_context", "plain_sentence_longer_or_story_like_requires_review"

    return "needs_domain_context", "unknown_domain_guarded_bucket"


def build_decision(row: dict[str, Any]) -> dict[str, Any]:
    decision, note = classify(row)
    texts = row.get("texts") if isinstance(row.get("texts"), dict) else {}
    return {
        "queue_run_id": row.get("queue_run_id"),
        "checkpoint_run_id": row.get("checkpoint_run_id"),
        "checkpoint_item_id": row.get("checkpoint_item_id"),
        "ledger_item_id": row.get("ledger_item_id"),
        "segment_id": row.get("segment_id"),
        "cluster": row.get("cluster"),
        "subpolicy_name": row.get("queue_bucket"),
        "decision": decision,
        "decision_options": [
            "false_positive_reopen",
            "needs_repair",
            "needs_domain_context",
            "needs_new_microagent",
            "manual_exception",
        ],
        "corrected_text": "",
        "notes": note,
        "evidence_text": texts.get("evidence_text"),
        "english_text": texts.get("english_text"),
        "spanish_text": texts.get("spanish_text"),
        "confirmed_text": texts.get("confirmed_text"),
    }


def write_outputs(decisions_path: Path, report_path: Path, decisions: list[dict[str, Any]]) -> None:
    with decisions_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in decisions:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    counts = Counter(row["decision"] for row in decisions)
    bucket_counts = Counter(f"{row.get('subpolicy_name')}|{row['decision']}" for row in decisions)
    note_counts = Counter(row["notes"] for row in decisions)
    lines = [
        "Autofix Unknown Domain Guarded Assisted Review",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- rows: {len(decisions):,}",
        *[f"- {label}: {count:,}" for label, count in counts.most_common()],
        "",
        "Decision by subpolicy:",
        *[f"- {label}: {count:,}" for label, count in bucket_counts.most_common()],
        "",
        "Reasons:",
        *[f"- {label}: {count:,}" for label, count in note_counts.most_common(30)],
        "",
        "Safety:",
        "- Assisted draft only; no output writes.",
        "- false_positive_reopen is evidence for future checkpointing, not lifecycle authority by itself.",
        "- needs_repair rows should feed smaller wording/lexical neurons before release.",
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, queue_jsonl: str, reviewer: str = "codex_autofix_unknown_domain_guarded_assisted_review") -> dict[str, Any]:
    settings = db.load_settings()
    queue_path = db.project_path(queue_jsonl)
    if not queue_path.exists():
        raise FileNotFoundError(queue_path)
    rows = load_jsonl(queue_path)
    decisions = [build_decision(row) for row in rows]
    decisions_path, report_path = report_paths(settings, queue_path, reviewer)
    write_outputs(decisions_path, report_path, decisions)
    counts = Counter(row["decision"] for row in decisions)
    print("[issue_autofix_unknown_domain_guarded_assisted_review] Draft generated")
    print(f"[issue_autofix_unknown_domain_guarded_assisted_review] Rows: {len(decisions):,}")
    for label, count in counts.most_common():
        print(f"[issue_autofix_unknown_domain_guarded_assisted_review] {label}: {count:,}")
    print(f"[issue_autofix_unknown_domain_guarded_assisted_review] Decisions: {decisions_path}")
    print(f"[issue_autofix_unknown_domain_guarded_assisted_review] Report: {report_path}")
    return {
        "rows": len(decisions),
        "counts": dict(counts),
        "decisions_path": str(decisions_path),
        "report_path": str(report_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Draft assisted decisions for autofix_unknown domain guarded queue.")
    parser.add_argument("--queue-jsonl", required=True)
    parser.add_argument("--reviewer", default="codex_autofix_unknown_domain_guarded_assisted_review")
    args = parser.parse_args()
    main(queue_jsonl=args.queue_jsonl, reviewer=args.reviewer)
