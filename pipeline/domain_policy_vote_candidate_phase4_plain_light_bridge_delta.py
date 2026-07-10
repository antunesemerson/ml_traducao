from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from domain_policy_vote_candidate_phase4_plain_light_bridge_dry_run import POLICY_ACTION, POLICY_NAME, TARGET_FINAL_STATE


SOURCE = "domain_policy_vote_candidate_phase4_plain_light_bridge_delta_v1"
INPUT_JSONL = Path("reports/20260702_005558_451283_domain_policy_vote_candidate_phase4_plain_light_bridge_materializer_apply.jsonl")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only phase 4 plain/light bridge segment-state delta.")
    parser.add_argument("--input-jsonl", type=Path, default=INPUT_JSONL)
    parser.add_argument("--before-run-id", type=int, required=True)
    parser.add_argument("--after-run-id", type=int, required=True)
    parser.add_argument("--expected-closed", type=int, default=115)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
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
    for start in range(0, len(segment_ids), 800):
        chunk = segment_ids[start : start + 800]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT segment_id, final_state, is_closed, policy_action, lifecycle_policy_allowed,
                   lifecycle_policy_action, confirmed_matches_output, needs_output_apply
            FROM segment_state_items
            WHERE run_id = ? AND segment_id IN ({placeholders})
            """,
            (run_id, *chunk),
        ).fetchall()
        for row in rows:
            out[int(row["segment_id"])] = dict(row)
    return out


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    input_rows = read_jsonl(args.input_jsonl)
    segment_ids = [
        int(row["segment_id"])
        for row in input_rows
        if row.get("record_type") in {"policy_item", "released"}
    ]
    if len(segment_ids) != args.expected_closed:
        raise SystemExit(f"input guard failed: {len(segment_ids)}")
    with connect_readonly() as conn:
        before_run = fetch_run(conn, args.before_run_id)
        after_run = fetch_run(conn, args.after_run_id)
        before_states = fetch_states(conn, args.before_run_id, segment_ids)
        after_states = fetch_states(conn, args.after_run_id, segment_ids)

    records: list[dict[str, Any]] = []
    for row in input_rows:
        if row.get("record_type") not in {"policy_item", "released"}:
            continue
        segment_id = int(row["segment_id"])
        before = before_states.get(segment_id, {})
        after = after_states.get(segment_id, {})
        closed_expected = after.get("final_state") == TARGET_FINAL_STATE
        records.append(
            {
                "source": SOURCE,
                "segment_id": segment_id,
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "policy_name": POLICY_NAME,
                "policy_action": POLICY_ACTION,
                "target_final_state": TARGET_FINAL_STATE,
                "before_final_state": before.get("final_state"),
                "after_final_state": after.get("final_state"),
                "transition": f"{before.get('final_state')} -> {after.get('final_state')}",
                "after_policy_action": after.get("policy_action"),
                "after_lifecycle_policy_allowed": int(after.get("lifecycle_policy_allowed") or 0),
                "after_lifecycle_policy_action": after.get("lifecycle_policy_action"),
                "after_confirmed_matches_output": int(after.get("confirmed_matches_output") or 0),
                "after_needs_output_apply": int(after.get("needs_output_apply") or 0),
                "validation": "expected_closed" if closed_expected else "not_closed",
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 1,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )

    global_delta = {
        "closed_count": int(after_run.get("closed_count") or 0) - int(before_run.get("closed_count") or 0),
        "pending_count": int(after_run.get("pending_count") or 0) - int(before_run.get("pending_count") or 0),
        "reopen_count": int(after_run.get("reopen_count") or 0) - int(before_run.get("reopen_count") or 0),
        "output_apply_pending_count": int(after_run.get("output_apply_pending_count") or 0)
        - int(before_run.get("output_apply_pending_count") or 0),
    }
    validation_counts = Counter(record["validation"] for record in records)
    after_state_counts = Counter(str(record["after_final_state"]) for record in records)
    after_lifecycle_counts = Counter(str(record["after_lifecycle_policy_action"]) for record in records)
    after_policy_counts = Counter(str(record["after_policy_action"]) for record in records)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_phase4_plain_light_bridge_delta",
        "before_run_id": args.before_run_id,
        "after_run_id": args.after_run_id,
        "policy_name": POLICY_NAME,
        "policy_action": POLICY_ACTION,
        "target_final_state": TARGET_FINAL_STATE,
        "record_count": len(records),
        "expected_closed": args.expected_closed,
        "closed_expected_count": validation_counts.get("expected_closed", 0),
        "not_closed_count": validation_counts.get("not_closed", 0),
        "expected_outcome_met": validation_counts.get("expected_closed", 0) == args.expected_closed
        and global_delta["output_apply_pending_count"] == 0,
        "global_delta": global_delta,
        "validation_counts": dict(validation_counts.most_common()),
        "after_state_counts": dict(after_state_counts.most_common()),
        "after_policy_action_counts": dict(after_policy_counts.most_common()),
        "after_lifecycle_policy_action_counts": dict(after_lifecycle_counts.most_common()),
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
            "Checkpoint met. Continue with read-only post-closure diagnostics."
            if validation_counts.get("expected_closed", 0) == args.expected_closed and global_delta["output_apply_pending_count"] == 0
            else "Checkpoint not met. Do not rerun apply/reindex/production; return policy consumption to architecture."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_phase4_plain_light_bridge_delta_{summary['before_run_id']}_{summary['after_run_id']}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Phase 4 plain/light bridge delta",
        f"before_run_id={summary['before_run_id']}",
        f"after_run_id={summary['after_run_id']}",
        f"record_count={summary['record_count']}",
        f"closed_expected_count={summary['closed_expected_count']}",
        f"not_closed_count={summary['not_closed_count']}",
        f"global_delta={json.dumps(summary['global_delta'], ensure_ascii=False, sort_keys=True)}",
        f"after_state_counts={json.dumps(summary['after_state_counts'], ensure_ascii=False, sort_keys=True)}",
        f"after_lifecycle_policy_action_counts={json.dumps(summary['after_lifecycle_policy_action_counts'], ensure_ascii=False, sort_keys=True)}",
        "",
        "Recommendation:",
        summary["single_operational_recommendation"],
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records, summary = build(args)
    txt_path, jsonl_path, summary_path = write_reports(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"closed_expected_count={summary['closed_expected_count']}")
    print(f"not_closed_count={summary['not_closed_count']}")
    print(f"needs_output_apply_delta={summary['global_delta']['output_apply_pending_count']}")
    print(f"expected_outcome_met={summary['expected_outcome_met']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("segment_state_count=1")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
