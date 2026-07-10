from __future__ import annotations

import argparse
import difflib
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import replace_quoted_text
from apply_segment_state_updates import canonical_localization_text, structural_tokens


SOURCE = "domain_policy_vote_candidate_pending_apply_run540_diff_preview_v1"
RUN_ID = 540
EXPECTED_COUNT = 12


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only diff preview for pending_apply_confirmed rows.")
    parser.add_argument("--run-id", type=int, default=RUN_ID)
    parser.add_argument("--expected-count", type=int, default=EXPECTED_COUNT)
    return parser.parse_args()


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def output_root() -> Path:
    return db.project_path(db.load_settings()["output_spanish"])


def fetch_rows(conn: sqlite3.Connection, run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            state.segment_id,
            state.relative_path,
            state.source_key,
            state.source_line_number,
            state.final_state,
            state.review_state,
            state.apply_state,
            state.confirmed_matches_output,
            state.needs_output_apply,
            state.confirmation_level,
            state.confirmation_label,
            state.locked,
            source.spanish_text,
            source.english_text,
            output.output_line_number,
            output.portuguese_text AS output_text,
            output.output_raw_line,
            confirm.confirmed_text,
            confirm.confirmation_source
        FROM segment_state_items state
        JOIN source_segments source ON source.id = state.segment_id
        JOIN output_segments output ON output.segment_id = state.segment_id
        JOIN segment_confirmations confirm ON confirm.segment_id = state.segment_id
        WHERE state.run_id = ?
          AND state.final_state = 'pending_apply_confirmed'
        ORDER BY state.segment_id
        """,
        (run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def one_line_diff(before: str, after: str) -> str:
    return "\n".join(difflib.ndiff(before.splitlines() or [before], after.splitlines() or [after]))


def file_current_line(row: dict[str, Any], root: Path) -> tuple[str | None, str | None]:
    path = root / Path(row["relative_path"])
    if not path.exists():
        return None, "missing_output_file"
    line_number = row.get("output_line_number")
    if line_number is None:
        return None, "missing_output_line"
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    line_index = int(line_number) - 1
    if line_index < 0 or line_index >= len(lines):
        return None, "line_out_of_range"
    return lines[line_index], None


def classify(row: dict[str, Any]) -> str:
    if structural_tokens(row.get("output_text")) == structural_tokens(row.get("confirmed_text")):
        return "same_structural_token_signature"
    return "token_or_structure_signature_mismatch"


def validate_row(row: dict[str, Any], root: Path) -> tuple[list[str], str | None, str | None]:
    failures: list[str] = []
    if row.get("final_state") != "pending_apply_confirmed":
        failures.append("final_state_not_pending_apply_confirmed")
    if int(row.get("needs_output_apply") or 0) != 1:
        failures.append("needs_output_apply_not_1")
    if int(row.get("confirmed_matches_output") or 0) != 0:
        failures.append("confirmed_matches_output_not_0")
    if str(row.get("confirmation_level") or "") != "human_confirmed":
        failures.append("confirmation_level_not_human_confirmed")
    output_text = str(row.get("output_text") or "")
    confirmed_text = str(row.get("confirmed_text") or "")
    if canonical_localization_text(output_text) == canonical_localization_text(confirmed_text):
        failures.append("canonical_output_already_equals_confirmed")
    if structural_tokens(output_text) != structural_tokens(confirmed_text):
        failures.append("token_or_structure_signature_mismatch")
    current_line, file_error = file_current_line(row, root)
    if file_error:
        failures.append(file_error)
        return failures, current_line, None
    assert current_line is not None
    try:
        line_with_current_db_text = replace_quoted_text(current_line, output_text)
    except ValueError:
        failures.append("current_line_without_quoted_value")
        return failures, current_line, None
    if line_with_current_db_text != current_line:
        failures.append("file_line_does_not_match_output_segments")
    try:
        new_line = replace_quoted_text(current_line, confirmed_text)
    except ValueError:
        failures.append("cannot_build_confirmed_line")
        return failures, current_line, None
    if new_line == current_line:
        failures.append("new_line_equals_current_line")
    return failures, current_line, new_line


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    root = output_root()
    with connect_readonly() as conn:
        run = conn.execute("SELECT * FROM segment_state_runs WHERE id = ?", (args.run_id,)).fetchone()
        if run is None or run["finished_at"] is None:
            raise SystemExit(f"segment_state_run_id {args.run_id} missing or incomplete")
        rows = fetch_rows(conn, args.run_id)
    if len(rows) != args.expected_count:
        raise SystemExit(f"pending_apply count guard failed: expected {args.expected_count}, got {len(rows)}")

    records: list[dict[str, Any]] = []
    for row in rows:
        failures, current_line, new_line = validate_row(row, root)
        ready = not failures
        output_text = str(row.get("output_text") or "")
        confirmed_text = str(row.get("confirmed_text") or "")
        records.append(
            {
                "source": SOURCE,
                "record_type": "diff_preview",
                "segment_state_run_id": args.run_id,
                "segment_id": int(row["segment_id"]),
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "source_line_number": row.get("source_line_number"),
                "output_line_number": row.get("output_line_number"),
                "final_state": row.get("final_state"),
                "review_state": row.get("review_state"),
                "apply_state": row.get("apply_state"),
                "confirmation_level": row.get("confirmation_level"),
                "confirmation_source": row.get("confirmation_source"),
                "confirmation_label": row.get("confirmation_label"),
                "locked": int(row.get("locked") or 0),
                "needs_output_apply": int(row.get("needs_output_apply") or 0),
                "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
                "classification": classify(row),
                "token_integrity_ok": structural_tokens(output_text) == structural_tokens(confirmed_text),
                "structure_integrity_ok": structural_tokens(output_text) == structural_tokens(confirmed_text),
                "current_output_guard_ok": "file_line_does_not_match_output_segments" not in failures
                and "missing_output_file" not in failures
                and "missing_output_line" not in failures
                and "line_out_of_range" not in failures
                and "current_line_without_quoted_value" not in failures,
                "ready_for_protected_apply": ready,
                "guard_failures": failures,
                "requires_snapshot_before_apply": True,
                "requires_rollback_path": True,
                "requires_post_validation_after_apply": True,
                "output_text": output_text,
                "confirmed_text": confirmed_text,
                "current_file_line": current_line,
                "preview_new_file_line": new_line,
                "text_diff_preview": one_line_diff(output_text, confirmed_text),
                "line_diff_preview": one_line_diff(current_line or "", new_line or "") if current_line and new_line else "",
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )

    classification_counts = Counter(row["classification"] for row in records)
    failure_counts = Counter(failure for row in records for failure in row["guard_failures"])
    path_counts = Counter(str(row["relative_path"]).split("/", 1)[0] for row in records)
    ready_count = sum(1 for row in records if row["ready_for_protected_apply"])
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_pending_apply_confirmed_diff_preview",
        "segment_state_run_id": args.run_id,
        "pending_apply_confirmed_count": len(records),
        "diff_preview_count": len(records),
        "ready_for_protected_apply_count": ready_count,
        "blocked_count": len(records) - ready_count,
        "token_integrity_ok_count": sum(1 for row in records if row["token_integrity_ok"]),
        "structure_integrity_ok_count": sum(1 for row in records if row["structure_integrity_ok"]),
        "current_output_guard_ok_count": sum(1 for row in records if row["current_output_guard_ok"]),
        "requires_apply_later_count": len(records),
        "requires_lifecycle_later_count": 0,
        "classification_counts": dict(classification_counts.most_common()),
        "guard_failure_counts": dict(failure_counts.most_common()),
        "path_group_counts_top": [{"key": key, "count": count} for key, count in path_counts.most_common(20)],
        "ready_segment_ids": [row["segment_id"] for row in records if row["ready_for_protected_apply"]],
        "blocked_segment_ids": [row["segment_id"] for row in records if not row["ready_for_protected_apply"]],
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": (
            "All 12 are ready for a separate protected apply dry-run/apply cycle."
            if ready_count == len(records)
            else "Do not apply yet; review blocked guard failures before creating a protected apply cycle."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_pending_apply_run{summary['segment_state_run_id']}_diff_preview"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "Pending apply confirmed diff preview",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"pending_apply_confirmed_count={summary['pending_apply_confirmed_count']}",
        f"ready_for_protected_apply_count={summary['ready_for_protected_apply_count']}",
        f"blocked_count={summary['blocked_count']}",
        f"token_integrity_ok_count={summary['token_integrity_ok_count']}",
        f"structure_integrity_ok_count={summary['structure_integrity_ok_count']}",
        f"current_output_guard_ok_count={summary['current_output_guard_ok_count']}",
        f"classification_counts={json.dumps(summary['classification_counts'], ensure_ascii=False, sort_keys=True)}",
        f"guard_failure_counts={json.dumps(summary['guard_failure_counts'], ensure_ascii=False, sort_keys=True)}",
        "",
        "Diff previews:",
    ]
    for row in records:
        lines.extend(
            [
                "",
                f"segment_id={row['segment_id']}",
                f"path={row['relative_path']}:{row['output_line_number']}",
                f"key={row['source_key']}",
                f"ready_for_protected_apply={row['ready_for_protected_apply']}",
                f"guard_failures={row['guard_failures']}",
                "output_text:",
                row["output_text"],
                "confirmed_text:",
                row["confirmed_text"],
                "text_diff:",
                row["text_diff_preview"],
                "line_diff:",
                row["line_diff_preview"],
            ]
        )
    lines.extend(["", "Recommendation:", summary["single_operational_recommendation"]])
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records, summary = build(args)
    txt_path, jsonl_path, summary_path = write_reports(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"pending_apply_confirmed_count={summary['pending_apply_confirmed_count']}")
    print(f"ready_for_protected_apply_count={summary['ready_for_protected_apply_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"token_integrity_ok_count={summary['token_integrity_ok_count']}")
    print(f"structure_integrity_ok_count={summary['structure_integrity_ok_count']}")
    print(f"current_output_guard_ok_count={summary['current_output_guard_ok_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
