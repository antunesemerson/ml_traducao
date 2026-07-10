from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import canonical_localization_text


SOURCE = "release_blocker_non_dynamic_high_issue_approve_ok_readiness_v1"
DEFAULT_INPUT = Path("reports/20260703_204147_294822_release_blocker_non_dynamic_high_issue_triage_readonly.jsonl")
EXPECTED_COUNT = 12
ALLOWED_CLASSES = {"high_issue_false_positive_output_ok", "high_issue_resolved_by_existing_output"}
TOKEN_RE = re.compile(r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z_]+|#!")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only readiness for high-issue non-dynamic approve-already-ok rows.")
    parser.add_argument("--input-jsonl", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--expected-count", type=int, default=EXPECTED_COUNT)
    return parser.parse_args()


def read_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("suggested_human_decision") != "approve_already_ok":
                continue
            row["_line_number"] = line_number
            rows.append(row)
    return rows


def fetch_context(conn: sqlite3.Connection, segment_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.spanish_text,
            s.english_text,
            output.portuguese_text AS output_text
        FROM source_segments s
        LEFT JOIN output_segments output ON output.segment_id = s.id
        WHERE s.id = ?
        """,
        (segment_id,),
    ).fetchone()
    return dict(row) if row else None


def token_signature(text: str) -> list[str]:
    return TOKEN_RE.findall(text or "")


def validate(row: dict[str, Any], context: dict[str, Any] | None) -> dict[str, Any]:
    reasons: list[str] = []
    segment_id = int(row["segment_id"])
    approved = str(row.get("output_text") or row.get("confirmed_text") or "")
    current_output = str(context.get("output_text") or "") if context else ""
    confirmed = str(row.get("confirmed_text") or approved)
    if context is None:
        reasons.append("missing_segment_or_output")
    if row.get("triage_class") not in ALLOWED_CLASSES:
        reasons.append("triage_class_not_approve_ok_safe")
    if row.get("token_surface") not in {"plain_text", "light_token"}:
        reasons.append("unsupported_token_surface")
    if canonical_localization_text(current_output) != canonical_localization_text(approved):
        reasons.append("current_output_not_approved_text")
    if canonical_localization_text(current_output) != canonical_localization_text(confirmed):
        reasons.append("current_output_not_confirmed_text")
    if int(row.get("needs_output_apply") or 0) != 0:
        reasons.append("needs_output_apply")
    if int(row.get("confirmed_matches_output") or 0) != 1:
        reasons.append("confirmed_matches_output_not_1")
    token_ok = token_signature(current_output) == token_signature(approved) == token_signature(confirmed)
    if not token_ok:
        reasons.append("token_integrity_mismatch")
    structure_ok = current_output.count("\n") == approved.count("\n") == confirmed.count("\n")
    if not structure_ok:
        reasons.append("structure_integrity_mismatch")
    canonical_ok = canonical_localization_text(current_output) == canonical_localization_text(approved)
    return {
        "source": SOURCE,
        "record_type": "approve_ok_readiness_item",
        "segment_id": segment_id,
        "relative_path": row.get("relative_path") or (context or {}).get("relative_path"),
        "source_key": row.get("source_key") or (context or {}).get("source_key"),
        "status": "ready" if not reasons else "blocked",
        "block_reasons": reasons,
        "triage_class": row.get("triage_class"),
        "suggested_human_decision": row.get("suggested_human_decision"),
        "token_surface": row.get("token_surface"),
        "token_integrity_ok": token_ok,
        "structure_integrity_ok": structure_ok,
        "canonical_l10n_ok": canonical_ok,
        "high_issue_false_positive_or_resolved": row.get("triage_class") in ALLOWED_CLASSES,
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "high_issue_count": int(row.get("high_issue_count") or 0),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "issue_families": row.get("issue_families") or "",
        "issue_kinds": row.get("issue_kinds") or "",
        "source_text": row.get("source_text") or (context or {}).get("spanish_text"),
        "output_text": current_output,
        "approved_text": approved,
        "confirmed_text": confirmed,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "materializer_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
    }


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, str]:
    base = reports_dir() / f"{stamp()}_release_blocker_non_dynamic_high_issue_approve_ok_readiness"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    jsonl_path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records), encoding="utf-8")
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "# High Issue Non-Dynamic Approve-Already-Ok Readiness",
        "",
        f"- record_count: {summary['record_count']}",
        f"- ready_count: {summary['ready_count']}",
        f"- blocked_count: {summary['blocked_count']}",
        "",
        "## Items",
    ]
    for record in records:
        lines.append(f"- {record['segment_id']} | {record['status']} | {record['source_key']} | {','.join(record['block_reasons'])}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary["output_files"]


def main() -> None:
    args = parse_args()
    rows = read_rows(args.input_jsonl)
    if len(rows) != args.expected_count:
        raise SystemExit(f"expected {args.expected_count} approve_already_ok rows, got {len(rows)}")
    records: list[dict[str, Any]] = []
    with db.connect(db.load_settings()) as conn:
        for row in rows:
            records.append(validate(row, fetch_context(conn, int(row["segment_id"]))))
    status_counts = Counter(record["status"] for record in records)
    block_counts = Counter(reason for record in records for reason in record["block_reasons"])
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only",
        "input_jsonl": str(args.input_jsonl),
        "record_count": len(records),
        "ready_count": status_counts.get("ready", 0),
        "blocked_count": status_counts.get("blocked", 0),
        "ready_segment_ids": [record["segment_id"] for record in records if record["status"] == "ready"],
        "blocked_segment_ids": [record["segment_id"] for record in records if record["status"] == "blocked"],
        "status_counts": dict(status_counts.most_common()),
        "block_reason_counts": dict(block_counts.most_common()),
        "token_integrity_ok_count": sum(1 for record in records if record["token_integrity_ok"]),
        "structure_integrity_ok_count": sum(1 for record in records if record["structure_integrity_ok"]),
        "canonical_l10n_ok_count": sum(1 for record in records if record["canonical_l10n_ok"]),
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
        "single_operational_recommendation": "Proceed with ingest only if ready_count=12 and blocked_count=0.",
    }
    outputs = write_reports(records, summary)
    print(f"markdown={outputs['markdown']}")
    print(f"jsonl={outputs['jsonl']}")
    print(f"summary={outputs['summary']}")
    print(f"ready_count={summary['ready_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"ready_segment_ids={json.dumps(summary['ready_segment_ids'], ensure_ascii=False)}")
    print(f"block_reason_counts={json.dumps(summary['block_reason_counts'], ensure_ascii=False)}")
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
