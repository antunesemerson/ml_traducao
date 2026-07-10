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
from release_quality_post_evaluation_queue_readonly import classify_needs_apply, has_token_surface


SOURCE = "release_quality_priority_apply_preview_v1"
DEFAULT_QUEUES = (
    "manual_feedback_apply_priority",
    "orthographic_microfix_apply_review",
)


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


def latest_completed_run(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL AND total_segments > 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise SystemExit("No completed segment-state run found.")
    return int(row["id"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only priority apply preview for post-evaluation needs_apply queues.")
    parser.add_argument("--current-run", type=int, default=None)
    parser.add_argument("--queues", nargs="+", default=list(DEFAULT_QUEUES))
    parser.add_argument("--expected-count", type=int, default=None)
    return parser.parse_args()


def fetch_rows(conn: sqlite3.Connection, current_run: int) -> list[dict[str, Any]]:
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
            state.policy_action,
            state.confirmation_level,
            state.confirmation_label,
            state.locked,
            state.confirmed_matches_output,
            state.needs_output_apply,
            state.lifecycle_policy_action,
            state.lifecycle_policy_allowed,
            source.spanish_text,
            source.english_text,
            source.old_text,
            output.output_line_number,
            output.portuguese_text AS output_text,
            output.output_raw_line,
            confirm.confirmed_text,
            confirm.confirmation_source,
            confirm.confidence_score
        FROM segment_state_items state
        JOIN source_segments source ON source.id = state.segment_id
        JOIN output_segments output ON output.segment_id = state.segment_id
        JOIN segment_confirmations confirm ON confirm.segment_id = state.segment_id
        WHERE state.run_id = ?
          AND state.final_state = 'pending_apply_confirmed'
          AND state.needs_output_apply = 1
        ORDER BY state.relative_path, state.source_key, state.segment_id
        """,
        (current_run,),
    ).fetchall()
    records = [dict(row) for row in rows]
    for record in records:
        record["queue"] = classify_needs_apply(record)
        record["token_surface"] = has_token_surface(record)
    return records


def file_current_line(row: dict[str, Any], root: Path) -> tuple[str | None, str | None]:
    path = root / Path(str(row["relative_path"]))
    if not path.exists():
        return None, "missing_output_file"
    line_number = row.get("output_line_number")
    if line_number is None:
        return None, "missing_output_line"
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    index = int(line_number) - 1
    if index < 0 or index >= len(lines):
        return None, "line_out_of_range"
    return lines[index], None


def one_line_diff(before: str | None, after: str | None) -> str:
    before_lines = (before or "").splitlines() or [before or ""]
    after_lines = (after or "").splitlines() or [after or ""]
    return "\n".join(difflib.ndiff(before_lines, after_lines))


def validate_row(row: dict[str, Any], root: Path) -> tuple[list[str], str | None, str | None]:
    failures: list[str] = []
    if row.get("final_state") != "pending_apply_confirmed":
        failures.append("final_state_not_pending_apply_confirmed")
    if int(row.get("needs_output_apply") or 0) != 1:
        failures.append("needs_output_apply_not_1")
    if int(row.get("confirmed_matches_output") or 0) != 0:
        failures.append("confirmed_matches_output_not_0")
    if row.get("confirmed_text") in (None, ""):
        failures.append("missing_confirmed_text")

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


def build_preview(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    root = output_root()
    with connect_readonly() as conn:
        current_run = args.current_run or latest_completed_run(conn)
        run = conn.execute("SELECT * FROM segment_state_runs WHERE id = ?", (current_run,)).fetchone()
        if run is None or run["finished_at"] is None:
            raise SystemExit(f"segment-state run {current_run} is missing or incomplete")
        rows = [row for row in fetch_rows(conn, current_run) if row["queue"] in set(args.queues)]

    if args.expected_count is not None and len(rows) != args.expected_count:
        raise SystemExit(f"count guard failed: expected {args.expected_count}, got {len(rows)}")

    records: list[dict[str, Any]] = []
    for row in rows:
        failures, current_line, new_line = validate_row(row, root)
        output_text = str(row.get("output_text") or "")
        confirmed_text = str(row.get("confirmed_text") or "")
        ready = not failures
        records.append(
            {
                "source": SOURCE,
                "record_type": "priority_apply_preview",
                "segment_state_run_id": current_run,
                "segment_id": int(row["segment_id"]),
                "queue": row["queue"],
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "source_line_number": row.get("source_line_number"),
                "output_line_number": row.get("output_line_number"),
                "final_state": row.get("final_state"),
                "review_state": row.get("review_state"),
                "apply_state": row.get("apply_state"),
                "policy_action": row.get("policy_action"),
                "confirmation_level": row.get("confirmation_level"),
                "confirmation_source": row.get("confirmation_source"),
                "confirmation_label": row.get("confirmation_label"),
                "confidence_score": row.get("confidence_score"),
                "locked": int(row.get("locked") or 0),
                "needs_output_apply": int(row.get("needs_output_apply") or 0),
                "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
                "lifecycle_policy_action": row.get("lifecycle_policy_action"),
                "lifecycle_policy_allowed": int(row.get("lifecycle_policy_allowed") or 0),
                "token_surface": bool(row.get("token_surface")),
                "token_integrity_ok": structural_tokens(output_text) == structural_tokens(confirmed_text),
                "structure_integrity_ok": structural_tokens(output_text) == structural_tokens(confirmed_text),
                "current_output_guard_ok": not any(
                    failure
                    in {
                        "file_line_does_not_match_output_segments",
                        "missing_output_file",
                        "missing_output_line",
                        "line_out_of_range",
                        "current_line_without_quoted_value",
                    }
                    for failure in failures
                ),
                "ready_for_protected_apply": ready,
                "guard_failures": failures,
                "old_text": row.get("old_text"),
                "output_text": output_text,
                "confirmed_text": confirmed_text,
                "current_file_line": current_line,
                "preview_new_file_line": new_line,
                "text_diff_preview": one_line_diff(output_text, confirmed_text),
                "line_diff_preview": one_line_diff(current_line, new_line) if current_line and new_line else "",
                "requires_snapshot_before_apply": True,
                "requires_post_validation_after_apply": True,
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )

    queue_counts = Counter(record["queue"] for record in records)
    failure_counts = Counter(failure for record in records for failure in record["guard_failures"])
    label_counts = Counter(record["confirmation_label"] for record in records)
    path_counts = Counter(record["relative_path"] for record in records)
    ready_ids = [record["segment_id"] for record in records if record["ready_for_protected_apply"]]
    blocked_ids = [record["segment_id"] for record in records if not record["ready_for_protected_apply"]]
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_priority_apply_preview",
        "segment_state_run_id": current_run,
        "selected_queues": list(args.queues),
        "record_count": len(records),
        "ready_count": len(ready_ids),
        "blocked_count": len(blocked_ids),
        "queue_counts": queue_counts.most_common(),
        "guard_failure_counts": failure_counts.most_common(),
        "confirmation_label_counts": label_counts.most_common(),
        "path_counts": path_counts.most_common(),
        "ready_segment_ids": ready_ids,
        "blocked_segment_ids": blocked_ids,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "single_operational_recommendation": (
            "Proceed to a separate protected apply cycle for ready rows only."
            if ready_ids and not blocked_ids
            else "Do not apply as a batch; resolve blocked guard failures first."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    queue_slug = "_".join(str(queue).replace(" ", "_") for queue in summary["selected_queues"])
    base = reports_dir() / f"{stamp()}_release_quality_priority_apply_preview_{queue_slug}"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    markdown = [
        "# Release Quality Priority Apply Preview",
        "",
        f"- run: `{summary['segment_state_run_id']}`",
        f"- queues: `{', '.join(summary['selected_queues'])}`",
        f"- records: `{summary['record_count']}`",
        f"- ready: `{summary['ready_count']}`",
        f"- blocked: `{summary['blocked_count']}`",
        "",
        "## Queues",
        "",
    ]
    for queue, count in summary["queue_counts"]:
        markdown.append(f"- `{queue}`: `{count}`")
    markdown.extend(["", "## Records", ""])
    for record in records:
        markdown.extend(
            [
                f"### {record['segment_id']} - {record['queue']}",
                "",
                f"- path: `{record['relative_path']}`",
                f"- key: `{record['source_key']}`",
                f"- ready: `{record['ready_for_protected_apply']}`",
                f"- failures: `{record['guard_failures']}`",
                f"- label: `{record['confirmation_label']}`",
                "",
                "```diff",
                record["text_diff_preview"],
                "```",
                "",
            ]
        )
    summary["output_files"] = {
        "markdown": str(md_path),
        "jsonl": str(jsonl_path),
        "summary": str(summary_path),
    }
    md_path.write_text("\n".join(markdown) + "\n", encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return md_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records, summary = build_preview(args)
    md_path, jsonl_path, summary_path = write_reports(records, summary)
    print(f"markdown={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"ready_count={summary['ready_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"queue_counts={json.dumps(summary['queue_counts'], ensure_ascii=False)}")
    print(f"guard_failure_counts={json.dumps(summary['guard_failure_counts'], ensure_ascii=False)}")
    print(f"ready_segment_ids={summary['ready_segment_ids']}")
    print(f"blocked_segment_ids={summary['blocked_segment_ids']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")
    print("source_changed=false")
    print("output_changed=false")


if __name__ == "__main__":
    main()
