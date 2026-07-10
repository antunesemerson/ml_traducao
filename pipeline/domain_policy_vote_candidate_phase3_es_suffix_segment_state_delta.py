from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import db


BEFORE_RUN_ID = 532
AFTER_RUN_ID = 533
APPLY_JSONL = Path("reports/20260701_164204_233293_domain_policy_vote_candidate_phase3_es_suffix_lifecycle_materializer_apply.jsonl")
EXPECTED_COUNT = 57
EXPECTED_AFTER_STATE = "closed_auto_confirmed_human_confirmed_misc_equal_output_es_suffix_lifecycle"
REPORT_STEM = "domain_policy_vote_candidate_phase3_es_suffix_segment_state_delta"


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def load_ids() -> list[int]:
    ids: list[int] = []
    with APPLY_JSONL.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                ids.append(int(json.loads(line)["segment_id"]))
    return sorted(set(ids))


def get_run(conn, run_id: int) -> dict:
    row = conn.execute(
        """
        SELECT id,total_segments,closed_count,pending_count,output_apply_pending_count,
               reopen_count,started_at,finished_at
        FROM segment_state_runs
        WHERE id=?
        """,
        (run_id,),
    ).fetchone()
    if not row:
        raise SystemExit(f"missing run {run_id}")
    return dict(row)


def states(conn, run_id: int, ids: list[int]) -> dict[int, dict]:
    placeholders = ",".join("?" for _ in ids)
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
        [run_id, *ids],
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def needs_output_apply(conn, run_id: int) -> tuple[int, list[int]]:
    rows = conn.execute(
        "SELECT segment_id FROM segment_state_items WHERE run_id=? AND needs_output_apply=1 ORDER BY segment_id",
        (run_id,),
    ).fetchall()
    ids = [int(row["segment_id"]) for row in rows]
    return len(ids), ids


def main() -> None:
    ids = load_ids()
    if len(ids) != EXPECTED_COUNT:
        raise SystemExit(f"expected {EXPECTED_COUNT}, got {len(ids)}")
    conn = db.connect(db.load_settings())
    conn.row_factory = db.sqlite3.Row
    before_run = get_run(conn, BEFORE_RUN_ID)
    after_run = get_run(conn, AFTER_RUN_ID)
    if not before_run["finished_at"] or not after_run["finished_at"]:
        raise SystemExit("incomplete run")
    before = states(conn, BEFORE_RUN_ID, ids)
    after = states(conn, AFTER_RUN_ID, ids)
    records = []
    transitions: Counter[str] = Counter()
    after_states: Counter[str] = Counter()
    failures = []
    for segment_id in ids:
        b = before.get(segment_id)
        a = after.get(segment_id)
        if not b or not a:
            record = {"segment_id": segment_id, "validation_status": "missing_state_row"}
            records.append(record)
            failures.append(record)
            continue
        transitions[f"{b['final_state']} -> {a['final_state']}"] += 1
        after_states[a["final_state"]] += 1
        reasons = []
        if a["final_state"] != EXPECTED_AFTER_STATE:
            reasons.append("after_final_state_unexpected")
        if int(a["is_closed"]) != 1:
            reasons.append("after_not_closed")
        if int(a["needs_output_apply"]) != 0:
            reasons.append("after_needs_output_apply")
        if int(a["confirmed_matches_output"]) != 1:
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

    remaining_count, remaining_ids = needs_output_apply(conn, AFTER_RUN_ID)
    summary = {
        "report": REPORT_STEM,
        "before_run_id": BEFORE_RUN_ID,
        "after_run_id": AFTER_RUN_ID,
        "policy_bridge": "human_confirmed_misc_equal_output_es_suffix_lifecycle_bridge",
        "policy_run_id": 98,
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
        "remaining_needs_output_apply_count": remaining_count,
        "remaining_needs_output_apply_segment_ids": remaining_ids,
        "output_apply_count": 0,
        "discovery_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
    }
    base = Path("reports") / f"{stamp()}_{REPORT_STEM}"
    txt = base.with_suffix(".txt")
    jsonl = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt.write_text(
        "\n".join(
            [
                "domain_policy_vote_candidate phase3 ES suffix segment-state delta",
                f"before_run_id: {BEFORE_RUN_ID}",
                f"after_run_id: {AFTER_RUN_ID}",
                "policy_bridge: human_confirmed_misc_equal_output_es_suffix_lifecycle_bridge",
                "policy_run_id: 98",
                f"focus_record_count: {summary['focus_record_count']}",
                f"focus_closed_expected_state_count: {summary['focus_closed_expected_state_count']}",
                f"validation_failure_count: {summary['validation_failure_count']}",
                f"global_closed_delta: {summary['global_delta']['closed_count']}",
                f"global_pending_delta: {summary['global_delta']['pending_count']}",
                f"global_reopen_delta: {summary['global_delta']['reopen_count']}",
                f"global_output_apply_pending_delta: {summary['global_delta']['output_apply_pending_count']}",
                f"remaining_needs_output_apply_count: {remaining_count}",
                f"remaining_needs_output_apply_segment_ids: {remaining_ids}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"wrote {txt}")
    print(f"wrote {jsonl}")
    print(f"wrote {summary_path}")


if __name__ == "__main__":
    main()
