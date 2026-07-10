from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


PACKET_PATH = Path("reports/20260629_110052_850804_domain_policy_vote_candidate_low_plain_remaining_human_packet.jsonl")
DECISION_GLOBS = (
    "*_domain_policy_vote_candidate_low_plain_remaining_culture_decisions.jsonl",
    "*_domain_policy_vote_candidate_low_plain_remaining_religion_holy_decisions.jsonl",
    "*_domain_policy_vote_candidate_low_plain_remaining_title_block1_decisions.jsonl",
    "*_domain_policy_vote_candidate_low_plain_remaining_title_block2_decisions.jsonl",
    "*_domain_policy_vote_candidate_low_plain_remaining_building_decisions.jsonl",
)
SOURCE = "domain_policy_vote_candidate_low_plain_remaining_decisions_consolidated"


def reports_dir() -> Path:
    path = Path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def latest_file(pattern: str) -> Path:
    paths = sorted(reports_dir().glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    if not paths:
        raise SystemExit(f"missing decision file matching {pattern}")
    return paths[0]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def top_counter(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common()]


def write_txt(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "domain_policy_vote_candidate low_plain remaining decisions consolidated",
        "",
        f"source_packet: {summary['source_packet']}",
        f"packet_count: {summary['packet_count']}",
        f"decision_count: {summary['decision_count']}",
        f"missing_packet_decision_count: {summary['missing_packet_decision_count']}",
        f"duplicate_decision_count: {summary['duplicate_decision_count']}",
        "",
        "decision_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["decision_counts"])
    lines.extend(["", "surface_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["surface_counts"])
    lines.extend(["", "items:"])
    for row in rows:
        lines.extend(
            [
                "",
                f"## segment_id {row['segment_id']}",
                f"- surface_bucket: {row.get('surface_bucket')}",
                f"- human_decision: {row.get('human_decision')}",
                f"- source_key: {row.get('source_key')}",
                f"- current_output_text: {row.get('current_output_text')}",
                f"- corrected_text: {row.get('corrected_text') or ''}",
                f"- review_notes: {row.get('review_notes') or ''}",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    packet_rows = read_jsonl(PACKET_PATH)
    packet_ids = {int(row["segment_id"]) for row in packet_rows}
    decision_paths = [latest_file(pattern) for pattern in DECISION_GLOBS]
    decisions: list[dict[str, Any]] = []
    for path in decision_paths:
        for row in read_jsonl(path):
            row["consolidated_source_file"] = str(path)
            decisions.append(row)

    counts_by_id = Counter(int(row["segment_id"]) for row in decisions)
    duplicates = sorted(segment_id for segment_id, count in counts_by_id.items() if count > 1)
    missing = sorted(packet_ids - set(counts_by_id))
    extras = sorted(set(counts_by_id) - packet_ids)
    if extras:
        raise SystemExit(f"decision ids not present in packet: {extras}")

    decisions.sort(key=lambda row: int(row["packet_index"]))
    decision_counts = Counter(str(row.get("human_decision") or "") for row in decisions)
    surface_counts = Counter(str(row.get("surface_bucket") or "") for row in decisions)
    correction_count = decision_counts.get("approve_correction", 0)
    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_decision_consolidation",
        "decision_source": SOURCE,
        "source_packet": str(PACKET_PATH),
        "decision_source_files": [str(path) for path in decision_paths],
        "packet_count": len(packet_rows),
        "decision_count": len(decisions),
        "missing_packet_decision_count": len(missing),
        "missing_packet_segment_ids": missing,
        "duplicate_decision_count": len(duplicates),
        "duplicate_segment_ids": duplicates,
        "approve_correction_count": correction_count,
        "approve_already_ok_count": decision_counts.get("approve_already_ok", 0),
        "decision_counts": top_counter(decision_counts),
        "surface_counts": top_counter(surface_counts),
        "gates": {
            "apply": "not_run",
            "lifecycle": "not_run",
            "segment_state": "not_run",
            "reindex": "not_run",
            "full_production": "not_run",
        },
        "next_recommendation": "run protected apply dry-run/diff preview for approve_correction rows only; ingest already_ok and corrections as learning after validation",
        "output_files": {},
    }

    base = reports_dir() / f"{timestamp()}_{SOURCE}"
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    txt_path = base.with_suffix(".txt")
    write_jsonl(jsonl_path, decisions)
    summary["output_files"] = {
        "jsonl": str(jsonl_path),
        "summary_json": str(summary_path),
        "txt": str(txt_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary, decisions)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
