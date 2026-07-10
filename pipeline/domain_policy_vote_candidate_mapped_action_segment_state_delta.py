from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_mapped_action_segment_state_delta_v1"
INPUT_JSONL = Path("reports/20260701_203003_043617_domain_policy_vote_candidate_mapped_action_guard_missing_diagnostic_91.jsonl")
BEFORE_RUN_ID = 533
AFTER_RUN_ID = 535
EXPECTED_COUNT = 91
SAMPLE_LIMIT = 8


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only delta for mapped-action segment-state validation.")
    parser.add_argument("--input-jsonl", type=Path, default=INPUT_JSONL)
    parser.add_argument("--before-run-id", type=int, default=BEFORE_RUN_ID)
    parser.add_argument("--after-run-id", type=int, default=AFTER_RUN_ID)
    parser.add_argument("--expected-count", type=int, default=EXPECTED_COUNT)
    return parser.parse_args()


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


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def fetch_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM segment_state_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise SystemExit(f"missing segment_state_run {run_id}")
    return dict(row)


def fetch_states(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for index in range(0, len(segment_ids), 800):
        chunk = segment_ids[index : index + 800]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT segment_id, final_state, is_closed, confirmed_matches_output, needs_output_apply,
                   lifecycle_policy_allowed, lifecycle_policy_action
            FROM segment_state_items
            WHERE run_id = ? AND segment_id IN ({placeholders})
            """,
            (run_id, *chunk),
        ).fetchall()
        for row in rows:
            out[int(row["segment_id"])] = dict(row)
    return out


def build(input_jsonl: Path, before_run_id: int, after_run_id: int, expected_count: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    focus = read_jsonl(input_jsonl)
    if len(focus) != expected_count:
        raise SystemExit(f"focus count guard failed: {len(focus)}")
    segment_ids = [int(row["segment_id"]) for row in focus]
    with connect_readonly() as conn:
        before_run = fetch_run(conn, before_run_id)
        after_run = fetch_run(conn, after_run_id)
        before_states = fetch_states(conn, before_run_id, segment_ids)
        after_states = fetch_states(conn, after_run_id, segment_ids)
    if not after_run.get("finished_at"):
        raise SystemExit("after run is not complete")

    records: list[dict[str, Any]] = []
    for row in focus:
        sid = int(row["segment_id"])
        before = before_states.get(sid, {})
        after = after_states.get(sid, {})
        transition = f"{before.get('final_state')} -> {after.get('final_state')}"
        expected_closed = row.get("problem_class") == "fast_path_does_not_consume_existing_policy_item"
        expected_pending = row.get("problem_class") == "policy_item_absent"
        validation = "unexpected"
        if expected_closed and after.get("final_state") == "closed_auto_confirmed_guarded_lifecycle":
            validation = "expected_fast_path_closed"
        elif expected_pending and after.get("final_state") == "reopen_auto_confirmed_autofix":
            validation = "expected_policy_absent_still_pending"
        records.append(
            {
                "source": SOURCE,
                "segment_id": sid,
                "problem_class": row.get("problem_class"),
                "token_surface": row.get("token_surface"),
                "lifecycle_policy_action": row.get("lifecycle_policy_action"),
                "policy_item_status": row.get("policy_item_status"),
                "before_final_state": before.get("final_state"),
                "after_final_state": after.get("final_state"),
                "transition": transition,
                "before_is_closed": int(before.get("is_closed") or 0),
                "after_is_closed": int(after.get("is_closed") or 0),
                "after_confirmed_matches_output": int(after.get("confirmed_matches_output") or 0),
                "after_needs_output_apply": int(after.get("needs_output_apply") or 0),
                "validation": validation,
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )
    records.sort(key=lambda item: (item["validation"], item["segment_id"]))

    transition_counts = Counter(row["transition"] for row in records)
    validation_counts = Counter(row["validation"] for row in records)
    problem_after_counts = Counter(f"{row['problem_class']}::{row['after_final_state']}" for row in records)
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        if len(samples[row["validation"]]) < SAMPLE_LIMIT:
            samples[row["validation"]].append(row)

    global_delta = {
        "closed_count": int(after_run.get("closed_count") or 0) - int(before_run.get("closed_count") or 0),
        "pending_count": int(after_run.get("pending_count") or 0) - int(before_run.get("pending_count") or 0),
        "reopen_count": int(after_run.get("reopen_count") or 0) - int(before_run.get("reopen_count") or 0),
        "output_apply_pending_count": int(after_run.get("output_apply_pending_count") or 0)
        - int(before_run.get("output_apply_pending_count") or 0),
    }
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_mapped_action_segment_state_delta",
        "input_jsonl": str(input_jsonl),
        "before_run_id": before_run_id,
        "after_run_id": after_run_id,
        "record_count": len(records),
        "expected_record_count": expected_count,
        "before_run": {key: before_run.get(key) for key in ("id", "finished_at", "closed_count", "pending_count", "reopen_count", "output_apply_pending_count")},
        "after_run": {key: after_run.get(key) for key in ("id", "finished_at", "closed_count", "pending_count", "reopen_count", "output_apply_pending_count")},
        "global_delta": global_delta,
        "transition_counts": dict(transition_counts.most_common()),
        "validation_counts": dict(validation_counts.most_common()),
        "problem_after_state_counts": dict(problem_after_counts.most_common()),
        "fast_path_closed_expected_count": sum(1 for row in records if row["validation"] == "expected_fast_path_closed"),
        "policy_absent_pending_expected_count": sum(1 for row in records if row["validation"] == "expected_policy_absent_still_pending"),
        "unexpected_count": sum(1 for row in records if row["validation"] == "unexpected"),
        "needs_output_apply_delta_ok": global_delta["output_apply_pending_count"] <= 0,
        "samples_by_validation": samples,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 1,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": (
            "If validation_counts has no unexpected rows, proceed with separate read-only diagnostic for policy_item_absent; do not apply or reindex."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_mapped_action_segment_state_delta_{summary['before_run_id']}_{summary['after_run_id']}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Mapped action segment-state delta",
        f"record_count={summary['record_count']}",
        f"global_delta={json.dumps(summary['global_delta'], ensure_ascii=False, sort_keys=True)}",
        "",
        "Validation:",
    ]
    for key, count in summary["validation_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Transitions:"])
    for key, count in summary["transition_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(
        [
            "",
            "Guards:",
            "candidate_generation_count=0",
            "apply_count=0",
            "lifecycle_count=0",
            "segment_state_count=1",
            "reindex_count=0",
            "production_full_count=0",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records, summary = build(args.input_jsonl, args.before_run_id, args.after_run_id, args.expected_count)
    txt, jsonl, summary_path = write_reports(records, summary)
    print(f"txt={txt}")
    print(f"jsonl={jsonl}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"fast_path_closed_expected_count={summary['fast_path_closed_expected_count']}")
    print(f"policy_absent_pending_expected_count={summary['policy_absent_pending_expected_count']}")
    print(f"unexpected_count={summary['unexpected_count']}")
    print(f"needs_output_apply_delta={summary['global_delta']['output_apply_pending_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("segment_state_count=1")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
