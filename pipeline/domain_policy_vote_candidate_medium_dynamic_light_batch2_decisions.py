from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


PACKET_PATH = Path("reports/20260629_125807_557769_domain_policy_vote_candidate_medium_dynamic_light_human_packet.jsonl")
SOURCE = "domain_policy_vote_candidate_medium_dynamic_light_batch2_decisions"

DECISIONS: dict[int, tuple[str, str, str]] = {
    20970: ("approve_already_ok", "", "Current PT-BR text is natural and preserves the token."),
    20964: ("approve_already_ok", "", "Current PT-BR text is natural and preserves the token."),
    16889: (
        "approve_correction",
        "Cavalaria leve capaz de atacar rapidamente inimigos expostos antes de recuar velozmente, nossos $hussar$ podem ser organizados em regimentos especializados em perseguição e assalto.",
        "Fix literal phrasing and agreement while preserving the token.",
    ),
    16859: (
        "needs_more_context",
        "",
        "Hold because 'hobby horses' may require domain-specific localization choice.",
    ),
    16493: (
        "approve_correction",
        "Pode reivindicar vários dos seus [claims|lE] em uma só guerra",
        "Fix perspective: your claims, not my claims.",
    ),
    16494: (
        "approve_correction",
        "Pode reivindicar vários [claims|lE] por outro personagem em uma única guerra",
        "Improve grammar while preserving the token.",
    ),
    16509: ("approve_already_ok", "", "Current PT-BR text is acceptable."),
    16512: ("approve_already_ok", "", "Current PT-BR text is acceptable."),
    16515: ("approve_already_ok", "", "Current PT-BR text is acceptable."),
}


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def read_packet() -> list[dict[str, Any]]:
    rows = []
    with PACKET_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def top_counter(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common()]


def write_txt(path: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "domain_policy_vote_candidate medium_dynamic_light batch2 decisions",
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
                f"- output_token: {row.get('output_token')}",
                f"- source_key: {row.get('source_key')}",
                f"- current_output_text: {row.get('current_output_text')}",
                f"- corrected_text: {row.get('corrected_text') or ''}",
                f"- review_notes: {row.get('review_notes') or ''}",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    by_id = {int(row["segment_id"]): row for row in read_packet()}
    missing = sorted(set(DECISIONS) - set(by_id))
    if missing:
        raise SystemExit(f"decision segment ids missing from packet: {missing}")
    rows = []
    for segment_id in sorted(DECISIONS, key=lambda sid: int(by_id[sid]["packet_index"])):
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
    base = reports_dir() / f"{stamp()}_{SOURCE}"
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    txt_path = base.with_suffix(".txt")
    write_jsonl(jsonl_path, rows)
    summary["output_files"] = {"jsonl": str(jsonl_path), "summary_json": str(summary_path), "txt": str(txt_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary, rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
