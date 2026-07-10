from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_medium_dynamic_light_human_packet"
DEFAULT_DIAGNOSTIC_PATH = Path("reports/20260629_121836_585965_domain_policy_vote_candidate_medium_dynamic_light_diagnostic.jsonl")
DEFAULT_TARGET_SUBLANE = "religion_faith_doctrine"
TARGET_CLASS = "token_simples_preservavel"
PREVIOUS_DECISION_GLOBS = (
    "*_domain_policy_vote_candidate_medium_dynamic_light_decisions_consolidated.jsonl",
    "*_domain_policy_vote_candidate_medium_dynamic_light_batch*_decisions.jsonl",
)


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def previous_hold_segment_ids() -> set[int]:
    segment_ids: set[int] = set()
    for pattern in PREVIOUS_DECISION_GLOBS:
        for path in reports_dir().glob(pattern):
            for row in read_jsonl(path):
                decision = str(row.get("human_decision") or "")
                if decision in {"needs_more_context", "hold_structural_or_domain_risk", "reject"}:
                    segment_ids.add(int(row["segment_id"]))
    return segment_ids


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def top_counter(counter: Counter[str], limit: int = 50) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def packet_rows(rows: list[dict[str, Any]], target_sublane: str, excluded_segment_ids: set[int]) -> list[dict[str, Any]]:
    selected = [
        row
        for row in rows
        if row.get("operational_class") == TARGET_CLASS
        and (target_sublane == "all" or row.get("surface_bucket") == target_sublane)
        and int(row["segment_id"]) not in excluded_segment_ids
        and "SelectLocalization" not in str(row.get("output_token") or "")
    ]
    selected.sort(key=lambda row: (str(row.get("surface_bucket") or ""), str(row.get("output_token") or ""), int(row["segment_id"])))
    packet = []
    for index, row in enumerate(selected, start=1):
        packet.append(
            {
                **row,
                "packet_index": index,
                "queue_source": SOURCE,
                "human_decision": "",
                "corrected_text": "",
                "review_notes": "",
                "allowed_decisions": [
                    "approve_already_ok",
                    "approve_correction",
                    "reject",
                    "needs_more_context",
                    "hold_structural_or_domain_risk",
                ],
            }
        )
    return packet


def write_txt(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "domain_policy_vote_candidate medium_dynamic_light human packet",
        "",
        f"source_diagnostic: {summary['source_diagnostic']}",
        f"packet_count: {summary['packet_count']}",
        f"target_sublane: {summary['target_sublane']}",
        f"target_class: {summary['target_class']}",
        "",
        "token_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["token_counts"])
    lines.extend(["", "items:"])
    for row in rows:
        lines.extend(
            [
                "",
                f"## {row['packet_index']}. segment_id {row['segment_id']}",
                f"- output_token: {row.get('output_token')}",
                f"- source_key: {row.get('source_key')}",
                f"- relative_path: {row.get('relative_path')}",
                f"- english_text: {row.get('english_text')}",
                f"- spanish_text: {row.get('spanish_text')}",
                f"- current_output_text: {row.get('current_output_text')}",
                "- human_decision:",
                "- corrected_text:",
                "- review_notes:",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--diagnostic-path", type=Path, default=DEFAULT_DIAGNOSTIC_PATH)
    parser.add_argument("--target-sublane", default=DEFAULT_TARGET_SUBLANE)
    parser.add_argument("--segment-state-run-id", type=int, default=495)
    args = parser.parse_args()

    excluded_segment_ids = previous_hold_segment_ids()
    rows = packet_rows(read_jsonl(args.diagnostic_path), args.target_sublane, excluded_segment_ids)
    if not rows:
        raise SystemExit("packet_count=0")
    token_counts = Counter(str(row.get("output_token") or "") for row in rows)
    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_human_packet",
        "source": SOURCE,
        "source_diagnostic": str(args.diagnostic_path),
        "segment_state_run_id": int(args.segment_state_run_id),
        "lane": "domain_policy_vote_candidate",
        "risk_bucket": "medium_dynamic_light",
        "target_sublane": args.target_sublane,
        "target_class": TARGET_CLASS,
        "packet_count": len(rows),
        "excluded_previous_hold_count": len(excluded_segment_ids),
        "excluded_previous_hold_segment_ids": sorted(excluded_segment_ids),
        "excluded_dynamic_token_rule": "output_token contains SelectLocalization",
        "token_counts": top_counter(token_counts),
        "gates": {
            "candidate_generation": "not_run",
            "apply": "not_run",
            "lifecycle": "not_run",
            "segment_state": "not_run",
            "reindex": "not_run",
            "full_production": "not_run",
        },
        "output_files": {},
    }
    base = reports_dir() / f"{stamp()}_{SOURCE}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, rows)
    summary["output_files"] = {
        "txt": str(txt_path),
        "jsonl": str(jsonl_path),
        "summary_json": str(summary_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary, rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
