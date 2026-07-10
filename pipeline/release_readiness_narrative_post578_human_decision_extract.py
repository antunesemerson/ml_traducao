from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens
from apply_segment_state_updates import canonical_localization_text


SOURCE = "release_readiness_narrative_post578_human_decision_extract_v1"
PACKET_MD = Path("reports/20260703_144255_182813_release_readiness_narrative_post578_correction_human_packet.md")
PACKET_JSONL = Path("reports/20260703_144255_182813_release_readiness_narrative_post578_correction_human_packet.jsonl")

VALID_DECISIONS = {
    "corrected_text",
    "approve_already_ok",
    "needs_more_context",
    "parser_later",
    "reject",
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract human decisions from the editable post-578 narrative Markdown packet.")
    parser.add_argument("--packet-md", type=Path, default=PACKET_MD)
    parser.add_argument("--packet-jsonl", type=Path, default=PACKET_JSONL)
    return parser.parse_args()


def read_jsonl(path: Path) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                rows[int(row["segment_id"])] = row
    return rows


def section_blocks(markdown: str) -> list[tuple[int, str]]:
    matches = list(re.finditer(r"^##\s+\d+\.\s+Segment\s+(\d+)\s*$", markdown, flags=re.MULTILINE))
    blocks: list[tuple[int, str]] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        blocks.append((int(match.group(1)), markdown[start:end]))
    return blocks


def extract_fenced_after(label: str, block: str) -> str:
    pattern = re.escape(label) + r".*?```text\s*(.*?)\s*```"
    match = re.search(pattern, block, flags=re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else ""


def extract_decision(block: str) -> str:
    # Accept either "Human decision: corrected_text" or a later edited line containing one valid decision.
    line_match = re.search(r"Human decision:\s*(.+)", block, flags=re.IGNORECASE)
    if line_match:
        line = line_match.group(1).strip()
        if "|" in line:
            return ""
        match = re.match(r"`?([a-z_]+)`?", line, flags=re.IGNORECASE)
        if match and match.group(1) in VALID_DECISIONS:
            return match.group(1)
    for decision in VALID_DECISIONS:
        if re.search(rf"Human decision:\s*.*\b{re.escape(decision)}\b", block):
            # The unedited template contains every option, so only accept this fallback when a corrected block or notes exist.
            corrected = extract_fenced_after("Corrected text", block)
            notes = extract_fenced_after("Human notes", block)
            if corrected or notes:
                return decision
    return ""


def token_integrity_ok(old: str, new: str) -> bool:
    if not new:
        return False
    return Counter(protected_tokens(old)) == Counter(protected_tokens(new))


def structure_integrity_ok(old: str, new: str) -> bool:
    if not new or "\n" in new or "\r" in new:
        return False
    for marker in ("#T", "#X", "#P", "#N", "#EMP", "#emp", "#weak", "#WEAK", "#!", "#F", "@warning_icon!"):
        if (marker in old) != (marker in new):
            return False
    for char in ("[", "]", "$"):
        if old.count(char) != new.count(char):
            return False
    return True


def classify(record: dict[str, Any], decision: str, corrected_text: str) -> tuple[str, list[str]]:
    reasons: list[str] = []
    current = str(record.get("current_output_text") or "")
    if not decision:
        return "missing_human_decision", ["human_decision_not_filled"]
    if decision == "corrected_text":
        if not corrected_text:
            return "blocked_missing_corrected_text", ["corrected_text_required_but_empty"]
        token_ok = token_integrity_ok(current, corrected_text)
        structure_ok = structure_integrity_ok(current, corrected_text)
        canonical_change = canonical_localization_text(current) != canonical_localization_text(corrected_text)
        if not token_ok:
            reasons.append("token_integrity_failed")
        if not structure_ok:
            reasons.append("structure_integrity_failed")
        if not canonical_change:
            reasons.append("canonical_l10n_no_change")
        return ("ready_for_diff_preview" if not reasons else "blocked_corrected_text"), reasons
    if decision == "approve_already_ok":
        if canonical_localization_text(current) != canonical_localization_text(str(record.get("confirmed_text") or current)):
            return "blocked_approve_already_ok", ["output_confirmed_divergence"]
        return "ready_for_approve_already_ok_validation", []
    return decision, []


def build_records(args: argparse.Namespace) -> list[dict[str, Any]]:
    md = db.project_path(args.packet_md).read_text(encoding="utf-8")
    source_rows = read_jsonl(args.packet_jsonl)
    records: list[dict[str, Any]] = []
    for segment_id, block in section_blocks(md):
        source = source_rows.get(segment_id, {})
        decision = extract_decision(block)
        corrected = extract_fenced_after("Corrected text", block)
        notes = extract_fenced_after("Human notes", block)
        status, reasons = classify(source, decision, corrected)
        current = str(source.get("current_output_text") or "")
        records.append(
            {
                "source": SOURCE,
                "record_type": "human_decision_extract_item",
                "segment_id": segment_id,
                "relative_path": source.get("relative_path"),
                "source_key": source.get("source_key"),
                "human_decision": decision,
                "corrected_text": corrected,
                "human_notes": notes,
                "status": status,
                "block_reasons": reasons,
                "token_integrity_ok": token_integrity_ok(current, corrected) if corrected else None,
                "structure_integrity_ok": structure_integrity_ok(current, corrected) if corrected else None,
                "canonical_l10n_changes": (
                    canonical_localization_text(current) != canonical_localization_text(corrected) if corrected else None
                ),
                "open_issue_count": int(source.get("open_issue_count") or 0),
                "high_issue_count": int(source.get("high_issue_count") or 0),
                "issue_rows_count": len(source.get("open_issues") or []),
                "current_output_text": current,
                "confirmed_text": source.get("confirmed_text"),
                "candidate_generation_count": 0,
                "apply_count": 0,
                "learning_ingest_count": 0,
                "issue_closure_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )
    return records


def write_reports(records: list[dict[str, Any]], args: argparse.Namespace) -> tuple[Path, Path, Path]:
    status_counts = Counter(record["status"] for record in records)
    decision_counts = Counter(record["human_decision"] or "missing" for record in records)
    summary: dict[str, Any] = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_human_decision_extract",
        "packet_md": str(args.packet_md),
        "packet_jsonl": str(args.packet_jsonl),
        "record_count": len(records),
        "decision_counts": dict(decision_counts.most_common()),
        "status_counts": dict(status_counts.most_common()),
        "corrected_text_filled_count": sum(1 for record in records if record.get("corrected_text")),
        "ready_for_diff_preview_count": status_counts.get("ready_for_diff_preview", 0),
        "ready_for_approve_already_ok_validation_count": status_counts.get("ready_for_approve_already_ok_validation", 0),
        "blocked_count": sum(1 for record in records if str(record["status"]).startswith("blocked") or record["status"] == "missing_human_decision"),
        "ids_by_status": {
            status: [int(record["segment_id"]) for record in records if record["status"] == status]
            for status in sorted(status_counts)
        },
        "candidate_generation_count": 0,
        "apply_count": 0,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "single_operational_recommendation": "Fill human_decision and corrected_text before any diff preview/apply cycle.",
    }
    base = reports_dir() / f"{stamp()}_release_readiness_narrative_post578_human_decision_extract"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "# Human decision extract",
        "",
        f"- record_count: {summary['record_count']}",
        f"- decision_counts: `{json.dumps(summary['decision_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- status_counts: `{json.dumps(summary['status_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- corrected_text_filled_count: {summary['corrected_text_filled_count']}",
        f"- ready_for_diff_preview_count: {summary['ready_for_diff_preview_count']}",
        f"- ready_for_approve_already_ok_validation_count: {summary['ready_for_approve_already_ok_validation_count']}",
        f"- blocked_count: {summary['blocked_count']}",
        "",
    ]
    for record in records:
        lines.append(f"- {record['segment_id']}: `{record['status']}` decision=`{record['human_decision'] or 'missing'}`")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records = build_records(args)
    md_path, jsonl_path, summary_path = write_reports(records, args)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    print(f"markdown={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"decision_counts={json.dumps(summary['decision_counts'], ensure_ascii=False, sort_keys=True)}")
    print(f"status_counts={json.dumps(summary['status_counts'], ensure_ascii=False, sort_keys=True)}")
    print(f"corrected_text_filled_count={summary['corrected_text_filled_count']}")
    print(f"ready_for_diff_preview_count={summary['ready_for_diff_preview_count']}")
    print(f"ready_for_approve_already_ok_validation_count={summary['ready_for_approve_already_ok_validation_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("learning_ingest_count=0")
    print("issue_closure_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
