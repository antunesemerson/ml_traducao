from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "v3_deterministic_correction_human_packet_v1"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def latest_review() -> Path:
    reports = db.project_path(db.load_settings()["reports_dir"])
    matches = sorted(reports.glob("*_v3_improvement_shadow_review_readonly.jsonl"))
    if not matches:
        raise RuntimeError("No V3 shadow review JSONL was found.")
    return matches[-1]


def report_paths() -> dict[str, Path]:
    reports = db.project_path(db.load_settings()["reports_dir"])
    base = reports / f"{stamp()}_v3_deterministic_correction_human_packet"
    return {
        "markdown": base.with_suffix(".md"),
        "jsonl": base.with_suffix(".jsonl"),
        "summary": base.with_name(base.name + "_summary.json"),
    }


def load_rows(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    deterministic = [
        row
        for row in rows
        if row["decision"] == "corrected_text_possible"
        and row["token_signature_exact"]
        and row["spanish_token_signature_exact"]
    ]
    token_authority = [
        row
        for row in rows
        if row["decision"] == "corrected_text_possible"
        and (not row["token_signature_exact"] or not row["spanish_token_signature_exact"])
    ]
    context = [row for row in rows if row["decision"] == "needs_context"]
    if len(deterministic) != 11 or len(token_authority) != 1 or len(context) != 5:
        raise RuntimeError("Reviewed decision split changed; regenerate the packet deliberately.")
    return deterministic, token_authority, context


def write_reports(
    paths: dict[str, Path],
    review_path: Path,
    deterministic: list[dict[str, Any]],
    token_authority: list[dict[str, Any]],
    context: list[dict[str, Any]],
) -> dict[str, Any]:
    packet_rows: list[dict[str, Any]] = []
    for index, source in enumerate(deterministic, start=1):
        row = dict(source)
        row.update(
            {
                "packet_index": index,
                "human_decision": None,
                "corrected_text": None,
                "assisted_suggestion": source["suggested_text"],
                "packet_status": "hold_human_review_pending",
                "ready_for_diff_preview": False,
                "candidate_generation_allowed": False,
                "apply_allowed": False,
            }
        )
        packet_rows.append(row)
    summary = {
        "schema_version": 1,
        "rule_version": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "read_only": True,
        "source_review": str(review_path),
        "record_count": len(packet_rows),
        "human_decision_filled_count": 0,
        "corrected_text_filled_count": 0,
        "ready_for_diff_preview_count": 0,
        "token_authority_hold_count": len(token_authority),
        "token_authority_hold_segment_ids": [int(row["segment_id"]) for row in token_authority],
        "context_hold_count": len(context),
        "context_hold_segment_ids": [int(row["segment_id"]) for row in context],
        "candidate_generation": 0,
        "apply": 0,
        "source_changed": False,
        "output_changed": False,
    }
    paths["summary"].write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with paths["jsonl"].open("w", encoding="utf-8", newline="\n") as handle:
        for row in packet_rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    lines = [
        "# V3 deterministic correction human packet",
        "",
        f"- Review items: `{len(packet_rows)}`",
        f"- Token-authority hold: `{len(token_authority)}` (`{token_authority[0]['segment_id']}`)",
        f"- Context holds: `{len(context)}`",
        "- Apply: `0`",
        "",
        "Fill one `Human decision` per item. For `corrected_text`, copy or edit the assisted suggestion into `Corrected text`.",
        "",
    ]
    for row in packet_rows:
        lines.extend(
            [
                f"## {row['packet_index']}. Segment {row['segment_id']}",
                "",
                f"- Path/key: `{row['relative_path']} :: {row['source_key']}`",
                f"- Pattern: `{row['pattern']}`",
                f"- Reason: {row['reason']}",
                f"- English: {row.get('english_text') or ''}",
                f"- Spanish: {row.get('spanish_text') or ''}",
                f"- Current PT-BR: {row.get('output_text') or ''}",
                f"- Assisted suggestion: {row.get('assisted_suggestion') or ''}",
                "- Token signature: `exact`",
                "",
                "Human decision:",
                "",
                "Corrected text:",
                "",
            ]
        )
    paths["markdown"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Create an editable human packet for deterministic V3 corrections.")
    parser.add_argument("--review", type=Path)
    args = parser.parse_args()
    review_path = args.review.resolve() if args.review else latest_review()
    deterministic, token_authority, context = load_rows(review_path)
    paths = report_paths()
    summary = write_reports(paths, review_path, deterministic, token_authority, context)
    print("[v3-human-packet] Read-only packet completed")
    print(f"[v3-human-packet] Items: {summary['record_count']}")
    print(f"[v3-human-packet] Markdown: {paths['markdown']}")
    print(f"[v3-human-packet] JSONL: {paths['jsonl']}")
    print(f"[v3-human-packet] Summary: {paths['summary']}")
    return summary


if __name__ == "__main__":
    main()
