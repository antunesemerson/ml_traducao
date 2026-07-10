from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import db


BEFORE_RUN_ID = 541
EXPECTED_COUNT = 1118
EXPECTED_AFTER_STATE = "closed_auto_confirmed_human_confirmed_misc_equal_output_multiline_serialization_lifecycle"
APPLY_JSONL = Path(
    "reports/20260702_121600_743601_domain_policy_vote_candidate_phase3_multiline_serialization_bridge_dry_run.jsonl"
)
REPORT_STEM = "domain_policy_vote_candidate_phase3_multiline_serialization_segment_state_delta"


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate segment-state delta for phase3 multiline serialization bridge.")
    parser.add_argument("--before-run-id", type=int, default=BEFORE_RUN_ID)
    parser.add_argument("--after-run-id", type=int)
    parser.add_argument("--input-jsonl", type=Path, default=APPLY_JSONL)
    parser.add_argument("--expected-count", type=int, default=EXPECTED_COUNT)
    return parser.parse_args()


def read_ids(path: Path) -> list[int]:
    ids: list[int] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                ids.append(int(json.loads(line)["segment_id"]))
    return sorted(set(ids))


def latest_run_id(conn) -> int:
    row = conn.execute("SELECT MAX(id) AS id FROM segment_state_runs").fetchone()
    if not row or row["id"] is None:
        raise SystemExit("missing segment_state_runs")
    return int(row["id"])


def get_run(conn, run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id,total_segments,closed_count,pending_count,output_apply_pending_count,
               reopen_count,started_at,finished_at
        FROM segment_state_runs
        WHERE id=?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"missing run {run_id}")
    return dict(row)


def states(conn, run_id: int, ids: list[int]) -> dict[int, dict[str, Any]]:
    output: dict[int, dict[str, Any]] = {}
    for offset in range(0, len(ids), 800):
        chunk = ids[offset : offset + 800]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT segment_id,relative_path,source_key,final_state,state_group,apply_state,
                   policy_action,confirmation_level,confirmation_label,locked,
                   confirmed_matches_output,needs_output_apply,needs_reopen,is_closed,
                   lifecycle_policy_allowed,lifecycle_policy_action
            FROM segment_state_items
            WHERE run_id=? AND segment_id IN ({placeholders})
            ORDER BY segment_id
            """,
            [run_id, *chunk],
        ).fetchall()
        output.update({int(row["segment_id"]): dict(row) for row in rows})
    return output


def needs_output_apply(conn, run_id: int) -> tuple[int, list[int]]:
    rows = conn.execute(
        "SELECT segment_id FROM segment_state_items WHERE run_id=? AND needs_output_apply=1 ORDER BY segment_id",
        (run_id,),
    ).fetchall()
    ids = [int(row["segment_id"]) for row in rows]
    return len(ids), ids


def main() -> None:
    args = parse_args()
    ids = read_ids(args.input_jsonl)
    if len(ids) != args.expected_count:
        raise SystemExit(f"expected {args.expected_count}, got {len(ids)}")
    conn = db.connect(db.load_settings())
    after_run_id = args.after_run_id or latest_run_id(conn)
    if after_run_id <= args.before_run_id:
        raise SystemExit(f"after_run_id must be greater than before_run_id: {after_run_id} <= {args.before_run_id}")
    before_run = get_run(conn, args.before_run_id)
    after_run = get_run(conn, after_run_id)
    before = states(conn, args.before_run_id, ids)
    after = states(conn, after_run_id, ids)
    before_output_apply_count, before_output_apply_ids = needs_output_apply(conn, args.before_run_id)
    after_output_apply_count, after_output_apply_ids = needs_output_apply(conn, after_run_id)

    records: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    transitions: Counter[str] = Counter()
    after_states: Counter[str] = Counter()
    for segment_id in ids:
        b = before.get(segment_id)
        a = after.get(segment_id)
        reasons: list[str] = []
        if not b or not a:
            reasons.append("missing_state_row")
            record = {"segment_id": segment_id, "validation_status": "fail", "validation_reasons": reasons}
            records.append(record)
            failures.append(record)
            continue
        transitions[f"{b['final_state']} -> {a['final_state']}"] += 1
        after_states[str(a["final_state"])] += 1
        if a["final_state"] != EXPECTED_AFTER_STATE:
            reasons.append("after_final_state_unexpected")
        if int(a["is_closed"] or 0) != 1:
            reasons.append("after_not_closed")
        if int(a["needs_output_apply"] or 0) != 0:
            reasons.append("after_needs_output_apply")
        if int(a["confirmed_matches_output"] or 0) != 1:
            reasons.append("after_confirmed_not_matching_output")
        record = {
            "segment_id": segment_id,
            "relative_path": a["relative_path"],
            "source_key": a["source_key"],
            "before_final_state": b["final_state"],
            "after_final_state": a["final_state"],
            "before_state_group": b["state_group"],
            "after_state_group": a["state_group"],
            "before_needs_output_apply": b["needs_output_apply"],
            "after_needs_output_apply": a["needs_output_apply"],
            "before_confirmed_matches_output": b["confirmed_matches_output"],
            "after_confirmed_matches_output": a["confirmed_matches_output"],
            "after_lifecycle_policy_allowed": a["lifecycle_policy_allowed"],
            "after_lifecycle_policy_action": a["lifecycle_policy_action"],
            "validation_status": "fail" if reasons else "ok",
            "validation_reasons": reasons,
        }
        records.append(record)
        if reasons:
            failures.append(record)

    summary = {
        "schema_version": 1,
        "report": REPORT_STEM,
        "before_run_id": args.before_run_id,
        "after_run_id": after_run_id,
        "focus_record_count": len(records),
        "focus_closed_expected_state_count": after_states[EXPECTED_AFTER_STATE],
        "validation_failure_count": len(failures),
        "transition_counts": dict(sorted(transitions.items())),
        "after_state_counts": dict(sorted(after_states.items())),
        "global_delta": {
            "closed_count": after_run["closed_count"] - before_run["closed_count"],
            "pending_count": after_run["pending_count"] - before_run["pending_count"],
            "reopen_count": after_run["reopen_count"] - before_run["reopen_count"],
            "output_apply_pending_count": after_run["output_apply_pending_count"] - before_run["output_apply_pending_count"],
        },
        "before_run": before_run,
        "after_run": after_run,
        "before_needs_output_apply_count": before_output_apply_count,
        "after_needs_output_apply_count": after_output_apply_count,
        "before_needs_output_apply_segment_ids": before_output_apply_ids,
        "after_needs_output_apply_segment_ids": after_output_apply_ids,
        "expected_outcome_met": len(failures) == 0 and after_output_apply_count <= before_output_apply_count,
        "output_apply_count": 0,
        "discovery_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
    }

    base = reports_dir() / f"{stamp()}_{REPORT_STEM}"
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
                "domain_policy_vote_candidate phase3 multiline serialization segment-state delta",
                f"before_run_id={args.before_run_id}",
                f"after_run_id={after_run_id}",
                f"focus_record_count={summary['focus_record_count']}",
                f"focus_closed_expected_state_count={summary['focus_closed_expected_state_count']}",
                f"validation_failure_count={summary['validation_failure_count']}",
                f"global_closed_delta={summary['global_delta']['closed_count']}",
                f"global_pending_delta={summary['global_delta']['pending_count']}",
                f"global_reopen_delta={summary['global_delta']['reopen_count']}",
                f"global_output_apply_pending_delta={summary['global_delta']['output_apply_pending_count']}",
                f"before_needs_output_apply_count={before_output_apply_count}",
                f"after_needs_output_apply_count={after_output_apply_count}",
                f"expected_outcome_met={summary['expected_outcome_met']}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")


if __name__ == "__main__":
    main()
