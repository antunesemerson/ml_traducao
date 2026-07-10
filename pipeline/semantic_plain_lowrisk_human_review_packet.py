from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "semantic_plain_lowrisk_human_review_packet_v1"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def latest_sample_summary() -> Path:
    matches = sorted(
        reports_dir().glob("*_semantic_plain_lowrisk_run406_review_sample_summary.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise SystemExit("missing semantic_plain_lowrisk_run406_review_sample summary")
    return matches[0]


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def review_priority(row: dict[str, Any]) -> tuple[int, int, int]:
    order = {
        "needs_ptbr_fluency": 0,
        "semantic_error_or_spanish_residue": 1,
        "needs_more_context": 2,
    }
    classification = str(row.get("initial_classification") or "")
    text_len = len(str(row.get("current_output_text") or ""))
    return (order.get(classification, 9), text_len, int(row.get("segment_id") or 0))


def build_packet(input_path: Path, summary: dict[str, Any]) -> dict[str, Any]:
    rows = [
        row
        for row in summary.get("sample", [])
        if row.get("human_review_required")
    ]
    rows.sort(key=review_priority)
    class_counts = Counter(str(row.get("initial_classification") or "") for row in rows)
    domain_counts = Counter(str(row.get("source_domain") or "") for row in rows)
    return {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_summary": str(input_path),
        "review_count": len(rows),
        "classification_counts": [{"key": key, "count": value} for key, value in class_counts.most_common()],
        "domain_counts": [{"key": key, "count": value} for key, value in domain_counts.most_common()],
        "review_rows": rows,
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "next_action": "human_review_fluency_first_then_context_cases",
    }


def write_outputs(packet: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_semantic_plain_lowrisk_human_review_packet"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in packet["review_rows"]:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "semantic plain lowrisk human review packet",
        f"source={SOURCE}",
        f"input_summary={packet['input_summary']}",
        f"review_count={packet['review_count']}",
        "",
        "classification_counts:",
    ]
    for item in packet["classification_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(["", "domain_counts:"])
    for item in packet["domain_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(
        [
            "",
            f"apply_ready_now={packet['apply_ready_now']}",
            f"production_full_recommended_now={str(packet['production_full_recommended_now']).lower()}",
            f"next_action={packet['next_action']}",
            "",
            "review_rows:",
        ]
    )
    for row in packet["review_rows"]:
        current = " ".join(str(row.get("current_output_text") or "").split())
        if len(current) > 220:
            current = current[:217] + "..."
        lines.append(
            "- "
            f"{row.get('segment_id')} | {row.get('initial_classification')} | "
            f"{row.get('source_domain')} | {current}"
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    input_path = latest_sample_summary()
    sample_summary = read_json(input_path)
    packet = build_packet(input_path, sample_summary)
    txt_path, jsonl_path, summary_path = write_outputs(packet)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"review_count={packet['review_count']}")
    print(f"classification_counts={packet['classification_counts']}")
    print(f"domain_counts={packet['domain_counts']}")
    print(f"next_action={packet['next_action']}")


if __name__ == "__main__":
    main()
