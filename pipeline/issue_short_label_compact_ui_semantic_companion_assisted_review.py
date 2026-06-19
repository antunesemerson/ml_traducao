from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_short_label_compact_ui_semantic_companion_assisted_review_v1"


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


def classify(row: dict[str, Any]) -> tuple[str, str]:
    suggested = str(row.get("suggested_decision") or "")
    family = str(row.get("issue_family") or "")
    text = str(row.get("confirmed_text") or row.get("evidence_text") or "")
    if suggested != "composition_ready":
        return "needs_domain_context", "suggested_decision_not_composition_ready"
    if not text.strip():
        return "needs_domain_context", "empty_text"
    if family not in {
        "religion_semantic_microagent",
        "title_policy_microagent",
        "dynamic_ck3_expression_microagent",
        "culture_semantic_microagent",
    }:
        return "needs_new_microagent", "unexpected_companion_family"
    if any(marker in text.casefold() for marker in ("no le ", " si ", " ya ", "probabilidad de éxito", " of ")):
        return "needs_repair", "visible_residual_or_english_surface"
    return "composition_ready", f"{family}:companion_issue_clean"


def build_decision(row: dict[str, Any]) -> dict[str, Any]:
    decision, note = classify(row)
    return {
        "queue_run_id": row.get("queue_run_id"),
        "ledger_item_id": row.get("ledger_item_id"),
        "segment_id": row.get("segment_id"),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "issue_family": row.get("issue_family"),
        "issue_kind": row.get("issue_kind"),
        "decision": decision,
        "decision_options": [
            "composition_ready",
            "needs_domain_context",
            "needs_repair",
            "needs_new_microagent",
        ],
        "corrected_text": "",
        "notes": note,
        "evidence_text": row.get("evidence_text"),
        "english_text": row.get("english_text"),
        "spanish_text": row.get("spanish_text"),
        "confirmed_text": row.get("confirmed_text"),
    }


def write_outputs(decisions_path: Path, report_path: Path, decisions: list[dict[str, Any]], queue_path: Path) -> None:
    with decisions_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in decisions:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    counts = Counter(row["decision"] for row in decisions)
    family_counts = Counter(f"{row.get('issue_family')}|{row['decision']}" for row in decisions)
    reason_counts = Counter(row["notes"] for row in decisions)
    lines = [
        "Short-label compact UI semantic companion assisted review",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Source queue: {queue_path}",
        "",
        "Summary:",
        f"- rows: {len(decisions):,}",
        *[f"- {label}: {count:,}" for label, count in counts.most_common()],
        "",
        "By family:",
        *[f"- {label}: {count:,}" for label, count in family_counts.most_common()],
        "",
        "Reasons:",
        *[f"- {label}: {count:,}" for label, count in reason_counts.most_common()],
        "",
        "Safety:",
        "- Assisted draft only; no output writes.",
        "- composition_ready means the companion issue can join the short-label checkpoint in a future bridge.",
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, queue_jsonl: str, reviewer: str = "codex_short_label_companion_assisted") -> dict[str, Any]:
    settings = db.load_settings()
    queue_path = db.project_path(queue_jsonl)
    if not queue_path.exists():
        raise FileNotFoundError(queue_path)
    rows = load_jsonl(queue_path)
    decisions = [build_decision(row) for row in rows]
    decisions_path, report_path = report_paths(settings, queue_path, reviewer)
    write_outputs(decisions_path, report_path, decisions, queue_path)
    counts = Counter(row["decision"] for row in decisions)
    print("[issue_short_label_compact_ui_semantic_companion_assisted_review] Draft generated")
    print(f"[issue_short_label_compact_ui_semantic_companion_assisted_review] Rows: {len(decisions):,}")
    for label, count in counts.most_common():
        print(f"[issue_short_label_compact_ui_semantic_companion_assisted_review] {label}: {count:,}")
    print(f"[issue_short_label_compact_ui_semantic_companion_assisted_review] Decisions: {decisions_path}")
    print(f"[issue_short_label_compact_ui_semantic_companion_assisted_review] Report: {report_path}")
    return {
        "rows": len(decisions),
        "counts": dict(counts),
        "decisions_path": str(decisions_path),
        "report_path": str(report_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Draft assisted decisions for compact UI semantic companion queue.")
    parser.add_argument("--queue-jsonl", required=True)
    parser.add_argument("--reviewer", default="codex_short_label_companion_assisted")
    args = parser.parse_args()
    main(queue_jsonl=args.queue_jsonl, reviewer=args.reviewer)
