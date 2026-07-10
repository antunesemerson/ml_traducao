from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_policy_item_absent_final_delta_v1"
INPUT_JSONL = Path("reports/20260701_220631_549784_domain_policy_vote_candidate_policy_item_absent_materialization_dry_run_9.jsonl")
EXPECTED_COUNT = 9
EXPECTED_CONTROLLED = "closed_auto_confirmed_controlled_token_subpolicy"
EXPECTED_SAME_TOKEN = "closed_auto_confirmed_same_token_boundary_repair"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only final delta for policy-item-absent closure.")
    parser.add_argument("--input-jsonl", type=Path, default=INPUT_JSONL)
    parser.add_argument("--before-run-id", type=int, required=True)
    parser.add_argument("--after-run-id", type=int, required=True)
    parser.add_argument("--expected-count", type=int, default=EXPECTED_COUNT)
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
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, final_state, is_closed, policy_action, lifecycle_policy_allowed,
               lifecycle_policy_action, confirmed_matches_output, needs_output_apply, reasons_json
        FROM segment_state_items
        WHERE run_id = ? AND segment_id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def fetch_policy_items(conn: sqlite3.Connection, segment_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT item.segment_id, run.id AS policy_run_id, run.policy_name, item.policy_action,
               item.policy_allowed, item.output_match_kind, item.token_status, item.block_reason
        FROM auto_confirmation_reopen_lifecycle_policy_items item
        JOIN auto_confirmation_reopen_lifecycle_policy_runs run ON run.id = item.run_id
        WHERE item.segment_id IN ({placeholders})
          AND run.policy_name = 'policy_item_absent_boundary_same_token_backfill'
        ORDER BY item.segment_id, item.policy_action
        """,
        segment_ids,
    ).fetchall()
    out: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(int(row["segment_id"]), []).append(dict(row))
    return out


def classify(final_state: str | None) -> str:
    if final_state == EXPECTED_CONTROLLED:
        return "expected_controlled_token_subpolicy_closed"
    if final_state == EXPECTED_SAME_TOKEN:
        return "expected_same_token_boundary_repair_closed"
    if final_state == "reopen_auto_confirmed_autofix":
        return "still_reopen_auto_confirmed_autofix"
    return "unexpected_state"


def build(input_jsonl: Path, before_run_id: int, after_run_id: int, expected_count: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    input_rows = read_jsonl(input_jsonl)
    if len(input_rows) != expected_count:
        raise SystemExit(f"input count guard failed: expected {expected_count}, got {len(input_rows)}")
    segment_ids = [int(row["segment_id"]) for row in input_rows]

    with connect_readonly() as conn:
        before_run = fetch_run(conn, before_run_id)
        after_run = fetch_run(conn, after_run_id)
        before_states = fetch_states(conn, before_run_id, segment_ids)
        after_states = fetch_states(conn, after_run_id, segment_ids)
        policy_items = fetch_policy_items(conn, segment_ids)

    records: list[dict[str, Any]] = []
    for row in input_rows:
        segment_id = int(row["segment_id"])
        before = before_states.get(segment_id, {})
        after = after_states.get(segment_id, {})
        after_final_state = after.get("final_state")
        records.append(
            {
                "source": SOURCE,
                "segment_id": segment_id,
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "decision_mode": row.get("decision_mode"),
                "recommended_policy_family": row.get("recommended_policy_family"),
                "before_final_state": before.get("final_state"),
                "after_final_state": after_final_state,
                "transition": f"{before.get('final_state')} -> {after_final_state}",
                "after_is_closed": int(after.get("is_closed") or 0),
                "after_policy_action": after.get("policy_action"),
                "after_lifecycle_policy_allowed": int(after.get("lifecycle_policy_allowed") or 0),
                "after_lifecycle_policy_action": after.get("lifecycle_policy_action"),
                "after_confirmed_matches_output": int(after.get("confirmed_matches_output") or 0),
                "after_needs_output_apply": int(after.get("needs_output_apply") or 0),
                "materialized_policy_item_count": len(policy_items.get(segment_id, [])),
                "materialized_policy_actions": [item["policy_action"] for item in policy_items.get(segment_id, [])],
                "validation": classify(after_final_state),
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
    after_policy_action_counts = Counter(str(record["after_policy_action"]) for record in records)
    after_lifecycle_policy_action_counts = Counter(str(record["after_lifecycle_policy_action"]) for record in records)
    transition_counts = Counter(record["transition"] for record in records)
    not_expected = [
        record["segment_id"]
        for record in records
        if record["validation"] not in {"expected_controlled_token_subpolicy_closed", "expected_same_token_boundary_repair_closed"}
    ]
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_final_segment_state_delta",
        "before_run_id": before_run_id,
        "after_run_id": after_run_id,
        "record_count": len(records),
        "expected_record_count": expected_count,
        "global_delta": global_delta,
        "after_state_counts": dict(after_state_counts.most_common()),
        "after_policy_action_counts": dict(after_policy_action_counts.most_common()),
        "after_lifecycle_policy_action_counts": dict(after_lifecycle_policy_action_counts.most_common()),
        "transition_counts": dict(transition_counts.most_common()),
        "validation_counts": dict(validation_counts.most_common()),
        "expected_controlled_token_subpolicy_closed_count": validation_counts.get(
            "expected_controlled_token_subpolicy_closed", 0
        ),
        "expected_same_token_boundary_repair_closed_count": validation_counts.get(
            "expected_same_token_boundary_repair_closed", 0
        ),
        "still_reopen_auto_confirmed_autofix_count": validation_counts.get("still_reopen_auto_confirmed_autofix", 0),
        "unexpected_state_count": validation_counts.get("unexpected_state", 0),
        "not_expected_segment_ids": not_expected,
        "all_9_left_reopen_auto_confirmed_autofix": validation_counts.get("still_reopen_auto_confirmed_autofix", 0) == 0,
        "needs_output_apply_delta_ok": global_delta["output_apply_pending_count"] <= 0,
        "expected_outcome_met": (
            validation_counts.get("expected_controlled_token_subpolicy_closed", 0) == 7
            and validation_counts.get("expected_same_token_boundary_repair_closed", 0) == 2
            and validation_counts.get("still_reopen_auto_confirmed_autofix", 0) == 0
            and global_delta["output_apply_pending_count"] <= 0
        ),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 1,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
    }
    summary["single_operational_recommendation"] = (
        "Checkpoint met. Keep output/reindex/production full in hold and move to the next read-only closure-debt tranche."
        if summary["expected_outcome_met"]
        else "Checkpoint not met. Keep apply/reindex/production full in hold and return the unexpected states to architecture/segment-state guard review."
    )
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / (
        f"{stamp()}_domain_policy_vote_candidate_policy_item_absent_final_delta_"
        f"{summary['before_run_id']}_{summary['after_run_id']}"
    )
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "Policy item absent final delta",
        f"before_run_id={summary['before_run_id']}",
        f"after_run_id={summary['after_run_id']}",
        f"record_count={summary['record_count']}",
        f"expected_outcome_met={summary['expected_outcome_met']}",
        f"global_delta={json.dumps(summary['global_delta'], ensure_ascii=False, sort_keys=True)}",
        "",
        "Validation:",
    ]
    for key, count in summary["validation_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "After states:"])
    for key, count in summary["after_state_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "After policy actions:"])
    for key, count in summary["after_policy_action_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "After lifecycle policy actions:"])
    for key, count in summary["after_lifecycle_policy_action_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Recommendation:", summary["single_operational_recommendation"]])
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records, summary = build(args.input_jsonl, args.before_run_id, args.after_run_id, args.expected_count)
    txt_path, jsonl_path, summary_path = write_reports(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"expected_outcome_met={summary['expected_outcome_met']}")
    print(f"expected_controlled_token_subpolicy_closed_count={summary['expected_controlled_token_subpolicy_closed_count']}")
    print(f"expected_same_token_boundary_repair_closed_count={summary['expected_same_token_boundary_repair_closed_count']}")
    print(f"still_reopen_auto_confirmed_autofix_count={summary['still_reopen_auto_confirmed_autofix_count']}")
    print(f"unexpected_state_count={summary['unexpected_state_count']}")
    print(f"needs_output_apply_delta={summary['global_delta']['output_apply_pending_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("segment_state_count=1")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
