from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "short_label_plain_human_review_packet_v1"
TARGET_DECISION = "human_sample_or_policy_closure_candidate"

PRONOUN_CUSTOM_RE = re.compile(r"custom_localization/es_custom_loc", re.IGNORECASE)
VERY_SHORT_GRAMMAR_RE = re.compile(r"^(?:le|te|es|se|lo|la|los|las|um|uma|o|a|os|as)$", re.IGNORECASE)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def latest_diagnostic_jsonl() -> Path:
    matches = sorted(
        reports_dir().glob("*_short_label_style_run406_sublane_diagnostic.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise SystemExit("missing short_label_style_run406_sublane_diagnostic jsonl")
    return matches[0]


def read_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("decision") == TARGET_DECISION:
                rows.append(row)
    return rows


def classify(row: dict[str, Any]) -> tuple[str, str]:
    path = str(row.get("relative_path") or "")
    text = str(row.get("current_output_text") or "").strip()
    source_key = str(row.get("source_key") or "")
    if PRONOUN_CUSTOM_RE.search(path) or source_key.startswith("CustomLoc_ES_") or VERY_SHORT_GRAMMAR_RE.match(text):
        return "hold_custom_localization_fragment", "short grammatical/custom localization fragment needs architecture or grammar policy"
    if str(row.get("shape_bucket")) == "plain_short_label" and len(text) <= 30:
        return "human_review_plain_label", "plain short label; human can mark ok/correction"
    return "human_review_plain_phrase", "plain short phrase; human can mark ok/correction"


def build_summary(input_path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    packet = []
    for row in rows:
        decision, rationale = classify(row)
        packet.append(
            {
                "segment_id": row.get("segment_id"),
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "source_line_number": row.get("source_line_number"),
                "packet_decision": decision,
                "rationale": rationale,
                "current_output_text": row.get("current_output_text"),
                "english_text": row.get("english_text"),
                "spanish_text": row.get("spanish_text"),
                "shape_bucket": row.get("shape_bucket"),
                "risk_bucket": row.get("risk_bucket"),
                "token_count": row.get("token_count"),
                "requires_apply_later": False,
                "requires_learning_if_approved": decision.startswith("human_review"),
            }
        )
    decision_counts = Counter(row["packet_decision"] for row in packet)
    file_counts = Counter(str(row["relative_path"]) for row in packet)
    human_review_count = sum(1 for row in packet if row["packet_decision"].startswith("human_review"))
    hold_count = sum(1 for row in packet if row["packet_decision"].startswith("hold"))
    return {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_jsonl": str(input_path),
        "target_decision": TARGET_DECISION,
        "packet_count": len(packet),
        "human_review_count": human_review_count,
        "hold_count": hold_count,
        "decision_counts": [{"key": key, "count": value} for key, value in decision_counts.most_common()],
        "file_counts": [{"key": key, "count": value} for key, value in file_counts.most_common()],
        "packet_rows": packet,
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "next_action": "human_review_plain_short_labels_only",
    }


def write_outputs(summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_short_label_plain_human_review_packet"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in summary["packet_rows"]:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "short label plain human review packet",
        f"source={SOURCE}",
        f"input_jsonl={summary['input_jsonl']}",
        f"packet_count={summary['packet_count']}",
        f"human_review_count={summary['human_review_count']}",
        f"hold_count={summary['hold_count']}",
        "",
        "decision_counts:",
    ]
    for item in summary["decision_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(["", "file_counts:"])
    for item in summary["file_counts"][:12]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(
        [
            "",
            f"apply_ready_now={summary['apply_ready_now']}",
            f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
            f"next_action={summary['next_action']}",
            "",
            "packet_rows:",
        ]
    )
    for row in summary["packet_rows"]:
        lines.append(f"- {row['segment_id']} | {row['packet_decision']} | {row['current_output_text']}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    input_path = latest_diagnostic_jsonl()
    rows = read_rows(input_path)
    summary = build_summary(input_path, rows)
    txt_path, jsonl_path, summary_path = write_outputs(summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"packet_count={summary['packet_count']}")
    print(f"human_review_count={summary['human_review_count']}")
    print(f"hold_count={summary['hold_count']}")
    print(f"next_action={summary['next_action']}")


if __name__ == "__main__":
    main()
