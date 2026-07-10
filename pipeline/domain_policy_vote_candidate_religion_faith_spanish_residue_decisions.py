from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


PACKET_PATH = Path("reports/20260630_120204_470880_domain_policy_vote_candidate_religion_faith_spanish_residue_human_packet.jsonl")
SOURCE = "domain_policy_vote_candidate_religion_faith_spanish_residue_decisions"

DECISIONS: dict[int, tuple[str, str, str]] = {
    49894: (
        "approve_already_ok",
        "",
        "Human-approved as already OK after unicode_escape verification; protected custom token preserved.",
    ),
    162715: (
        "approve_already_ok",
        "",
        "Human-approved as already OK after unicode_escape verification; title getter tokens preserved.",
    ),
    62887: (
        "needs_more_context",
        "",
        "Long narrative with truncated packet context; keep out of protected apply.",
    ),
    161572: (
        "approve_correction",
        "Total de [taxes|El] disponíveis\\n#low O conjunto de todos os [taxes|El] que as [holdings|El] deste [title|El] fornecem, sem contar a quantia que os [vassals|El] guardam para si.#!",
        "Human-approved Spanish residue repair for tax tooltip; all CK3 tokens preserved.",
    ),
    161574: (
        "approve_correction",
        "Total de [levies|El] disponíveis\\n#low O conjunto de todas as [levies|El] que as [holdings|El] deste [title|El] fornecem, sem contar os bônus e a quantia que os [vassals|El] guardam para si.#!",
        "Human-approved Spanish residue repair for levy tooltip; all CK3 tokens preserved.",
    ),
}


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


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


def main() -> None:
    packet_rows = read_jsonl(PACKET_PATH)
    by_id = {int(row["segment_id"]): row for row in packet_rows}
    missing = sorted(set(DECISIONS) - set(by_id))
    unexpected = sorted(set(by_id) - set(DECISIONS))
    if missing:
        raise SystemExit(f"decision segment ids missing from packet: {missing}")
    if unexpected:
        raise SystemExit(f"packet segment ids missing decisions: {unexpected}")

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
    correction_count = counts.get("approve_correction", 0)
    hold_count = counts.get("needs_more_context", 0)
    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_human_decisions",
        "decision_source": SOURCE,
        "source_packet": str(PACKET_PATH),
        "decision_count": len(rows),
        "approve_correction_count": correction_count,
        "needs_more_context_count": hold_count,
        "decision_counts": [{"key": key, "count": count} for key, count in counts.most_common()],
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "gates": {
            "apply": "not_run",
            "lifecycle": "not_run",
            "segment_state": "not_run",
            "reindex": "not_run",
            "full_production": "not_run",
        },
        "next_recommendation": "run protected apply dry-run for approve_correction rows only",
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
        "domain_policy_vote_candidate religion_faith spanish residue decisions",
        "",
        f"decision_count: {len(rows)}",
        f"approve_correction_count: {correction_count}",
        f"needs_more_context_count: {hold_count}",
        "",
        "decision_counts:",
    ]
    lines.extend(f"- {count} | {key}" for key, count in counts.most_common())
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
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
