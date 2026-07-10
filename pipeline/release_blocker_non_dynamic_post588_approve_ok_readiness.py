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


SOURCE = "release_blocker_non_dynamic_post588_approve_ok_readiness_v1"
DEFAULT_INPUT = Path("reports/20260703_215001_917388_release_readiness_post588_high_issue_summary_readonly.jsonl")
DEFAULT_LEDGER_RUN_ID = 76
EXCLUDED_IDS = {120831, 126552, 127174}
TOKEN_RE = re.compile(r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z_]+|#!")
DYNAMIC_RE = re.compile(r"Select_CString|SelectLocalization|\.Get|\.Custom|ROOT\.|SCOPE\.|CHARACTER\.")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only readiness for post-588 high_issue_non_dynamic approve-ok candidates.")
    parser.add_argument("--input-jsonl", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--ledger-run-id", type=int, default=DEFAULT_LEDGER_RUN_ID)
    return parser.parse_args()


def read_candidates(path: Path, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            segment_id = int(row.get("segment_id") or 0)
            if segment_id in EXCLUDED_IDS:
                continue
            if row.get("classification") != "approve_already_ok_possible":
                continue
            if row.get("token_surface") not in {"plain_text", "light_token"}:
                continue
            text = str(row.get("output_text") or "")
            if DYNAMIC_RE.search(text):
                continue
            row["_line_number"] = line_number
            rows.append(row)
    return rows[:limit]


def token_signature(text: str) -> list[str]:
    return TOKEN_RE.findall(text or "")


def fetch_context(conn: sqlite3.Connection, segment_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
          s.id AS segment_id,
          s.relative_path,
          s.source_key,
          s.spanish_text,
          output.portuguese_text AS output_text,
          conf.confirmed_text,
          conf.confirmation_level,
          conf.confirmation_source,
          conf.confirmation_label,
          conf.locked
        FROM source_segments s
        LEFT JOIN output_segments output ON output.segment_id = s.id
        LEFT JOIN segment_confirmations conf ON conf.segment_id = s.id
        WHERE s.id = ?
        ORDER BY conf.id DESC
        LIMIT 1
        """,
        (segment_id,),
    ).fetchone()
    return dict(row) if row else None


def issue_closure_count(conn: sqlite3.Connection, segment_id: int, ledger_run_id: int) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND segment_id = ?
          AND COALESCE(status, 'open') NOT IN ('closed', 'resolved', 'dismissed')
        """,
        (ledger_run_id, segment_id),
    ).fetchone()
    return int(row["count"] or 0)


def validate(row: dict[str, Any], context: dict[str, Any] | None, closure_count: int) -> dict[str, Any]:
    reasons: list[str] = []
    segment_id = int(row["segment_id"])
    packet_output = str(row.get("output_text") or "")
    current_output = str(context.get("output_text") or "") if context else ""
    existing_confirmed = str((context or {}).get("confirmed_text") or row.get("confirmed_text") or "")
    effective_confirmed = current_output or packet_output
    if context is None:
        reasons.append("missing_segment")
    if not current_output:
        reasons.append("missing_current_output")
    if row.get("classification") != "approve_already_ok_possible":
        reasons.append("not_approve_already_ok_possible")
    if row.get("token_surface") not in {"plain_text", "light_token"}:
        reasons.append("unsupported_token_surface")
    if canonical_localization_text(current_output) != canonical_localization_text(packet_output):
        reasons.append("current_output_differs_from_probe")
    if int(row.get("needs_output_apply") or 0) != 0:
        reasons.append("needs_output_apply")
    if DYNAMIC_RE.search(current_output):
        reasons.append("dynamic_surface_detected")
    token_ok = token_signature(current_output) == token_signature(packet_output)
    if not token_ok:
        reasons.append("token_integrity_mismatch")
    structure_ok = current_output.count("\n") == packet_output.count("\n")
    if not structure_ok:
        reasons.append("structure_integrity_mismatch")
    if closure_count <= 0:
        reasons.append("no_open_issues_in_target_ledger")
    return {
        "source": SOURCE,
        "record_type": "post588_approve_ok_readiness_item",
        "segment_id": segment_id,
        "relative_path": row.get("relative_path") or (context or {}).get("relative_path"),
        "source_key": row.get("source_key") or (context or {}).get("source_key"),
        "status": "ready" if not reasons else "blocked",
        "block_reasons": reasons,
        "suggested_human_decision": "approve_already_ok",
        "classification": row.get("classification"),
        "token_surface": row.get("token_surface"),
        "expected_issue_closure_count": closure_count,
        "token_integrity_ok": token_ok,
        "structure_integrity_ok": structure_ok,
        "canonical_l10n_ok": canonical_localization_text(current_output) == canonical_localization_text(packet_output),
        "already_ok_confirmation_can_be_created": bool(current_output)
        and canonical_localization_text(current_output) == canonical_localization_text(packet_output),
        "existing_confirmed_text": existing_confirmed,
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "high_issue_count": int(row.get("high_issue_count") or 0),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "confirmation_level": (context or {}).get("confirmation_level"),
        "confirmation_source": (context or {}).get("confirmation_source"),
        "confirmation_label": (context or {}).get("confirmation_label"),
        "locked": int((context or {}).get("locked") or 0),
        "issue_families": row.get("issue_families") or "",
        "issue_kinds": row.get("issue_kinds") or "",
        "source_text": (context or {}).get("spanish_text") or row.get("source_text"),
        "output_text": current_output,
        "confirmed_text": effective_confirmed,
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


def markdown(summary: dict[str, Any], records: list[dict[str, Any]]) -> str:
    lines = [
        "# Post-588 High Issue Non-Dynamic Approve-OK Readiness",
        "",
        f"- ready_count: {summary['ready_count']}",
        f"- blocked_count: {summary['blocked_count']}",
        f"- expected_issue_closures: {summary['expected_issue_closure_count']}",
        "",
        "## Items",
    ]
    for record in records:
        lines.append(
            f"- {record['segment_id']} | {record['status']} | issues={record['expected_issue_closure_count']} | "
            f"{record['source_key']} | {','.join(record['block_reasons'])}"
        )
    lines.append("")
    return "\n".join(lines)


def write(records: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, str]:
    base = reports_dir() / f"{stamp()}_release_blocker_non_dynamic_post588_approve_ok_readiness"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    jsonl_path.write_text("".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records), encoding="utf-8")
    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    md_path.write_text(markdown(summary, records), encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary["output_files"]


def main() -> None:
    args = parse_args()
    rows = read_candidates(args.input_jsonl, args.limit)
    records: list[dict[str, Any]] = []
    with db.connect(db.load_settings()) as conn:
        for row in rows:
            segment_id = int(row["segment_id"])
            records.append(validate(row, fetch_context(conn, segment_id), issue_closure_count(conn, segment_id, args.ledger_run_id)))
    status_counts = Counter(record["status"] for record in records)
    block_counts = Counter(reason for record in records for reason in record["block_reasons"])
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_post588_approve_ok_readiness",
        "input_jsonl": str(args.input_jsonl),
        "ledger_run_id": args.ledger_run_id,
        "record_count": len(records),
        "ready_count": status_counts.get("ready", 0),
        "blocked_count": status_counts.get("blocked", 0),
        "ready_segment_ids": [record["segment_id"] for record in records if record["status"] == "ready"],
        "blocked_segment_ids": [record["segment_id"] for record in records if record["status"] == "blocked"],
        "block_reason_counts": dict(block_counts.most_common()),
        "expected_issue_closure_count": sum(record["expected_issue_closure_count"] for record in records if record["status"] == "ready"),
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
        "production_full_recommended_now": False,
        "single_operational_recommendation": "Process ready_count only in a separate approved cycle.",
    }
    outputs = write(records, summary)
    print(f"markdown={outputs['markdown']}")
    print(f"jsonl={outputs['jsonl']}")
    print(f"summary={outputs['summary']}")
    print(f"ready_count={summary['ready_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"expected_issue_closure_count={summary['expected_issue_closure_count']}")
    print(f"ready_segment_ids={json.dumps(summary['ready_segment_ids'], ensure_ascii=False)}")
    print(f"blocked_segment_ids={json.dumps(summary['blocked_segment_ids'], ensure_ascii=False)}")
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
