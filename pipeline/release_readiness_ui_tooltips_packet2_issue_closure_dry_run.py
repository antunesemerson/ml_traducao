from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_readiness_ui_tooltips_packet2_issue_closure_dry_run_v1"
TRIAGE_JSONL = Path("reports/20260702_184827_999672_release_readiness_ui_tooltips_packet2_corrected_issue_triage.jsonl")
PREVIEW_JSONL = Path("reports/20260702_190649_403737_release_readiness_ui_tooltips_packet2_corrected_preview.jsonl")
APPLY_SUMMARY = Path("reports/20260702_191416_274867_release_readiness_ui_tooltips_packet2_corrected_apply_apply_summary.json")
SEGMENT_STATE_RUN_ID = 554
LEDGER_RUN_ID = 76
EXPECTED_ISSUE_COUNT = 12
EXPECTED_SEGMENT_COUNT = 6
TARGET_VALIDATION_STATUS = "closed_resolved_by_human_release_review"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only issue closure dry-run for packet2 corrected UI/tooltips issues.")
    parser.add_argument("--triage-jsonl", type=Path, default=TRIAGE_JSONL)
    parser.add_argument("--preview-jsonl", type=Path, default=PREVIEW_JSONL)
    parser.add_argument("--apply-summary", type=Path, default=APPLY_SUMMARY)
    parser.add_argument("--segment-state-run-id", type=int, default=SEGMENT_STATE_RUN_ID)
    parser.add_argument("--ledger-run-id", type=int, default=LEDGER_RUN_ID)
    parser.add_argument("--expected-issue-count", type=int, default=EXPECTED_ISSUE_COUNT)
    parser.add_argument("--expected-segment-count", type=int, default=EXPECTED_SEGMENT_COUNT)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(db.project_path(path).read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def fetch_state(conn: sqlite3.Connection, state_run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, final_state, confirmation_level, confirmation_label, locked,
               review_state, confirmed_matches_output, needs_output_apply
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        """,
        [state_run_id, *segment_ids],
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def fetch_issues_by_segment(conn: sqlite3.Connection, ledger_run_id: int, segment_ids: list[int]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
          AND COALESCE(status, 'open') NOT IN ('closed', 'resolved', 'dismissed')
          AND lower(COALESCE(issue_severity, '')) NOT IN ('high', 'critical', 'error')
        ORDER BY segment_id, id
        """,
        [ledger_run_id, *segment_ids],
    ).fetchall()
    return [dict(row) for row in rows]


def build_records(args: argparse.Namespace) -> list[dict[str, Any]]:
    preview_rows = [
        row
        for row in read_jsonl(args.preview_jsonl)
        if row.get("record_type") == "corrected_text_diff_preview"
        and row.get("status") == "ready"
        and row.get("issue_resolution_class") == "resolved_by_corrected_text"
    ]
    apply_summary = read_json(args.apply_summary)
    if int(apply_summary.get("reviewed_count") or 0) != args.expected_segment_count:
        raise SystemExit("apply summary reviewed_count guard failed")
    post_validation = apply_summary.get("post_validation") or {}
    if int(post_validation.get("file_ok") or 0) != args.expected_segment_count:
        raise SystemExit("apply summary file_ok guard failed")
    if int(post_validation.get("output_db_ok") or 0) != args.expected_segment_count:
        raise SystemExit("apply summary output_db_ok guard failed")
    if int(post_validation.get("confirmation_ok") or 0) != args.expected_segment_count:
        raise SystemExit("apply summary confirmation_ok guard failed")
    segment_ids = sorted({int(row["segment_id"]) for row in preview_rows})
    if len(segment_ids) != args.expected_segment_count:
        raise SystemExit("preview segment count guard failed")

    with connect_readonly() as conn:
        states = fetch_state(conn, args.segment_state_run_id, segment_ids)
        issues = fetch_issues_by_segment(conn, args.ledger_run_id, segment_ids)
    if len(issues) != args.expected_issue_count:
        raise SystemExit(f"issue count guard failed: {len(issues)} expected {args.expected_issue_count}")
    preview_by_segment = {int(row["segment_id"]): row for row in preview_rows}

    records: list[dict[str, Any]] = []
    for issue in issues:
        issue_id = int(issue["id"])
        segment_id = int(issue["segment_id"])
        preview = preview_by_segment[segment_id]
        state = states.get(segment_id)
        reasons: list[str] = []
        if state is None:
            reasons.append("missing_segment_state")
            state = {}
        if issue.get("status") not in {"open", "blocked"}:
            reasons.append("issue_not_open_or_blocked")
        if str(issue.get("issue_severity") or "").lower() in {"high", "critical", "error"}:
            reasons.append("high_issue")
        if state.get("confirmation_level") != "human_confirmed":
            reasons.append("state_not_human_confirmed")
        if int(state.get("locked") or 0) != 1:
            reasons.append("state_not_locked")
        if state.get("review_state") != "human_locked":
            reasons.append("review_state_not_human_locked")
        if int(state.get("confirmed_matches_output") or 0) != 1:
            reasons.append("confirmed_matches_output_not_1")
        if int(state.get("needs_output_apply") or 0) != 0:
            reasons.append("needs_output_apply")
        records.append(
            {
                "source": SOURCE,
                "record_type": "issue_closure_dry_run",
                "issue_id": issue_id,
                "issue_run_id": args.ledger_run_id,
                "segment_id": segment_id,
                "relative_path": issue.get("relative_path"),
                "source_key": issue.get("source_key"),
                "issue_family": issue.get("issue_family"),
                "issue_kind": issue.get("issue_kind"),
                "issue_severity": issue.get("issue_severity"),
                "triage_class": preview.get("issue_resolution_class"),
                "triage_reason": "resolved by protected corrected_text apply and validated output/confirmation alignment",
                "current_status": issue.get("status"),
                "target_status": "closed",
                "target_validation_status": TARGET_VALIDATION_STATUS,
                "state_final_state": state.get("final_state"),
                "state_confirmation_level": state.get("confirmation_level"),
                "state_confirmation_label": state.get("confirmation_label"),
                "state_locked": int(state.get("locked") or 0),
                "state_review_state": state.get("review_state"),
                "state_confirmed_matches_output": int(state.get("confirmed_matches_output") or 0),
                "state_needs_output_apply": int(state.get("needs_output_apply") or 0),
                "dry_run_decision": "released" if not reasons else "blocked",
                "block_reasons": reasons,
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )
    return records


def write_reports(records: list[dict[str, Any]], args: argparse.Namespace) -> tuple[Path, Path, Path]:
    released = [record for record in records if record["dry_run_decision"] == "released"]
    blocked = [record for record in records if record["dry_run_decision"] != "released"]
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "dry_run",
        "triage_jsonl": str(args.triage_jsonl),
        "apply_summary": str(args.apply_summary),
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": args.ledger_run_id,
        "record_count": len(records),
        "segment_count": len({int(record["segment_id"]) for record in records}),
        "released_count": len(released),
        "blocked_count": len(blocked),
        "released_issue_ids": [int(record["issue_id"]) for record in released],
        "released_segment_ids": sorted({int(record["segment_id"]) for record in released}),
        "blocked_issue_ids": [int(record["issue_id"]) for record in blocked],
        "blocked_segment_ids": sorted({int(record["segment_id"]) for record in blocked}),
        "issue_family_counts": dict(Counter(record["issue_family"] for record in records).most_common()),
        "issue_kind_counts": dict(Counter(record["issue_kind"] for record in records).most_common()),
        "block_reason_counts": dict(Counter(reason for record in blocked for reason in record["block_reasons"]).most_common()),
        "expected_issue_count": args.expected_issue_count,
        "expected_segment_count": args.expected_segment_count,
        "guards_100_percent": len(released) == args.expected_issue_count and not blocked,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "single_operational_recommendation": (
            "Dry-run is clean; next step may materialize issue closure only after explicit approval."
            if len(released) == args.expected_issue_count and not blocked
            else "Do not close issues; resolve blocked guards first."
        ),
    }
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_packet2_issue_closure_dry_run"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                "UI/tooltips packet2 issue closure dry-run",
                f"record_count={summary['record_count']}",
                f"segment_count={summary['segment_count']}",
                f"released_count={summary['released_count']}",
                f"blocked_count={summary['blocked_count']}",
                f"guards_100_percent={summary['guards_100_percent']}",
                f"issue_family_counts={json.dumps(summary['issue_family_counts'], ensure_ascii=False, sort_keys=True)}",
                f"block_reason_counts={json.dumps(summary['block_reason_counts'], ensure_ascii=False, sort_keys=True)}",
                "candidate_generation_count=0",
                "apply_count=0",
                "lifecycle_count=0",
                "segment_state_count=0",
                "reindex_count=0",
                "production_full_count=0",
                "",
                f"recommendation={summary['single_operational_recommendation']}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records = build_records(args)
    txt_path, jsonl_path, summary_path = write_reports(records, args)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={len(records)}")
    print(f"released_count={sum(1 for r in records if r['dry_run_decision'] == 'released')}")
    print(f"blocked_count={sum(1 for r in records if r['dry_run_decision'] != 'released')}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
