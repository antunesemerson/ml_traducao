from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


PACKET_PATH = Path("reports/20260629_122429_322473_domain_policy_vote_candidate_medium_dynamic_light_human_packet.jsonl")
SOURCE = "domain_policy_vote_candidate_medium_dynamic_light_block1_decisions"

DECISIONS: dict[int, tuple[str, str, str]] = {
    239175: ("approve_already_ok", "", "Current PT-BR text is acceptable."),
    240705: ("approve_correction", "de $baltic_health_god_name$", "Possessive key; Spanish confirms de + token."),
    240701: ("approve_correction", "de $baltic_high_god_name$", "Possessive key; Spanish confirms de + token."),
    241738: ("needs_more_context", "", "Token expansion may affect gender/order."),
    241739: ("needs_more_context", "", "Token expansion may affect gender/order."),
    242734: ("approve_already_ok", "", "Post-token adjective is more stable for token expansion."),
    242735: ("approve_already_ok", "", "Post-token adjective is more stable for token expansion."),
    242101: ("approve_already_ok", "", "Post-token adjective is more stable for token expansion."),
    242102: ("approve_already_ok", "", "Post-token adjective is more stable for token expansion."),
    238011: ("needs_more_context", "", "English source contains omitted $hostility_evil_tooltippable$ token; needs policy/context."),
    240842: ("approve_correction", "de $finno_ugric_health_god_name$", "Possessive key; Spanish confirms de + token."),
    242523: ("approve_already_ok", "", "Post-token adjective is more stable for token expansion."),
    242524: ("approve_already_ok", "", "Post-token adjective is more stable for token expansion."),
    238796: ("approve_already_ok", "", "Token is already possessive; avoid duplicated de."),
    238798: ("approve_already_ok", "", "Token is already possessive; avoid duplicated de."),
    238800: ("approve_already_ok", "", "Token is already possessive; avoid duplicated de."),
    238802: ("approve_already_ok", "", "Token is already possessive; avoid duplicated de."),
    238804: ("approve_already_ok", "", "Token is already possessive; avoid duplicated de."),
    238806: ("approve_already_ok", "", "Token is already possessive; avoid duplicated de."),
    238808: ("approve_already_ok", "", "Token is already possessive; avoid duplicated de."),
}


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def stamp() -> str:
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
        "domain_policy_vote_candidate medium_dynamic_light block1 decisions",
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


def top_counter(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common()]


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
