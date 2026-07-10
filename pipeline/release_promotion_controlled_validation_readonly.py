from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import replace_quoted_text
from apply_segment_state_updates import canonical_localization_text, structural_tokens


SOURCE = "release_promotion_controlled_validation_readonly_v1"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def output_root() -> Path:
    return db.project_path(db.load_settings()["output_spanish"])


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate audited promotions without applying them.")
    parser.add_argument("--audit-jsonl", type=Path, required=True)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def fetch_db_rows(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            state.segment_id,
            state.final_state,
            state.needs_output_apply,
            state.confirmed_matches_output,
            state.locked,
            output.output_line_number,
            output.output_raw_line,
            output.portuguese_text AS db_output_text,
            confirm.confirmed_text AS db_confirmed_text,
            confirm.confirmation_source AS db_confirmation_source
        FROM segment_state_items state
        JOIN output_segments output ON output.segment_id = state.segment_id
        JOIN segment_confirmations confirm ON confirm.segment_id = state.segment_id
        WHERE state.run_id = ?
          AND state.segment_id IN ({placeholders})
        """,
        [run_id, *segment_ids],
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def latest_ledger_run_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT MAX(id) AS id FROM ml_issue_ledger_runs WHERE finished_at IS NOT NULL"
    ).fetchone()
    return int(row["id"]) if row and row["id"] is not None else None


def fetch_open_ledger_counts(
    conn: sqlite3.Connection,
    ledger_run_id: int | None,
    segment_ids: list[int],
) -> dict[int, int]:
    if ledger_run_id is None or not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, COUNT(*) AS issue_count
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
          AND lower(COALESCE(status, 'open')) NOT LIKE 'closed%'
        GROUP BY segment_id
        """,
        [ledger_run_id, *segment_ids],
    ).fetchall()
    return {int(row["segment_id"]): int(row["issue_count"] or 0) for row in rows}


def file_validation(record: dict[str, Any], db_row: dict[str, Any], root: Path) -> list[str]:
    failures: list[str] = []
    path = root / Path(str(record.get("relative_path") or ""))
    if not path.exists():
        return ["missing_output_file"]
    line_number = db_row.get("output_line_number")
    if line_number is None:
        return ["missing_output_line_number"]
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    index = int(line_number) - 1
    if index < 0 or index >= len(lines):
        return ["output_line_out_of_range"]
    current_line = lines[index]
    output_text = str(db_row.get("db_output_text") or "")
    try:
        rebuilt = replace_quoted_text(current_line, output_text)
    except ValueError:
        return ["output_line_without_replaceable_value"]
    if rebuilt != current_line:
        failures.append("file_line_differs_from_output_db")
    return failures


def validate(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    eligible = [record for record in records if record.get("eligible_for_controlled_validation")]
    if not eligible:
        raise SystemExit("Audit has no records eligible for controlled validation.")
    run_ids = {int(record.get("segment_state_run_id") or 0) for record in eligible}
    if len(run_ids) != 1:
        raise SystemExit(f"Expected one segment-state run, found: {sorted(run_ids)}")
    run_id = next(iter(run_ids))
    segment_ids = [int(record["segment_id"]) for record in eligible]
    root = output_root()

    with connect_readonly() as conn:
        db_rows = fetch_db_rows(conn, run_id, segment_ids)
        ledger_run_id = latest_ledger_run_id(conn)
        open_ledger_counts = fetch_open_ledger_counts(conn, ledger_run_id, segment_ids)

    results: list[dict[str, Any]] = []
    for record in eligible:
        segment_id = int(record["segment_id"])
        db_row = db_rows.get(segment_id)
        failures: list[str] = []
        if db_row is None:
            failures.append("missing_segment_state_or_output_db_row")
        else:
            audit_output = str(record.get("output_text") or "")
            db_output = str(db_row.get("db_output_text") or "")
            confirmed = str(db_row.get("db_confirmed_text") or "")
            if canonical_localization_text(audit_output) != canonical_localization_text(db_output):
                failures.append("audit_output_differs_from_output_db")
            if canonical_localization_text(db_output) != canonical_localization_text(confirmed):
                failures.append("output_differs_from_confirmed")
            if structural_tokens(db_output) != structural_tokens(confirmed):
                failures.append("output_confirmed_token_signature_mismatch")
            if int(db_row.get("needs_output_apply") or 0) != 0:
                failures.append("unexpected_needs_output_apply")
            failures.extend(file_validation(record, db_row, root))

        if failures:
            readiness = "blocked"
        elif open_ledger_counts.get(segment_id, 0) > 0:
            readiness = "ready_for_issue_closure_dry_run"
        else:
            readiness = "ready_for_lifecycle_dry_run"

        results.append(
            {
                **record,
                "validation_source": SOURCE,
                "readiness": readiness,
                "validation_failures": failures,
                "db_final_state": db_row.get("final_state") if db_row else None,
                "db_confirmation_source": db_row.get("db_confirmation_source") if db_row else None,
                "open_ledger_issue_count": open_ledger_counts.get(segment_id, 0),
                "ready_for_apply": False,
                "apply_count": 0,
                "issue_closure_count": 0,
                "segment_state_count": 0,
                "output_changed": False,
            }
        )

    readiness_counts = Counter(result["readiness"] for result in results)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "segment_state_run_id": run_id,
        "ledger_run_id": ledger_run_id,
        "record_count": len(results),
        "readiness_counts": dict(readiness_counts),
        "ready_for_lifecycle_dry_run_count": readiness_counts["ready_for_lifecycle_dry_run"],
        "ready_for_issue_closure_dry_run_count": readiness_counts["ready_for_issue_closure_dry_run"],
        "blocked_count": readiness_counts["blocked"],
        "ready_for_apply_count": 0,
        "apply_count": 0,
        "issue_closure_count": 0,
        "segment_state_count": 0,
        "source_changed": False,
        "output_changed": False,
        "recommendation": (
            "Run issue-closure dry-run only for the issue-bearing validated records. "
            "Run lifecycle/materializer dry-run for issue-free records; do not apply in the same step."
        ),
    }
    return results, summary


def write_reports(results: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_promotion_controlled_validation_readonly"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "# Release Promotion Controlled Validation (read-only)",
        "",
        f"- segment-state: `{summary['segment_state_run_id']}`",
        f"- records: `{summary['record_count']}`",
        f"- lifecycle dry-run: `{summary['ready_for_lifecycle_dry_run_count']}`",
        f"- issue closure dry-run first: `{summary['ready_for_issue_closure_dry_run_count']}`",
        f"- blocked: `{summary['blocked_count']}`",
        f"- ready for apply now: `{summary['ready_for_apply_count']}`",
        "",
        "| ID | lane | readiness | score signals/high | open ledger | failures |",
        "|---:|---|---|---:|---|",
    ]
    for result in results:
        failures = ", ".join(result["validation_failures"]) or "-"
        lines.append(
            f"| {result['segment_id']} | `{result['lane']}` | `{result['readiness']}` | "
            f"{result['score_issue_count']}/{result['score_high_issue_count']} | "
            f"{result['open_ledger_issue_count']} | {failures} |"
        )
    lines.extend(
        [
            "",
            "## Guards",
            "",
            "- apply: `0`",
            "- issue closure: `0`",
            "- lifecycle/materializer: `0`",
            "- segment-state: `0`",
            "- source/output changed: `false`",
            "",
            f"Recommendation: {summary['recommendation']}",
        ]
    )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return md_path, jsonl_path, summary_path


def main() -> int:
    args = parse_args()
    records = read_jsonl(args.audit_jsonl)
    results, summary = validate(records)
    paths = write_reports(results, summary)
    print(json.dumps({"summary": summary, "artifacts": [str(path) for path in paths]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
