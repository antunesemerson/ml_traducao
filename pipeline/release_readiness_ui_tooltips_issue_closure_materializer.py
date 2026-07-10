from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import db


SOURCE = "release_readiness_ui_tooltips_issue_closure_materializer_v1"
DRY_RUN_JSONL = Path("reports/20260702_162709_546165_release_readiness_ui_tooltips_issue_closure_dry_run.jsonl")
DRY_RUN_SUMMARY = Path("reports/20260702_162709_546165_release_readiness_ui_tooltips_issue_closure_dry_run_summary.json")
EXPECTED_ISSUE_COUNT = 37
EXPECTED_SEGMENT_COUNT = 17
TARGET_STATUS = "closed"
TARGET_VALIDATION_STATUS = "closed_resolved_by_human_release_review"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Protected materializer for reviewed UI/tooltips issue closure.")
    parser.add_argument("--dry-run-jsonl", type=Path, default=DRY_RUN_JSONL)
    parser.add_argument("--dry-run-summary", type=Path, default=DRY_RUN_SUMMARY)
    parser.add_argument("--expected-issue-count", type=int, default=EXPECTED_ISSUE_COUNT)
    parser.add_argument("--expected-segment-count", type=int, default=EXPECTED_SEGMENT_COUNT)
    parser.add_argument("--apply", action="store_true")
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


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db.get_database_path(db.load_settings()), timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_issue_rows(conn: sqlite3.Connection, issue_ids: list[int]) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in issue_ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM ml_issue_ledger_items
        WHERE id IN ({placeholders})
        """,
        issue_ids,
    ).fetchall()
    return {int(row["id"]): dict(row) for row in rows}


def validate_inputs(summary: dict[str, Any], dry_rows: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    guards = {
        "record_count": args.expected_issue_count,
        "released_count": args.expected_issue_count,
        "blocked_count": 0,
        "segment_count": args.expected_segment_count,
    }
    for key, expected in guards.items():
        if int(summary.get(key) or 0) != expected:
            raise SystemExit(f"summary guard failed: {key}={summary.get(key)} expected {expected}")
    released = [row for row in dry_rows if row.get("dry_run_decision") == "released"]
    if len(released) != args.expected_issue_count:
        raise SystemExit(f"released row count guard failed: {len(released)}")
    if len({int(row["segment_id"]) for row in released}) != args.expected_segment_count:
        raise SystemExit("segment count guard failed")
    if any(row.get("block_reasons") for row in released):
        raise SystemExit("released row has block reasons")
    return released


def build_records(rows: list[dict[str, Any]], current: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        issue_id = int(row["issue_id"])
        db_row = current.get(issue_id)
        reasons: list[str] = []
        if db_row is None:
            reasons.append("missing_issue_row")
            db_row = {}
        if int(db_row.get("segment_id") or 0) != int(row["segment_id"]):
            reasons.append("segment_id_mismatch")
        if db_row.get("issue_family") != row.get("issue_family"):
            reasons.append("issue_family_mismatch")
        if db_row.get("issue_kind") != row.get("issue_kind"):
            reasons.append("issue_kind_mismatch")
        if str(db_row.get("issue_severity") or "").lower() != str(row.get("issue_severity") or "").lower():
            reasons.append("issue_severity_mismatch")
        if str(db_row.get("status") or "open") not in {"open", "blocked"}:
            reasons.append("issue_status_not_open_or_blocked")
        if int(row.get("confirmed_matches_output") or 0) != 1:
            reasons.append("dry_run_confirmed_matches_output_not_1")
        if int(row.get("needs_output_apply") or 0) != 0:
            reasons.append("dry_run_needs_output_apply_not_0")
        records.append(
            {
                "source": SOURCE,
                "record_type": "issue_closure_materializer_item",
                "issue_id": issue_id,
                "issue_run_id": row.get("issue_run_id"),
                "segment_id": int(row["segment_id"]),
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "issue_family": row.get("issue_family"),
                "issue_kind": row.get("issue_kind"),
                "issue_severity": row.get("issue_severity"),
                "old_status": db_row.get("status"),
                "old_validation_status": db_row.get("validation_status"),
                "new_status": TARGET_STATUS,
                "new_validation_status": TARGET_VALIDATION_STATUS,
                "status": "ready" if not reasons else "blocked",
                "block_reasons": reasons,
                "backup_row": db_row,
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )
    return records


def apply_records(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> int:
    applied = 0
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
        applied += int(conn.total_changes >= applied)
    conn.commit()
    return len(records)


def post_validate(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> dict[str, Any]:
    issue_ids = [int(record["issue_id"]) for record in records]
    current = fetch_issue_rows(conn, issue_ids)
    ok = 0
    details: list[dict[str, Any]] = []
    for record in records:
        row = current.get(int(record["issue_id"]))
        matches = bool(
            row
            and row["status"] == TARGET_STATUS
            and row["validation_status"] == TARGET_VALIDATION_STATUS
            and int(row["segment_id"]) == int(record["segment_id"])
        )
        ok += int(matches)
        details.append({"issue_id": record["issue_id"], "segment_id": record["segment_id"], "matches": matches})
    return {"issue_status_ok": ok, "details": details}


def write_reports(summary: dict[str, Any], records: list[dict[str, Any]]) -> tuple[Path, Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_issue_closure_materializer_{summary['mode']}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    backup_path = Path(str(base) + "_backup.jsonl")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    with backup_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record["backup_row"], ensure_ascii=False, sort_keys=True) + "\n")
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
                "Release readiness UI/tooltips issue closure materializer",
                f"mode={summary['mode']}",
                f"issue_count={summary['issue_count']}",
                f"segment_count={summary['segment_count']}",
                f"ready_count={summary['ready_count']}",
                f"blocked_count={summary['blocked_count']}",
                f"closed_issue_count={summary['closed_issue_count']}",
                f"post_validation={json.dumps(summary['post_validation'], ensure_ascii=False, sort_keys=True)}",
                f"backup_jsonl={backup_path}",
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
    dry_summary = read_json(args.dry_run_summary)
    dry_rows = validate_inputs(dry_summary, read_jsonl(args.dry_run_jsonl), args)
    with connect() as conn:
        current = fetch_issue_rows(conn, [int(row["issue_id"]) for row in dry_rows])
        records = build_records(dry_rows, current)
        blocked = [record for record in records if record["status"] != "ready"]
        if blocked:
            mode = "blocked"
        closed = 0
        post_validation = {"issue_status_ok": 0, "details": []}
        if args.apply and not blocked:
            closed = apply_records(conn, records)
            post_validation = post_validate(conn, records)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": mode,
        "dry_run_jsonl": str(args.dry_run_jsonl),
        "dry_run_summary": str(args.dry_run_summary),
        "issue_count": len(records),
        "segment_count": len({int(record["segment_id"]) for record in records}),
        "ready_count": sum(1 for record in records if record["status"] == "ready"),
        "blocked_count": len([record for record in records if record["status"] != "ready"]),
        "block_reason_counts": dict(Counter(reason for record in records for reason in record["block_reasons"]).most_common()),
        "issue_family_counts": dict(Counter(str(record["issue_family"]) for record in records).most_common(50)),
        "issue_severity_counts": dict(Counter(str(record["issue_severity"]) for record in records).most_common()),
        "closed_issue_count": closed,
        "post_validation": post_validation,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
    }
    txt_path, jsonl_path, summary_path, backup_path = write_reports(summary, records)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"backup_jsonl={backup_path}")
    print(f"mode={summary['mode']}")
    print(f"issue_count={summary['issue_count']}")
    print(f"ready_count={summary['ready_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"closed_issue_count={summary['closed_issue_count']}")
    print("candidate_generation_count=0")
    print("output_apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
