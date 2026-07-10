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
from apply_segment_state_updates import structural_tokens
from release_quality_token_policy_family_review_readonly import reports_dir, short


SOURCE = "release_quality_style_suffix_policy_dry_run_v1"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only strict dry-run for style suffix literal token deltas.")
    parser.add_argument("--family-review-jsonl", type=Path, required=True)
    parser.add_argument("--expected-count", type=int, default=None)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    resolved = db.project_path(path)
    rows: list[dict[str, Any]] = []
    with resolved.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def output_root() -> Path:
    return db.project_path(db.load_settings()["output_spanish"])


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def fetch_live_output(conn: sqlite3.Connection, segment_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
            source.id AS segment_id,
            source.relative_path,
            source.source_key,
            source.is_active,
            output.output_line_number,
            output.portuguese_text AS output_text
        FROM source_segments source
        LEFT JOIN output_segments output ON output.segment_id = source.id
        WHERE source.id = ?
        LIMIT 1
        """,
        (segment_id,),
    ).fetchone()
    return dict(row) if row else None


def current_file_text(root: Path, live: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    relative_path = live.get("relative_path")
    line_number = live.get("output_line_number")
    if not relative_path:
        return None, None, "missing_relative_path"
    if line_number is None:
        return None, None, "missing_output_line_number"
    path = root / Path(str(relative_path))
    if not path.exists():
        return None, None, "missing_output_file"
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    index = int(line_number) - 1
    if index < 0 or index >= len(lines):
        return None, None, "output_line_out_of_range"
    raw_line = lines[index]
    first = raw_line.find('"')
    last = raw_line.rfind('"')
    if first < 0 or last <= first:
        return raw_line, None, "file_line_without_quoted_value"
    return raw_line, raw_line[first + 1 : last].replace('\\"', '"'), None


def validate_record(record: dict[str, Any], live: dict[str, Any] | None, root: Path) -> tuple[list[str], str | None]:
    failures: list[str] = []
    output_text = str(record.get("output_text") or "")
    confirmed_text = str(record.get("confirmed_text") or "")
    flags = set(record.get("flags") or [])
    added_tokens = list(record.get("added_tokens") or [])
    removed_tokens = list(record.get("removed_tokens") or [])

    if record.get("route") != "route_style_suffix_literal_policy_candidate":
        failures.append("route_not_style_suffix_literal_candidate")
    if record.get("candidate_for_policy_design") is not True:
        failures.append("not_policy_design_candidate")
    if {"getter", "select_cstring", "local_player", "multiline", "concept"} & flags:
        failures.append("runtime_or_context_flag_present")
    if not added_tokens and not removed_tokens:
        failures.append("missing_token_delta")
    if structural_tokens(output_text) != structural_tokens(confirmed_text):
        failures.append("structural_tokens_mismatch")
    if live is None:
        failures.append("missing_live_segment")
        return failures, None
    if int(live.get("is_active") or 0) != 1:
        failures.append("source_not_active")
    if live.get("relative_path") != record.get("relative_path"):
        failures.append("live_relative_path_mismatch")
    if live.get("source_key") != record.get("source_key"):
        failures.append("live_source_key_mismatch")
    if str(live.get("output_text") or "") != output_text:
        failures.append("live_output_text_mismatch")

    raw_line, file_text, file_error = current_file_text(root, live)
    if file_error:
        failures.append(file_error)
        return failures, raw_line
    if file_text != output_text:
        failures.append("file_output_text_mismatch")
    try:
        replace_quoted_text(raw_line or "", confirmed_text)
    except ValueError:
        failures.append("cannot_build_preview_line")
    return failures, raw_line


def build_report(family_review_jsonl: Path, expected_count: int | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    input_records = [
        row
        for row in read_jsonl(family_review_jsonl)
        if row.get("route") == "route_style_suffix_literal_policy_candidate"
    ]
    if expected_count is not None and len(input_records) != expected_count:
        raise SystemExit(f"count guard failed: expected {expected_count}, got {len(input_records)}")

    root = output_root()
    records: list[dict[str, Any]] = []
    with connect_readonly() as conn:
        for row in input_records:
            live = fetch_live_output(conn, int(row.get("segment_id") or 0))
            failures, raw_line = validate_record(row, live, root)
            records.append(
                {
                    "source": SOURCE,
                    "record_type": "style_suffix_policy_dry_run",
                    "segment_id": int(row.get("segment_id") or 0),
                    "relative_path": row.get("relative_path"),
                    "source_key": row.get("source_key"),
                    "token_delta_decision": row.get("token_delta_decision"),
                    "candidate_for_policy_apply_preview": not failures,
                    "ready_for_apply": False,
                    "guard_failures": failures,
                    "added_tokens": row.get("added_tokens") or [],
                    "removed_tokens": row.get("removed_tokens") or [],
                    "current_file_line": raw_line,
                    "output_text": row.get("output_text"),
                    "confirmed_text": row.get("confirmed_text"),
                    "next_action": (
                        "eligible_for_strict_style_suffix_apply_preview_no_write"
                        if not failures
                        else "keep_hold_until_guard_is_understood"
                    ),
                    "apply_count": 0,
                    "ingest_count": 0,
                    "segment_state_count": 0,
                    "production_full_count": 0,
                }
            )

    failure_counts = Counter(failure for record in records for failure in record["guard_failures"])
    ready_count = sum(1 for record in records if record["candidate_for_policy_apply_preview"])
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "family_review_jsonl": str(db.project_path(family_review_jsonl)),
        "record_count": len(records),
        "candidate_for_policy_apply_preview_count": ready_count,
        "blocked_count": len(records) - ready_count,
        "ready_for_apply_count": 0,
        "failure_counts": failure_counts.most_common(),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "ingest_count": 0,
        "issue_closure_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "single_operational_recommendation": (
            "If this remains clean, keep it behind a small policy dry-run. Do not combine with nickname policy."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_quality_style_suffix_policy_dry_run"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "# Release Quality Style Suffix Policy Dry-Run",
        "",
        f"- records: `{summary['record_count']}`",
        f"- policy apply preview candidates: `{summary['candidate_for_policy_apply_preview_count']}`",
        f"- blocked: `{summary['blocked_count']}`",
        f"- ready for apply now: `{summary['ready_for_apply_count']}`",
        "",
        "## Failure Counts",
        "",
    ]
    if summary["failure_counts"]:
        for failure, count in summary["failure_counts"]:
            lines.append(f"- `{failure}`: `{count}`")
    else:
        lines.append("- none")
    lines.extend(["", "## Candidate Samples", ""])
    for record in [item for item in records if item["candidate_for_policy_apply_preview"]][:20]:
        lines.extend(
            [
                f"### {record['segment_id']} - {record['source_key']}",
                f"- added: `{record['added_tokens']}`",
                f"- removed: `{record['removed_tokens']}`",
                f"- output: {short(record['output_text'])}",
                f"- confirmed: {short(record['confirmed_text'])}",
                "",
            ]
        )

    summary["output_files"] = {
        "markdown": str(md_path),
        "jsonl": str(jsonl_path),
        "summary": str(summary_path),
    }
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return md_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records, summary = build_report(args.family_review_jsonl, args.expected_count)
    md_path, jsonl_path, summary_path = write_reports(records, summary)
    print(f"markdown={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"candidate_for_policy_apply_preview_count={summary['candidate_for_policy_apply_preview_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"ready_for_apply_count={summary['ready_for_apply_count']}")
    print(f"failure_counts={json.dumps(summary['failure_counts'], ensure_ascii=False)}")
    print("apply_count=0")
    print("ingest_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")
    print("source_changed=false")
    print("output_changed=false")


if __name__ == "__main__":
    main()
