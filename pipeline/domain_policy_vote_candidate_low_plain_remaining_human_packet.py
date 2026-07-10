from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE_DIAGNOSTIC_GLOB = "*_domain_policy_vote_candidate_low_plain_domain_remaining_diagnostic.jsonl"
SOURCE = "domain_policy_vote_candidate_low_plain_remaining_human_packet"
INCLUDED_RECOMMENDATIONS = {"small_human_packet", "abstract_pattern_diagnostic"}


def reports_dir() -> Path:
    path = Path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def latest_diagnostic_path() -> Path:
    paths = sorted(reports_dir().glob(SOURCE_DIAGNOSTIC_GLOB), key=lambda path: path.stat().st_mtime, reverse=True)
    if not paths:
        raise SystemExit(f"missing diagnostic matching {SOURCE_DIAGNOSTIC_GLOB}")
    return paths[0]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def top_counter(counter: Counter[str], limit: int = 50) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def make_packet(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = [
        row
        for row in rows
        if row.get("recommendation") in INCLUDED_RECOMMENDATIONS
        and not row.get("matching_exact_rule_ids")
        and not row.get("exclusion_reasons")
    ]
    selected.sort(key=lambda row: (str(row.get("surface_bucket") or ""), int(row["segment_id"])))
    packet: list[dict[str, Any]] = []
    for idx, row in enumerate(selected, start=1):
        packet.append(
            {
                **row,
                "packet_index": idx,
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
        "domain_policy_vote_candidate low_plain_domain remaining human packet",
        "",
        f"source_diagnostic: {summary['source_diagnostic']}",
        f"packet_count: {summary['packet_count']}",
        f"mode: {summary['mode']}",
        "",
        "decision_options:",
        "- approve_already_ok",
        "- approve_correction",
        "- reject",
        "- needs_more_context",
        "- hold_structural_or_domain_risk",
        "",
        "sublane_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["sublane_counts"])
    lines.extend(["", "recommendation_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["recommendation_counts"])
    lines.extend(["", "items:"])
    for row in rows:
        lines.extend(
            [
                "",
                f"## {row['packet_index']}. segment_id {row['segment_id']}",
                f"- surface_bucket: {row.get('surface_bucket')}",
                f"- recommendation: {row.get('recommendation')}",
                f"- recommendation_reason: {row.get('recommendation_reason')}",
                f"- abstract_pattern_tags: {', '.join(row.get('abstract_pattern_tags') or []) or 'none'}",
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
    diagnostic_path = latest_diagnostic_path()
    diagnostic_rows = read_jsonl(diagnostic_path)
    packet_rows = make_packet(diagnostic_rows)
    if not packet_rows:
        raise SystemExit("packet_count=0; no human packet generated")

    sublane_counts = Counter(str(row.get("surface_bucket") or "") for row in packet_rows)
    recommendation_counts = Counter(str(row.get("recommendation") or "") for row in packet_rows)
    tag_counts = Counter(tag for row in packet_rows for tag in row.get("abstract_pattern_tags") or [])
    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_human_packet",
        "queue_source": SOURCE,
        "source_diagnostic": str(diagnostic_path),
        "diagnostic_row_count": len(diagnostic_rows),
        "packet_count": len(packet_rows),
        "excluded_hold_context_count": sum(1 for row in diagnostic_rows if row.get("recommendation") == "hold_context"),
        "sublane_counts": top_counter(sublane_counts),
        "recommendation_counts": top_counter(recommendation_counts),
        "abstract_pattern_tag_counts": top_counter(tag_counts),
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

    base = reports_dir() / f"{timestamp()}_{SOURCE}"
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    txt_path = base.with_suffix(".txt")
    write_jsonl(jsonl_path, packet_rows)
    summary["output_files"] = {
        "jsonl": str(jsonl_path),
        "summary_json": str(summary_path),
        "txt": str(txt_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary, packet_rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
