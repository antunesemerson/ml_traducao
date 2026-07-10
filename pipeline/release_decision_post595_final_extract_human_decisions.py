from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_decision_post595_final_extract_human_decisions_v1"
DEFAULT_MARKDOWN = Path("reports/20260704_113645_109267_release_decision_post595_final_human_packet.md")
DEFAULT_PACKET_JSONL = Path("reports/20260704_113645_109267_release_decision_post595_final_human_packet.jsonl")
VALID_DECISIONS = {"corrected_text", "approve_already_ok", "needs_more_context", "parser_later", "reject"}
TOKEN_RE = re.compile(r"\[[^\]]+\]|\$[^$\s]+\$|@[A-Za-z0-9_]+!|#[A-Za-z0-9_]+|#!|Glossary\([^)]+\)")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract final post595 human decisions.")
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
    return [
        {"segment_id": match.group(1), "body": markdown[match.end() : matches[index + 1].start() if index + 1 < len(matches) else len(markdown)]}
        for index, match in enumerate(matches)
    ]


def extract_field(body: str, field: str) -> str:
    pattern = re.compile(rf"^{re.escape(field)}:\s*(.*?)\s*(?=^(?:Human decision|Corrected text|Notes):|\Z)", re.MULTILINE | re.DOTALL)
    match = pattern.search(body)
    return match.group(1).strip() if match else ""


def canonical(text: str) -> str:
    return "\n".join(line.rstrip() for line in (text or "").strip().splitlines())


def tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(text or "")


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


def db_output(conn, segment_id: int) -> str:
    row = conn.execute("SELECT portuguese_text FROM output_segments WHERE segment_id=?", (segment_id,)).fetchone()
    return str(row["portuguese_text"] or "") if row else ""


def classify(record: dict[str, Any], conn) -> tuple[str, list[str]]:
    decision = record["human_decision"]
    corrected = record["corrected_text"]
    output = str(record.get("output_text") or "")
    current_output = db_output(conn, int(record["segment_id"]))
    reasons: list[str] = []
    if not decision:
        return "hold_human_review_pending", ["missing_human_decision"]
    if decision not in VALID_DECISIONS:
        return "blocked_invalid_decision", ["invalid_human_decision"]
    if canonical(current_output) != canonical(output):
        reasons.append("source_output_changed_during_preview")
    if decision == "corrected_text":
        if not corrected:
            reasons.append("missing_corrected_text")
        if tokens(output) != tokens(corrected):
            reasons.append("token_integrity_mismatch")
        if structure_signature(output) != structure_signature(corrected):
            reasons.append("structure_integrity_mismatch")
        if canonical(output) == canonical(corrected):
            reasons.append("canonical_l10n_no_change")
        return ("ready_for_diff_preview" if not reasons else "blocked_corrected_text", reasons)
    if decision == "approve_already_ok":
        if canonical(output) != canonical(current_output):
            reasons.append("output_not_current")
        return ("ready_approve_already_ok" if not reasons else "blocked_approve_already_ok", reasons)
    return f"hold_{decision}", [decision]


def main() -> None:
    args = parse_args()
    markdown = db.project_path(args.markdown).read_text(encoding="utf-8-sig")
    packet = read_packet(args.packet_jsonl)
    conn = db.connect(db.load_settings())
    records: list[dict[str, Any]] = []
    for block in extract_blocks(markdown):
        segment_id = int(block["segment_id"])
        base = dict(packet.get(segment_id, {}))
        record = {
            **base,
            "source": SOURCE,
            "segment_id": segment_id,
            "human_decision": extract_field(block["body"], "Human decision"),
            "corrected_text": extract_field(block["body"], "Corrected text"),
            "notes": extract_field(block["body"], "Notes"),
        }
        status, reasons = classify(record, conn)
        record["validation_status"] = status
        record["block_reasons"] = reasons
        records.append(record)
    status_counts = Counter(row["validation_status"] for row in records)
    decision_counts = Counter(row["human_decision"] or "missing" for row in records)
    reason_counts = Counter(reason for row in records for reason in row["block_reasons"])
    ready_apply_ids = [row["segment_id"] for row in records if row["validation_status"] == "ready_for_diff_preview"]
    ready_approve_ids = [row["segment_id"] for row in records if row["validation_status"] == "ready_approve_already_ok"]
    blocked_ids = [row["segment_id"] for row in records if row["validation_status"].startswith("blocked")]
    hold_ids = [row["segment_id"] for row in records if row["validation_status"].startswith("hold")]
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_extract_and_preview_gate",
        "record_count": len(records),
        "corrected_text_count": decision_counts.get("corrected_text", 0),
        "approve_already_ok_count": decision_counts.get("approve_already_ok", 0),
        "needs_more_context_count": decision_counts.get("needs_more_context", 0),
        "ready_count": len(ready_apply_ids) + len(ready_approve_ids),
        "blocked_count": len(blocked_ids),
        "hold_count": len(hold_ids),
        "decision_counts": dict(decision_counts.most_common()),
        "status_counts": dict(status_counts.most_common()),
        "block_reason_counts": dict(reason_counts.most_common()),
        "ready_apply_ids": ready_apply_ids,
        "ready_approve_already_ok_ids": ready_approve_ids,
        "blocked_ids": blocked_ids,
        "hold_ids": hold_ids,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "materializer_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
    }
    base = reports_dir() / f"{stamp()}_release_decision_post595_final_human_decision_extract"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    md_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"markdown={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"corrected_text_count={summary['corrected_text_count']}")
    print(f"approve_already_ok_count={summary['approve_already_ok_count']}")
    print(f"needs_more_context_count={summary['needs_more_context_count']}")
    print(f"ready_count={summary['ready_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"block_reason_counts={json.dumps(summary['block_reason_counts'], ensure_ascii=False)}")
    print(f"ready_apply_ids={ready_apply_ids}")
    print(f"ready_approve_already_ok_ids={ready_approve_ids}")
    print(f"hold_ids={hold_ids}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("ingest_count=0")
    print("issue_closure_count=0")
    print("lifecycle_count=0")
    print("materializer_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")
    print("source_changed=false")
    print("output_changed=false")


if __name__ == "__main__":
    main()
