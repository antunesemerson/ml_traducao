from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_dynamic_custom_gender_delegate_export_v1"
DEFAULT_DECISION = "needs_new_microagent"
DEFAULT_NOTE_CONTAINS = "gender_dynamic_token_should_be_owned_by_gender_microagent"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"JSONL row at {path}:{line_number} is not an object.")
            payload["_source_file"] = path.name
            payload["_source_file_line"] = line_number
            rows.append(payload)
    return rows


def report_paths(settings: dict[str, Any], issue_kind: str) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_{issue_kind}_gender_dynamic_token_delegate"
    return base.with_suffix(".jsonl"), base.with_suffix(".txt")


def key_of(row: dict[str, Any]) -> int:
    return int(row.get("ledger_item_id") or 0)


def build_export_row(queue_row: dict[str, Any], decision_row: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "rule_version": RULE_VERSION,
        "owner_agent": "micro_gender_dynamic_token_delegate",
        "created_by": "learning_front_codex",
        "source_file": str(queue_row.get("_source_file") or ""),
        "source_file_line": int(queue_row.get("_source_file_line") or 0),
        "queue": {
            key: value
            for key, value in queue_row.items()
            if not key.startswith("_")
        },
        "decision": {
            key: value
            for key, value in decision_row.items()
            if not key.startswith("_")
        },
        "routing_note": "Dynamic Custom('ES_*') evidence delegated from generic dynamic queue to gender-token diagnostic.",
    }


def write_outputs(
    *,
    jsonl_path: Path,
    report_path: Path,
    rows: list[dict[str, Any]],
    queue_path: Path,
    decisions_path: Path,
    decision_filter: str,
    note_contains: str,
) -> None:
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    queue_counts = Counter(row["queue"].get("queue_bucket") for row in rows)
    key_counts = Counter(row["queue"].get("source_key") for row in rows)
    lines = [
        "Dynamic custom localization gender delegate export",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "Inputs:",
        f"- queue_jsonl: {queue_path}",
        f"- decisions_jsonl: {decisions_path}",
        f"- decision_filter: {decision_filter}",
        f"- note_contains: {note_contains}",
        "",
        "Summary:",
        f"- exported_rows: {len(rows):,}",
        "",
        "Queue buckets:",
        *[f"- {label}: {count:,}" for label, count in queue_counts.most_common()],
        "",
        "Top source keys:",
        *[f"- {label}: {count:,}" for label, count in key_counts.most_common(15)],
        "",
        "Safety:",
        "- Evidence export only; no confirmations and no output writes.",
        "- Rows preserve the original queue item plus assisted decision for downstream diagnostics.",
        "- This file is meant for issue_gender_dynamic_token_delegate_diagnostic.py.",
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export dynamic Custom('ES_*') evidence as gender delegate diagnostic input."
    )
    parser.add_argument("--queue-jsonl", required=True)
    parser.add_argument("--decisions-jsonl", required=True)
    parser.add_argument("--decision", default=DEFAULT_DECISION)
    parser.add_argument("--note-contains", default=DEFAULT_NOTE_CONTAINS)
    parser.add_argument("--issue-kind", default="custom_localization_expression")
    args = parser.parse_args()

    settings = db.load_settings()
    queue_path = Path(args.queue_jsonl)
    decisions_path = Path(args.decisions_jsonl)
    queue_rows = load_jsonl(queue_path)
    decision_rows = load_jsonl(decisions_path)

    queue_by_ledger = {key_of(row): row for row in queue_rows if key_of(row)}
    exported: list[dict[str, Any]] = []
    skipped = Counter()
    for decision in decision_rows:
        if str(decision.get("decision") or "") != args.decision:
            skipped["decision_filter"] += 1
            continue
        notes = str(decision.get("notes") or "")
        if args.note_contains and args.note_contains not in notes:
            skipped["note_filter"] += 1
            continue
        ledger_item_id = key_of(decision)
        queue_row = queue_by_ledger.get(ledger_item_id)
        if not queue_row:
            skipped["missing_queue_row"] += 1
            continue
        exported.append(build_export_row(queue_row, decision))

    jsonl_path, report_path = report_paths(settings, args.issue_kind)
    write_outputs(
        jsonl_path=jsonl_path,
        report_path=report_path,
        rows=exported,
        queue_path=queue_path,
        decisions_path=decisions_path,
        decision_filter=args.decision,
        note_contains=args.note_contains,
    )

    print("[issue_dynamic_custom_gender_delegate_export] Export generated")
    print(f"[issue_dynamic_custom_gender_delegate_export] Queue rows: {len(queue_rows):,}")
    print(f"[issue_dynamic_custom_gender_delegate_export] Decision rows: {len(decision_rows):,}")
    print(f"[issue_dynamic_custom_gender_delegate_export] Exported rows: {len(exported):,}")
    for label, count in skipped.most_common():
        print(f"[issue_dynamic_custom_gender_delegate_export] Skipped {label}: {count:,}")
    print(f"[issue_dynamic_custom_gender_delegate_export] JSONL: {jsonl_path}")
    print(f"[issue_dynamic_custom_gender_delegate_export] Report: {report_path}")


if __name__ == "__main__":
    main()
