from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import db


BEFORE_RUN_ID = 530
AFTER_RUN_ID = 531
APPLY_JSONL = Path("reports/20260701_141324_812810_domain_policy_vote_candidate_phase2_human_repair_label_bridge_materializer_apply.jsonl")
EXPECTED_COUNT = 325
EXPECTED_AFTER_STATE = "closed_auto_confirmed_human_confirmed_repair_label_close_lifecycle"
REPORT_STEM = "domain_policy_vote_candidate_phase2_human_repair_label_segment_state_delta"


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def load_segment_ids() -> list[int]:
    segment_ids: list[int] = []
    with APPLY_JSONL.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                segment_ids.append(int(json.loads(line)["segment_id"]))
    return sorted(set(segment_ids))


def get_run(conn, run_id: int) -> dict:
    row = conn.execute(
        """
        SELECT id, total_segments, closed_count, pending_count,
               output_apply_pending_count, reopen_count, started_at, finished_at
        FROM segment_state_runs
        WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    if not row:
        raise SystemExit(f"missing segment_state_run {run_id}")
    return dict(row)


def fetch_state_rows(conn, run_id: int, segment_ids: list[int]) -> dict[int, dict]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT segment_id, relative_path, source_key, final_state, state_group,
               apply_state, policy_action, confirmation_level, confirmation_label,
               locked, confirmed_matches_output, needs_output_apply, needs_reopen,
               is_closed, lifecycle_policy_allowed, lifecycle_policy_action
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        ORDER BY segment_id
        """,
        [run_id, *segment_ids],
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def remaining_needs_output_apply(conn, run_id: int) -> tuple[int, list[int]]:
    rows = conn.execute(
        """
        SELECT segment_id
        FROM segment_state_items
        WHERE run_id = ?
          AND needs_output_apply = 1
        ORDER BY segment_id
        """,
        (run_id,),
    ).fetchall()
    ids = [int(row["segment_id"]) for row in rows]
    return len(ids), ids


def main() -> None:
    segment_ids = load_segment_ids()
    if len(segment_ids) != EXPECTED_COUNT:
        raise SystemExit(f"expected {EXPECTED_COUNT} segment ids, got {len(segment_ids)}")

    conn = db.connect(db.load_settings())
    conn.row_factory = db.sqlite3.Row
    before_run = get_run(conn, BEFORE_RUN_ID)
    after_run = get_run(conn, AFTER_RUN_ID)
    if before_run["finished_at"] is None or after_run["finished_at"] is None:
        raise SystemExit("incomplete segment-state run in delta")

    before_rows = fetch_state_rows(conn, BEFORE_RUN_ID, segment_ids)
    after_rows = fetch_state_rows(conn, AFTER_RUN_ID, segment_ids)

    records: list[dict] = []
    transition_counts: Counter[str] = Counter()
    after_state_counts: Counter[str] = Counter()
    failures: list[dict] = []

    for segment_id in segment_ids:
        before = before_rows.get(segment_id)
        after = after_rows.get(segment_id)
        if not before or not after:
            record = {
                "segment_id": segment_id,
                "validation_status": "missing_state_row",
                "has_before": bool(before),
                "has_after": bool(after),
            }
            records.append(record)
            failures.append(record)
            continue
        transition = f"{before['final_state']} -> {after['final_state']}"
        transition_counts[transition] += 1
        after_state_counts[after["final_state"]] += 1
        reasons: list[str] = []
        if after["final_state"] != EXPECTED_AFTER_STATE:
            reasons.append("after_final_state_unexpected")
        if int(after["is_closed"]) != 1:
            reasons.append("after_not_closed")
        if int(after["needs_output_apply"]) != 0:
            reasons.append("after_needs_output_apply")
        if int(after["confirmed_matches_output"]) != 1:
            reasons.append("after_confirmed_not_matching_output")
        record = {
            "segment_id": segment_id,
            "relative_path": after["relative_path"],
            "source_key": after["source_key"],
            "before_final_state": before["final_state"],
            "after_final_state": after["final_state"],
            "before_state_group": before["state_group"],
            "after_state_group": after["state_group"],
            "before_needs_output_apply": before["needs_output_apply"],
            "after_needs_output_apply": after["needs_output_apply"],
            "before_confirmed_matches_output": before["confirmed_matches_output"],
            "after_confirmed_matches_output": after["confirmed_matches_output"],
            "after_policy_action": after["policy_action"],
            "after_lifecycle_policy_allowed": after["lifecycle_policy_allowed"],
            "after_lifecycle_policy_action": after["lifecycle_policy_action"],
            "validation_status": "fail" if reasons else "ok",
            "validation_reasons": reasons,
        }
        records.append(record)
        if reasons:
            failures.append(record)

    remaining_count, remaining_ids = remaining_needs_output_apply(conn, AFTER_RUN_ID)
    summary = {
        "report": REPORT_STEM,
        "before_run_id": BEFORE_RUN_ID,
        "after_run_id": AFTER_RUN_ID,
        "policy_bridge": "human_confirmed_repair_label_close_lifecycle_bridge",
        "policy_run_id": 96,
        "focus_record_count": len(records),
        "focus_closed_expected_state_count": after_state_counts[EXPECTED_AFTER_STATE],
        "validation_failure_count": len(failures),
        "transition_counts": dict(sorted(transition_counts.items())),
        "after_state_counts": dict(sorted(after_state_counts.items())),
        "global_delta": {
            "closed_count": after_run["closed_count"] - before_run["closed_count"],
            "pending_count": after_run["pending_count"] - before_run["pending_count"],
            "reopen_count": after_run["reopen_count"] - before_run["reopen_count"],
            "output_apply_pending_count": after_run["output_apply_pending_count"]
            - before_run["output_apply_pending_count"],
        },
        "before_run": before_run,
        "after_run": after_run,
        "remaining_needs_output_apply_count": remaining_count,
        "remaining_needs_output_apply_segment_ids": remaining_ids,
        "phase_3_excluded": True,
        "phase_4_excluded": True,
        "debug_existing_policy_consumption_excluded": True,
        "holds_excluded": True,
        "output_apply_count": 0,
        "discovery_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
    }

    reports_dir = Path("reports")
    stamp = utc_stamp()
    txt_path = reports_dir / f"{stamp}_{REPORT_STEM}.txt"
    jsonl_path = reports_dir / f"{stamp}_{REPORT_STEM}.jsonl"
    summary_path = reports_dir / f"{stamp}_{REPORT_STEM}_summary.json"

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "domain_policy_vote_candidate phase2 human repair-label segment-state delta",
        f"before_run_id: {BEFORE_RUN_ID}",
        f"after_run_id: {AFTER_RUN_ID}",
        "policy_bridge: human_confirmed_repair_label_close_lifecycle_bridge",
        "policy_run_id: 96",
        f"focus_record_count: {summary['focus_record_count']}",
        f"focus_closed_expected_state_count: {summary['focus_closed_expected_state_count']}",
        f"validation_failure_count: {summary['validation_failure_count']}",
        f"global_closed_delta: {summary['global_delta']['closed_count']}",
        f"global_pending_delta: {summary['global_delta']['pending_count']}",
        f"global_reopen_delta: {summary['global_delta']['reopen_count']}",
        f"global_output_apply_pending_delta: {summary['global_delta']['output_apply_pending_count']}",
        f"remaining_needs_output_apply_count: {remaining_count}",
        f"remaining_needs_output_apply_segment_ids: {remaining_ids}",
        "transition_counts:",
    ]
    lines.extend(f"- {key}: {value}" for key, value in summary["transition_counts"].items())
    lines.extend(
        [
            "guards:",
            "- output_apply_count: 0",
            "- discovery_count: 0",
            "- reindex_count: 0",
            "- production_full_count: 0",
            f"jsonl: {jsonl_path}",
            f"summary: {summary_path}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"wrote {txt_path}")
    print(f"wrote {jsonl_path}")
    print(f"wrote {summary_path}")


if __name__ == "__main__":
    main()
