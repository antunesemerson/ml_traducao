from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import canonical_localization_text


SOURCE = "release_readiness_ui_tooltips_packet3_approve_ok_issue_closure_v1"
DEFAULT_INPUT_JSONL = Path("reports/20260703_130759_913573_release_readiness_ui_tooltips_packet3_approve_ok_readiness.jsonl")
DEFAULT_SEGMENT_STATE_RUN_ID = 574
DEFAULT_LEDGER_RUN_ID = 76
EXPECTED_SEGMENT_COUNT = 30
TARGET_STATUS = "closed"
TARGET_VALIDATION_STATUS = "closed_resolved_by_human_approve_already_ok"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Close issue ledger rows resolved by packet3 approve-already-ok human review.")
    parser.add_argument("--input-jsonl", type=Path, default=DEFAULT_INPUT_JSONL)
    parser.add_argument("--segment-state-run-id", type=int, default=DEFAULT_SEGMENT_STATE_RUN_ID)
    parser.add_argument("--ledger-run-id", type=int, default=DEFAULT_LEDGER_RUN_ID)
    parser.add_argument("--expected-segment-count", type=int, default=EXPECTED_SEGMENT_COUNT)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def read_ids(path: Path) -> list[int]:
    ids: list[int] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("status", "ready") == "ready" or row.get("suggested_human_decision") == "approve_already_ok":
                ids.append(int(row["segment_id"]))
    return sorted(set(ids))


def fetch_rows(conn: sqlite3.Connection, ids: list[int], state_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        WITH latest_confirmation AS (
          SELECT c.*
          FROM segment_confirmations c
          JOIN (
            SELECT segment_id, MAX(id) AS max_id
            FROM segment_confirmations
            WHERE segment_id IN ({placeholders})
            GROUP BY segment_id
          ) latest ON latest.segment_id = c.segment_id AND latest.max_id = c.id
        )
        SELECT
          issue.id AS issue_id,
          issue.run_id AS issue_run_id,
          issue.segment_id,
          issue.relative_path,
          issue.source_key,
          issue.issue_family,
          issue.issue_kind,
          issue.issue_severity,
          issue.status AS old_status,
          issue.validation_status AS old_validation_status,
          state.final_state,
          state.state_group,
          state.confirmed_matches_output,
          state.needs_output_apply,
          output.portuguese_text AS output_text,
          confirmation.confirmed_text,
          confirmation.confirmation_level,
          confirmation.confirmation_source,
          confirmation.confirmation_label,
          confirmation.locked
        FROM ml_issue_ledger_items issue
        JOIN segment_state_items state
          ON state.segment_id = issue.segment_id
         AND state.run_id = ?
        JOIN output_segments output
          ON output.segment_id = issue.segment_id
        JOIN latest_confirmation confirmation
          ON confirmation.segment_id = issue.segment_id
        WHERE issue.run_id = ?
          AND issue.segment_id IN ({placeholders})
          AND COALESCE(issue.status, 'open') NOT IN ('closed', 'resolved', 'dismissed')
        ORDER BY issue.segment_id, issue.id
        """,
        [*ids, state_run_id, ledger_run_id, *ids],
    ).fetchall()
    return [dict(row) for row in rows]


def classify(row: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if row.get("old_status") not in {"open", "blocked"}:
        reasons.append("issue_not_open_or_blocked")
    if str(row.get("issue_severity") or "").lower() in {"high", "critical", "error"}:
        reasons.append("high_issue")
    if row.get("final_state") != "reopen_auto_confirmed_autofix":
        reasons.append("unexpected_final_state")
    if row.get("state_group") != "pending":
        reasons.append("not_pending")
    if row.get("confirmation_level") != "human_confirmed":
        reasons.append("not_human_confirmed")
    if int(row.get("locked") or 0) != 1:
        reasons.append("not_locked")
    if int(row.get("needs_output_apply") or 0) != 0:
        reasons.append("needs_output_apply")
    if canonical_localization_text(row.get("output_text") or "") != canonical_localization_text(row.get("confirmed_text") or ""):
        reasons.append("canonical_output_not_confirmed")
    return ("released" if not reasons else "blocked"), reasons


def apply_records(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> None:
    for record in records:
        conn.execute(
            """
            UPDATE ml_issue_ledger_items
            SET status = ?,
                validation_status = ?
            WHERE id = ?
              AND segment_id = ?
            """,
            (TARGET_STATUS, TARGET_VALIDATION_STATUS, int(record["issue_id"]), int(record["segment_id"])),
        )
    conn.commit()


def post_validate(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> dict[str, Any]:
    ok = 0
    for record in records:
        row = conn.execute(
            "SELECT status, validation_status FROM ml_issue_ledger_items WHERE id=? AND segment_id=?",
            (int(record["issue_id"]), int(record["segment_id"])),
        ).fetchone()
        ok += int(bool(row and row["status"] == TARGET_STATUS and row["validation_status"] == TARGET_VALIDATION_STATUS))
    return {"issue_status_ok": ok}


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any], backup_rows: list[dict[str, Any]]) -> tuple[Path, Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_packet3_approve_ok_issue_closure_{summary['mode']}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    backup_path = Path(str(base) + "_backup.jsonl")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    with backup_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in backup_rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {
        "txt": str(txt_path),
        "jsonl": str(jsonl_path),
        "summary": str(summary_path),
        "backup_jsonl": str(backup_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                "UI/tooltips packet3 approve-ok issue closure",
                f"mode={summary['mode']}",
                f"issue_count={summary['issue_count']}",
                f"segment_count={summary['segment_count']}",
                f"released_count={summary['released_count']}",
                f"blocked_count={summary['blocked_count']}",
                f"closed_issue_count={summary['closed_issue_count']}",
                f"block_reason_counts={json.dumps(summary['block_reason_counts'], ensure_ascii=False, sort_keys=True)}",
                "candidate_generation_count=0",
                "output_apply_count=0",
                "lifecycle_count=0",
                "segment_state_count=0",
                "reindex_count=0",
                "production_full_count=0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return txt_path, jsonl_path, summary_path, backup_path


def main() -> None:
    args = parse_args()
    mode = "apply" if args.apply else "dry_run"
    ids = read_ids(args.input_jsonl)
    if len(ids) != args.expected_segment_count:
        raise SystemExit(f"expected {args.expected_segment_count} segments, got {len(ids)}")
    with db.connect(db.load_settings()) as conn:
        raw_rows = fetch_rows(conn, ids, args.segment_state_run_id, args.ledger_run_id)
        records: list[dict[str, Any]] = []
        backup_rows = [dict(row) for row in raw_rows]
        for row in raw_rows:
            decision, reasons = classify(row)
            records.append(
                {
                    "source": SOURCE,
                    "record_type": "issue_closure_item",
                    "issue_id": int(row["issue_id"]),
                    "issue_run_id": int(row["issue_run_id"]),
                    "segment_id": int(row["segment_id"]),
                    "relative_path": row["relative_path"],
                    "source_key": row["source_key"],
                    "issue_family": row["issue_family"],
                    "issue_kind": row["issue_kind"],
                    "issue_severity": row["issue_severity"],
                    "old_status": row["old_status"],
                    "old_validation_status": row["old_validation_status"],
                    "new_status": TARGET_STATUS,
                    "new_validation_status": TARGET_VALIDATION_STATUS,
                    "dry_run_decision": decision,
                    "block_reasons": reasons,
                    "candidate_generation_count": 0,
                    "apply_count": 0,
                    "lifecycle_count": 0,
                    "segment_state_count": 0,
                    "reindex_count": 0,
                    "production_full_count": 0,
                }
            )
        blocked = [record for record in records if record["dry_run_decision"] != "released"]
        released = [record for record in records if record["dry_run_decision"] == "released"]
        if args.apply and blocked:
            raise SystemExit("blocked records present; refusing apply")
        post_validation = {"issue_status_ok": 0}
        if args.apply:
            apply_records(conn, released)
            post_validation = post_validate(conn, released)
    block_counts = Counter(reason for record in blocked for reason in record.get("block_reasons", []))
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": mode,
        "input_jsonl": str(args.input_jsonl),
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": args.ledger_run_id,
        "issue_count": len(records),
        "segment_count": len(set(record["segment_id"] for record in records)),
        "released_count": len(released),
        "blocked_count": len(blocked),
        "closed_issue_count": len(released) if args.apply else 0,
        "issue_family_counts": dict(Counter(record["issue_family"] for record in records).most_common()),
        "block_reason_counts": dict(block_counts.most_common()),
        "post_validation": post_validation,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
    }
    txt, jsonl, summary_path, backup = write_reports(records, summary, backup_rows)
    print(f"txt={txt}")
    print(f"jsonl={jsonl}")
    print(f"summary={summary_path}")
    print(f"backup_jsonl={backup}")
    print(f"mode={mode}")
    print(f"issue_count={summary['issue_count']}")
    print(f"segment_count={summary['segment_count']}")
    print(f"released_count={summary['released_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"closed_issue_count={summary['closed_issue_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
