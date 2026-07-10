from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_decision_post592_top15_extract_human_decisions_v1"
DEFAULT_MARKDOWN = Path("reports/20260703_235712_724351_release_decision_post592_top15_human_packet.md")
DEFAULT_PACKET_JSONL = Path("reports/20260703_235712_724351_release_decision_post592_top15_human_packet.jsonl")
VALID_DECISIONS = {"corrected_text", "approve_already_ok", "needs_more_context", "parser_later", "reject"}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract filled human decisions from post-592 top15 Markdown packet.")
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--packet-jsonl", type=Path, default=DEFAULT_PACKET_JSONL)
    return parser.parse_args()


def read_packet(path: Path) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    with db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                rows[int(row["segment_id"])] = row
    return rows


def extract_blocks(markdown: str) -> list[dict[str, str]]:
    pattern = re.compile(r"^##\s+\d+\.\s+Segment\s+(\d+)\s*$", re.MULTILINE)
    matches = list(pattern.finditer(markdown))
    blocks: list[dict[str, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        blocks.append({"segment_id": match.group(1), "body": markdown[start:end]})
    return blocks


def extract_field(body: str, field: str) -> str:
    escaped = re.escape(field)
    pattern = re.compile(rf"^{escaped}:\s*(.*?)\s*(?=^(?:Human decision|Corrected text|Notes):|\Z)", re.MULTILINE | re.DOTALL)
    match = pattern.search(body)
    if not match:
        return ""
    return match.group(1).strip()


def token_signature(text: str) -> list[str]:
    return re.findall(r"\[[^\]]+\]|\$[^$\s]+\$|#[A-Za-z0-9_]+|#!", text or "")


def structure_signature(text: str) -> dict[str, int]:
    text = text or ""
    return {
        "line_count": text.count("\n") + 1 if text else 0,
        "open_brackets": text.count("["),
        "close_brackets": text.count("]"),
        "open_tags": len(re.findall(r"#[A-Za-z0-9_]+", text)),
        "close_tags": text.count("#!"),
        "dollar_count": text.count("$"),
    }


def normalize_l10n(text: str) -> str:
    return "\n".join(line.rstrip() for line in (text or "").strip().splitlines())


def classify(record: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    decision = record["human_decision"]
    corrected = record["corrected_text"]
    output = record.get("output_text") or ""
    current_target = record.get("current_target_or_confirmed_text") or ""
    if not decision:
        return "hold_human_review_pending", ["missing_human_decision"]
    if decision not in VALID_DECISIONS:
        return "blocked_invalid_decision", ["invalid_human_decision"]
    if decision == "corrected_text":
        if not corrected:
            reasons.append("missing_corrected_text")
        if token_signature(output) != token_signature(corrected):
            reasons.append("token_integrity_mismatch")
        if structure_signature(output) != structure_signature(corrected):
            reasons.append("structure_integrity_mismatch")
        if normalize_l10n(output) == normalize_l10n(corrected):
            reasons.append("canonical_l10n_no_change")
        if reasons:
            return "blocked_corrected_text", reasons
        return "ready_for_diff_preview", []
    if decision == "approve_already_ok":
        if normalize_l10n(output) != normalize_l10n(current_target):
            reasons.append("output_target_divergence")
        if token_signature(output) != token_signature(current_target):
            reasons.append("token_integrity_mismatch")
        if structure_signature(output) != structure_signature(current_target):
            reasons.append("structure_integrity_mismatch")
        if reasons:
            return "blocked_approve_already_ok", reasons
        return "ready_approve_already_ok", []
    return f"hold_{decision}", [decision]


def main() -> None:
    args = parse_args()
    markdown_path = db.project_path(args.markdown)
    markdown = markdown_path.read_text(encoding="utf-8-sig")
    packet = read_packet(args.packet_jsonl)
    records: list[dict[str, Any]] = []
    for block in extract_blocks(markdown):
        segment_id = int(block["segment_id"])
        base = dict(packet.get(segment_id, {}))
        decision = extract_field(block["body"], "Human decision")
        corrected = extract_field(block["body"], "Corrected text")
        notes = extract_field(block["body"], "Notes")
        record = {
            **base,
            "source": SOURCE,
            "segment_id": segment_id,
            "human_decision": decision,
            "corrected_text": corrected,
            "notes": notes,
        }
        status, reasons = classify(record)
        record["validation_status"] = status
        record["block_reasons"] = reasons
        record["token_integrity_ok"] = not any(reason == "token_integrity_mismatch" for reason in reasons)
        record["structure_integrity_ok"] = not any(reason == "structure_integrity_mismatch" for reason in reasons)
        record["canonical_l10n_ok"] = not any(reason in {"canonical_l10n_no_change", "output_target_divergence"} for reason in reasons)
        records.append(record)
    status_counts = Counter(row["validation_status"] for row in records)
    decision_counts = Counter(row["human_decision"] or "missing" for row in records)
    reason_counts = Counter(reason for row in records for reason in row["block_reasons"])
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_extract_human_decisions",
        "input_markdown": str(args.markdown),
        "input_packet_jsonl": str(args.packet_jsonl),
        "record_count": len(records),
        "decision_counts": dict(decision_counts.most_common()),
        "status_counts": dict(status_counts.most_common()),
        "block_reason_counts": dict(reason_counts.most_common()),
        "corrected_text_ready_count": status_counts.get("ready_for_diff_preview", 0),
        "approve_already_ok_ready_count": status_counts.get("ready_approve_already_ok", 0),
        "blocked_count": len(records)
        - status_counts.get("ready_for_diff_preview", 0)
        - status_counts.get("ready_approve_already_ok", 0),
        "ready_corrected_text_ids": [row["segment_id"] for row in records if row["validation_status"] == "ready_for_diff_preview"],
        "ready_approve_already_ok_ids": [row["segment_id"] for row in records if row["validation_status"] == "ready_approve_already_ok"],
        "blocked_ids": [row["segment_id"] for row in records if row["validation_status"].startswith("blocked") or row["validation_status"].startswith("hold")],
        "candidate_generation_count": 0,
        "apply_count": 0,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "materializer_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "single_operational_recommendation": (
            "Markdown has no filled decisions; keep packet in hold_human_review_pending."
            if status_counts.get("hold_human_review_pending", 0) == len(records)
            else "Review ready/blocked split before any protected apply or learning ingest."
        ),
    }
    base = reports_dir() / f"{stamp()}_release_decision_post592_top15_human_decision_extract"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    md_lines = [
        "# Post-592 Top 15 Human Decision Extract",
        "",
        f"- record_count: {summary['record_count']}",
        f"- decision_counts: {json.dumps(summary['decision_counts'], ensure_ascii=False)}",
        f"- status_counts: {json.dumps(summary['status_counts'], ensure_ascii=False)}",
        f"- block_reason_counts: {json.dumps(summary['block_reason_counts'], ensure_ascii=False)}",
        f"- corrected_text_ready_count: {summary['corrected_text_ready_count']}",
        f"- approve_already_ok_ready_count: {summary['approve_already_ok_ready_count']}",
        f"- blocked_count: {summary['blocked_count']}",
        "",
        "## Items",
    ]
    for record in records:
        md_lines.extend(
            [
                f"- {record['segment_id']}: {record['validation_status']} | decision={record['human_decision'] or 'missing'} | reasons={record['block_reasons']}",
            ]
        )
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"markdown={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"decision_counts={json.dumps(summary['decision_counts'], ensure_ascii=False)}")
    print(f"status_counts={json.dumps(summary['status_counts'], ensure_ascii=False)}")
    print(f"block_reason_counts={json.dumps(summary['block_reason_counts'], ensure_ascii=False)}")
    print(f"corrected_text_ready_count={summary['corrected_text_ready_count']}")
    print(f"approve_already_ok_ready_count={summary['approve_already_ok_ready_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("learning_ingest_count=0")
    print("issue_closure_count=0")
    print("lifecycle_count=0")
    print("materializer_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
