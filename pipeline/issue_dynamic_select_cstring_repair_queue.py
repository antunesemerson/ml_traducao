from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "issue_dynamic_select_cstring_repair_queue_v1"
DEFAULT_AUDIT_GLOB = "*_issue_dynamic_select_cstring_pattern_audit.csv"
TARGET_PAYLOADS = {"spanish_pronoun_or_verb_literal", "spanish_title_literal"}


def latest_audit_csv(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    candidates = sorted(reports_dir.glob(DEFAULT_AUDIT_GLOB), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise RuntimeError("No dynamic Select_CString pattern audit CSV found.")
    return candidates[0]


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_dynamic_select_cstring_repair_queue"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
    )


def load_audit_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def priority(row: dict[str, Any]) -> float:
    score = 0.0
    if row["payload_family"] == "spanish_pronoun_or_verb_literal":
        score += 1000
    if row["payload_family"] == "spanish_title_literal":
        score += 850
    if row["condition_family"] == "local_player_branch":
        score += 300
    if row["condition_family"] == "player_branch":
        score += 220
    if row["condition_family"] == "gender_branch":
        score += 180
    if row["queue_bucket"] == "dynamic_select_cstring_short":
        score += 90
    try:
        score += int(row["segment_id"]) % 17 / 100
    except (TypeError, ValueError):
        pass
    return round(score, 4)


def select_rows(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    target_rows = [row for row in rows if row.get("payload_family") in TARGET_PAYLOADS]
    by_observation: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in target_rows:
        key = (row["queue_item_id"], row["select_index"], row["payload_family"])
        row["priority_score"] = priority(row)
        row["suggested_decision"] = "repair_spanish_select_cstring_literal"
        by_observation[key] = row
    selected = sorted(
        by_observation.values(),
        key=lambda row: (-float(row["priority_score"]), row["relative_path"], row["source_key"], int(row["select_index"] or 0)),
    )
    return selected[:limit]


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    template_path: Path,
    audit_csv: Path,
    rows: list[dict[str, Any]],
    selected: list[dict[str, Any]],
) -> None:
    fields = [
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "queue_bucket",
        "select_index",
        "condition_family",
        "payload_family",
        "condition",
        "left_literal",
        "right_literal",
        "priority_score",
        "suggested_decision",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in selected:
            writer.writerow({field: row.get(field) for field in fields})
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            payload = {field: row.get(field) for field in fields}
            payload["learning_only"] = 1
            payload["apply_allowed"] = 0
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    with template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            payload = {
                "queue_item_id": row["queue_item_id"],
                "ledger_item_id": row["ledger_item_id"],
                "segment_id": row["segment_id"],
                "select_index": row["select_index"],
                "decision": "",
                "decision_options": [
                    "needs_repair",
                    "needs_new_microagent",
                    "needs_domain_context",
                    "manual_exception",
                    "false_positive_reopen",
                ],
                "corrected_left_literal": "",
                "corrected_right_literal": "",
                "notes": "",
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    payload_counts = Counter(row["payload_family"] for row in rows)
    selected_payload_counts = Counter(row["payload_family"] for row in selected)
    condition_counts = Counter(row["condition_family"] for row in selected)
    path_counts = Counter(row["relative_path"] for row in selected)
    lines = [
        "Issue dynamic Select_CString repair queue",
        f"Rule version: {RULE_VERSION}",
        f"Source audit CSV: {audit_csv}",
        "",
        "Summary:",
        f"- Audit observations: {len(rows):,}",
        f"- Target selected: {len(selected):,}",
        "- Apply allowed: 0",
        "",
        "Audit target payloads:",
        *[f"- {key}: {value:,}" for key, value in payload_counts.most_common() if key in TARGET_PAYLOADS],
        "",
        "Selected payloads:",
        *[f"- {key}: {value:,}" for key, value in selected_payload_counts.most_common()],
        "",
        "Selected condition families:",
        *[f"- {key}: {value:,}" for key, value in condition_counts.most_common()],
        "",
        "Selected paths:",
        *[f"- {key}: {value:,}" for key, value in path_counts.most_common(20)],
        "",
        "Samples:",
    ]
    for row in selected[:40]:
        lines.append(
            f"- {row['condition_family']} / {row['payload_family']} | "
            f"{row['relative_path']}::{row['source_key']} | "
            f"{row['left_literal']!r} -> {row['right_literal']!r}"
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, audit_csv: str | None = None, limit: int = 240) -> dict[str, Any]:
    settings = db.load_settings()
    selected_audit_csv = Path(audit_csv) if audit_csv else latest_audit_csv(settings)
    rows = load_audit_rows(selected_audit_csv)
    selected = select_rows(rows, limit=limit)
    txt_path, csv_path, jsonl_path, template_path = report_paths(settings)
    write_outputs(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        template_path=template_path,
        audit_csv=selected_audit_csv,
        rows=rows,
        selected=selected,
    )
    print("[issue_dynamic_select_cstring_repair_queue] Queue generated")
    print(f"[issue_dynamic_select_cstring_repair_queue] Rule version: {RULE_VERSION}")
    print(f"[issue_dynamic_select_cstring_repair_queue] Selected: {len(selected):,}")
    print("[issue_dynamic_select_cstring_repair_queue] Apply allowed: 0")
    print(f"[issue_dynamic_select_cstring_repair_queue] Report: {txt_path}")
    print(f"[issue_dynamic_select_cstring_repair_queue] CSV: {csv_path}")
    print(f"[issue_dynamic_select_cstring_repair_queue] JSONL: {jsonl_path}")
    print(f"[issue_dynamic_select_cstring_repair_queue] Decisions template: {template_path}")
    return {
        "selected": len(selected),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(template_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a learning-only repair queue for dynamic Select_CString literals.")
    parser.add_argument("--audit-csv", default=None)
    parser.add_argument("--limit", type=int, default=240)
    args = parser.parse_args()
    main(audit_csv=args.audit_csv, limit=args.limit)
