from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


PACKET_PATH = Path("reports/20260629_110052_850804_domain_policy_vote_candidate_low_plain_remaining_human_packet.jsonl")
SOURCE = "domain_policy_vote_candidate_low_plain_remaining_building_decisions"

DECISIONS: dict[int, tuple[str, str, str]] = {
    46846: (
        "approve_correction",
        "Seus cavalariços experientes equipam seu séquito com as melhores montarias de todo o reino.",
        "Stable-hands means cavalariços/tratadores, not estábulos.",
    ),
}


def reports_dir() -> Path:
    path = Path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def read_packet() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with PACKET_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_txt(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "domain_policy_vote_candidate low_plain remaining building decisions",
        "",
        f"source_packet: {summary['source_packet']}",
        f"decision_count: {summary['decision_count']}",
        "",
        "decision_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["decision_counts"])
    lines.extend(["", "items:"])
    for row in rows:
        lines.extend(
            [
                "",
                f"## segment_id {row['segment_id']}",
                f"- human_decision: {row['human_decision']}",
                f"- source_key: {row.get('source_key')}",
                f"- current_output_text: {row.get('current_output_text')}",
                f"- corrected_text: {row.get('corrected_text') or ''}",
                f"- review_notes: {row.get('review_notes') or ''}",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def top_counter(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common()]


def main() -> None:
    by_id = {int(row["segment_id"]): row for row in read_packet()}
    missing = sorted(set(DECISIONS) - set(by_id))
    if missing:
        raise SystemExit(f"decision segment ids missing from packet: {missing}")
    rows: list[dict[str, Any]] = []
    for segment_id in sorted(DECISIONS):
        decision, corrected_text, notes = DECISIONS[segment_id]
        rows.append(
            {
                **by_id[segment_id],
                "decision_source": SOURCE,
                "human_decision": decision,
                "corrected_text": corrected_text,
                "review_notes": notes,
            }
        )
    counts = Counter(row["human_decision"] for row in rows)
    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_human_decisions",
        "decision_source": SOURCE,
        "source_packet": str(PACKET_PATH),
        "decision_count": len(rows),
        "decision_counts": top_counter(counts),
        "gates": {
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
    write_jsonl(jsonl_path, rows)
    summary["output_files"] = {
        "jsonl": str(jsonl_path),
        "summary_json": str(summary_path),
        "txt": str(txt_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary, rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
