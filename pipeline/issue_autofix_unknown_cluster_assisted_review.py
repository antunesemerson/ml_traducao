from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_autofix_unknown_cluster_assisted_review_v1"

SPANISH_OR_LITERAL_BAD = re.compile(
    r"\b("
    r"muy|yo|despiadad[oa]s?|herramienta|fan[aá]tic[oa]s?|"
    r"levies|charioteers|cannot|can not|the|your|you"
    r")\b",
    re.IGNORECASE,
)
BROKEN_SURFACE = re.compile(r"\b(uma um|um uma|o a|a o)\b", re.IGNORECASE)
EXTRA_RESIDUAL_BAD = re.compile(r"\b(interesante|estudios)\b", re.IGNORECASE)
SPANISH_MARKUP_BAD = re.compile(r"#X\s+no\s*#!|#t\s+Estudios\b", re.IGNORECASE)
DANGLING_GENDER_STEM = re.compile(r"\b(?:atrasad|lan[çc]ad|cansad|casad|ferid|ocupad|preparad)\b", re.IGNORECASE)
TRAILING_PREPOSITION = re.compile(r"\b(?:em|de|da|do|para|por|com)\s*$", re.IGNORECASE)
CONFIRM_KEY = re.compile(r"(?:CONFIRM|CONFIRMATION|_CONFIRM$|_ASK_|ASK_)", re.IGNORECASE)
TOOLTIP_KEY = re.compile(r"(?:_TT$|_TOOLTIP$|TOOLTIP|_tt_|_tt$)", re.IGNORECASE)
STRICT_WARNING_PREFIX = re.compile(
    r"^\s*(?:\$[A-Za-z0-9_]+\$\s*)?(?:@warning_icon!|#X|#warning|@alert_icon!)",
    re.IGNORECASE,
)
COMPACT_WARNING_KEY = re.compile(r"(?:CANNOT|CANT|CAN_NOT|WONT|TOO_|BLOCK|WARNING|_TT$|_TOOLTIP$)", re.IGNORECASE)
WARNING_MARKER = re.compile(r"@warning_icon!|#X|Você não pode|Não é possível|Isto é um ato", re.IGNORECASE)
LIST_MARKER = re.compile(r"^\s*(?:\$EFFECT_LIST_BULLET\$|\$BULLET|\$BULLET_WITH_TAB\$)|(?:\\n|^)\s*(?:\$EFFECT_LIST_BULLET\$|\$BULLET)", re.IGNORECASE)
NARRATIVE_KEY = re.compile(r"\.(?:desc|toast|flavor)$|_desc$|description|proposal$", re.IGNORECASE)


def report_paths(settings: dict[str, Any], queue_path: Path) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    stem = queue_path.stem
    base = reports_dir / f"{stamp}_{stem}_codex_autofix_unknown_cluster_assisted_reviewed"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def load_rows(path: Path) -> list[dict[str, Any]]:
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


def text_payload(row: dict[str, Any]) -> str:
    texts = row.get("texts") if isinstance(row.get("texts"), dict) else {}
    for key in ("confirmed_text", "evidence_text", "english_text", "spanish_text"):
        value = texts.get(key)
        if value:
            return str(value)
    return str(row.get("evidence_text") or "")


def key_of(row: dict[str, Any]) -> str:
    return str(row.get("source_key") or "")


def is_compact_warning_surface(text: str, key: str) -> bool:
    if STRICT_WARNING_PREFIX.search(text) and not NARRATIVE_KEY.search(key):
        return len(text) <= 280 and text.count("\n") <= 2
    if len(text) <= 180 and text.count("\n") <= 1 and COMPACT_WARNING_KEY.search(key):
        return bool(
            re.search(
                r"VocÃª nÃ£o pode|NÃ£o Ã© possÃ­vel|nÃ£o pode|nÃ£o poderÃ¡|indispon[iÃ­]vel|desativar[Ã¡a]|bloquead",
                text,
                re.IGNORECASE,
            )
        )
    return False


def decide(row: dict[str, Any]) -> tuple[str, str]:
    cluster = str(row.get("queue_bucket") or row.get("cluster") or "")
    text = text_payload(row)
    key = key_of(row)

    if (
        SPANISH_OR_LITERAL_BAD.search(text)
        or EXTRA_RESIDUAL_BAD.search(text)
        or SPANISH_MARKUP_BAD.search(text)
        or BROKEN_SURFACE.search(text)
        or DANGLING_GENDER_STEM.search(text)
        or ("#X" in text and "#!" not in text)
    ):
        return "needs_repair", "visible residual Spanish, English/literal residue, or broken PT-BR surface detected"
    if TRAILING_PREPOSITION.search(text.strip()):
        return "needs_domain_context", "surface appears truncated or depends on continuation context"

    if cluster == "ui_warning_or_blocker_text":
        if is_compact_warning_surface(text, key):
            return "safe_surface", "clear warning/blocker UI surface with no detected text repair"
        if WARNING_MARKER.search(text):
            return "needs_domain_context", "warning marker appears outside a compact governed UI surface"
        return "needs_domain_context", "warning cluster lacks a strong warning marker"

    if cluster == "confirmation_or_question_text":
        if CONFIRM_KEY.search(key) and len(text) <= 180:
            return "safe_surface", "short confirmation/ask surface with no detected repair"
        return "needs_domain_context", "question-like prose may depend on gameplay or narrative context"

    if cluster == "list_value_or_counter_text":
        if LIST_MARKER.search(text) and not NARRATIVE_KEY.search(key):
            return "needs_new_microagent", "reusable list/counter surface, but needs a dedicated bullet/list microagent"
        return "needs_domain_context", "list/counter candidate is explanatory prose or domain text"

    if cluster == "ui_tooltip_or_markup_text":
        if TOOLTIP_KEY.search(key) and len(text) <= 220:
            return "safe_surface", "compact tooltip-like UI markup surface with no detected repair"
        if WARNING_MARKER.search(text):
            return "needs_new_microagent", "tooltip with warning marker should route to warning tooltip microagent"
        return "needs_domain_context", "markup appears inside longer prose or semantic description"

    return "needs_domain_context", "unknown autofix surface needs context before route promotion"


def build_decision(row: dict[str, Any]) -> dict[str, Any]:
    decision, note = decide(row)
    texts = row.get("texts") if isinstance(row.get("texts"), dict) else {}
    return {
        "queue_run_id": row.get("queue_run_id"),
        "ledger_item_id": row.get("ledger_item_id"),
        "segment_id": row.get("segment_id"),
        "cluster": row.get("queue_bucket") or row.get("cluster"),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "decision": decision,
        "decision_options": [
            "safe_surface",
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
    cluster_counts = Counter(f"{row.get('cluster')}|{row['decision']}" for row in decisions)
    lines = [
        "Autofix unknown cluster assisted review",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- rows: {len(decisions):,}",
        *[f"- {label}: {count:,}" for label, count in counts.most_common()],
        "",
        "Decision by cluster:",
        *[f"- {label}: {count:,}" for label, count in cluster_counts.most_common()],
        "",
        "Safety:",
        "- Assisted draft only; no output writes.",
        "- safe_surface is normalized by ingest as positive evidence, not as production authority.",
        "- needs_new_microagent should become a narrow route/specialist before lifecycle closure.",
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Draft assisted decisions for autofix_unknown cluster queues.")
    parser.add_argument("--queue-jsonl", required=True)
    args = parser.parse_args()

    settings = db.load_settings()
    queue_path = Path(args.queue_jsonl)
    decisions_path, report_path = report_paths(settings, queue_path)
    rows = load_rows(queue_path)
    decisions = [build_decision(row) for row in rows]
    write_outputs(decisions_path, report_path, decisions)

    counts = Counter(row["decision"] for row in decisions)
    print("[issue_autofix_unknown_cluster_assisted_review] Draft generated")
    print(f"[issue_autofix_unknown_cluster_assisted_review] Rows: {len(decisions):,}")
    for label, count in counts.most_common():
        print(f"[issue_autofix_unknown_cluster_assisted_review] {label}: {count:,}")
    print(f"[issue_autofix_unknown_cluster_assisted_review] Decisions: {decisions_path}")
    print(f"[issue_autofix_unknown_cluster_assisted_review] Report: {report_path}")


if __name__ == "__main__":
    main()
