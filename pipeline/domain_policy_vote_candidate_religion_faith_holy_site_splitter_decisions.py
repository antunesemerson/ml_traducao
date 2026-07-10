from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_faith_holy_site_splitter_decisions"
PACKET_PATH = Path("reports/20260630_125719_719476_religion_faith_doctrine_holy_site_splitter_policy_dry_run.jsonl")

APPROVED_ALREADY_OK = {
    239292: "Holy-site effect tooltip is already correct.",
    239325: "Holy-site effect tooltip is already correct; capitalization variation accepted.",
    239334: "Holy-site effect tooltip is already correct.",
    239337: "Holy-site effect tooltip is already correct.",
    239355: "Holy-site effect tooltip is already correct.",
    239745: "Holy-site effect tooltip is already correct.",
    239971: "Fervor-per-holy-site tooltip is acceptable as written.",
}

HELD_CONTEXT = {
    282943: "Long building/destination description; keep for holy_site_building_destination review.",
    282944: "Long building/destination description; keep for holy_site_building_destination review.",
    282950: "Long building/destination description; keep for holy_site_building_destination review.",
    283045: "Long building/destination description; keep for holy_site_building_destination review.",
}


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> None:
    packet_rows = read_jsonl(PACKET_PATH)
    by_id = {int(row["segment_id"]): row for row in packet_rows}
    expected_ids = set(APPROVED_ALREADY_OK) | set(HELD_CONTEXT)
    missing = sorted(expected_ids - set(by_id))
    unexpected = sorted(set(by_id) - expected_ids)
    if missing:
        raise SystemExit(f"decision segment ids missing from packet: {missing}")
    if unexpected:
        raise SystemExit(f"packet segment ids missing decisions: {unexpected}")

    rows: list[dict[str, Any]] = []
    for segment_id in sorted(expected_ids):
        packet = by_id[segment_id]
        if segment_id in APPROVED_ALREADY_OK:
            decision = "approve_already_ok"
            notes = APPROVED_ALREADY_OK[segment_id]
        else:
            decision = "hold_context"
            notes = HELD_CONTEXT[segment_id]
        rows.append(
            {
                **packet,
                "decision_source": SOURCE,
                "human_decision": decision,
                "corrected_text": "",
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
        "approve_already_ok_count": counts.get("approve_already_ok", 0),
        "hold_context_count": counts.get("hold_context", 0),
        "decision_counts": [{"key": key, "count": count} for key, count in counts.most_common()],
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "next_recommendation": "ingest approve_already_ok rows as local learning and keep hold_context rows out of lifecycle",
        "output_files": {},
    }
    base = reports_dir() / f"{stamp()}_{SOURCE}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, rows)
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "domain_policy_vote_candidate religion_faith holy-site splitter decisions",
        "",
        f"decision_count: {len(rows)}",
        f"approve_already_ok_count: {counts.get('approve_already_ok', 0)}",
        f"hold_context_count: {counts.get('hold_context', 0)}",
        "",
        "items:",
    ]
    for row in rows:
        lines.extend(
            [
                "",
                f"## {row['segment_id']} | {row['human_decision']} | {row['source_key']}",
                f"- route: {row.get('route')}",
                f"- notes: {row['review_notes']}",
            ]
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
