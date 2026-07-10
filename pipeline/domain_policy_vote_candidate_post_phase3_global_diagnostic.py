from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_post_phase3_global_diagnostic_v1"
CLOSURE_PACKET_SUMMARY = Path("reports/20260701_202156_749132_domain_policy_vote_candidate_closure_debt_architecture_packet_512_533_summary.json")
PHASE1_DELTA_SUMMARY = Path("reports/20260701_162208_269724_domain_policy_vote_candidate_phase1_human_package_close_segment_state_delta_summary.json")
PHASE2_DELTA_SUMMARY = Path("reports/20260701_171853_436296_domain_policy_vote_candidate_phase2_human_repair_label_segment_state_delta_summary.json")
PHASE3_PLAIN_DELTA_SUMMARY = Path("reports/20260701_184525_135277_domain_policy_vote_candidate_phase3_plain_light_segment_state_delta_summary.json")
PHASE3_ES_DELTA_SUMMARY = Path("reports/20260701_195335_506639_domain_policy_vote_candidate_phase3_es_suffix_segment_state_delta_summary.json")
PHASE3_FINAL_SUMMARY = Path("reports/20260701_201054_710162_domain_policy_vote_candidate_phase3_final_consolidation_summary.json")
FROM_RUN_ID = 512


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only global diagnostic after phase 3 residual classification.")
    parser.add_argument("--closure-packet-summary", type=Path, default=CLOSURE_PACKET_SUMMARY)
    parser.add_argument("--phase1-delta-summary", type=Path, default=PHASE1_DELTA_SUMMARY)
    parser.add_argument("--phase2-delta-summary", type=Path, default=PHASE2_DELTA_SUMMARY)
    parser.add_argument("--phase3-plain-delta-summary", type=Path, default=PHASE3_PLAIN_DELTA_SUMMARY)
    parser.add_argument("--phase3-es-delta-summary", type=Path, default=PHASE3_ES_DELTA_SUMMARY)
    parser.add_argument("--phase3-final-summary", type=Path, default=PHASE3_FINAL_SUMMARY)
    parser.add_argument("--from-run-id", type=int, default=FROM_RUN_ID)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(db.project_path(path).read_text(encoding="utf-8"))


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def latest_complete_run(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL AND total_segments > 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise SystemExit("missing complete segment_state run")
    return dict(row)


def state_counts(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    final_state_counts = {
        str(row["final_state"]): int(row["count"])
        for row in conn.execute(
            """
            SELECT final_state, COUNT(*) AS count
            FROM segment_state_items
            WHERE run_id = ? AND is_closed = 0
            GROUP BY final_state
            ORDER BY count DESC
            """,
            (run_id,),
        )
    }
    needs_output_counts = {
        str(row["final_state"]): int(row["count"])
        for row in conn.execute(
            """
            SELECT final_state, COUNT(*) AS count
            FROM segment_state_items
            WHERE run_id = ? AND needs_output_apply = 1
            GROUP BY final_state
            ORDER BY count DESC
            """,
            (run_id,),
        )
    }
    pending_apply_ids = [
        int(row["segment_id"])
        for row in conn.execute(
            """
            SELECT segment_id
            FROM segment_state_items
            WHERE run_id = ? AND final_state = 'pending_apply_confirmed'
            ORDER BY segment_id
            """,
            (run_id,),
        )
    ]
    return {
        "final_state_pending_counts": final_state_counts,
        "needs_output_apply_counts": needs_output_counts,
        "pending_apply_confirmed_segment_ids": pending_apply_ids,
    }


def closed_delta(summary: dict[str, Any]) -> int:
    return int((summary.get("global_delta") or {}).get("closed_count") or 0)


def validate_zero_ops(*summaries: dict[str, Any]) -> None:
    for summary in summaries:
        for key in ("candidate_generation_count", "apply_count", "lifecycle_count", "segment_state_count", "reindex_count", "production_full_count"):
            if int(summary.get(key) or 0) != 0:
                raise SystemExit(f"{summary.get('source') or summary.get('report')} {key} guard failed")


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    closure = read_json(args.closure_packet_summary)
    phase1 = read_json(args.phase1_delta_summary)
    phase2 = read_json(args.phase2_delta_summary)
    phase3_plain = read_json(args.phase3_plain_delta_summary)
    phase3_es = read_json(args.phase3_es_delta_summary)
    phase3_final = read_json(args.phase3_final_summary)
    validate_zero_ops(closure, phase3_final)

    with connect_readonly() as conn:
        current_run = latest_complete_run(conn)
        current_state = state_counts(conn, int(current_run["id"]))

    current_run_id = int(current_run["id"])
    if current_run_id != int(closure.get("to_run_id") or current_run_id):
        raise SystemExit("closure packet is not aligned with latest complete run")

    phase_closed = {
        "phase_1_human_package_close": closed_delta(phase1),
        "phase_2_human_repair_label": closed_delta(phase2),
        "phase_3_plain_light": closed_delta(phase3_plain),
        "phase_3_es_suffix": closed_delta(phase3_es),
    }
    phase_closed_total = sum(phase_closed.values())

    classification = closure.get("classification_counts") or {}
    architecture_decisions = closure.get("architecture_decision_counts") or {}
    recommended_plan = {row["phase"]: int(row.get("count") or 0) for row in closure.get("recommended_policy_plan") or []}
    phase3_rows = phase3_final.get("consolidated_rows") or []

    remaining_rows = [
        {
            "record_type": "closed_measure",
            "family": "phase_1_human_package_close",
            "count": phase_closed["phase_1_human_package_close"],
            "state": "already_closed_by_lifecycle",
            "next_action": "none",
        },
        {
            "record_type": "closed_measure",
            "family": "phase_2_human_repair_label",
            "count": phase_closed["phase_2_human_repair_label"],
            "state": "already_closed_by_lifecycle",
            "next_action": "none",
        },
        {
            "record_type": "closed_measure",
            "family": "phase_3_plain_light",
            "count": phase_closed["phase_3_plain_light"],
            "state": "already_closed_by_lifecycle",
            "next_action": "none",
        },
        {
            "record_type": "closed_measure",
            "family": "phase_3_es_suffix",
            "count": phase_closed["phase_3_es_suffix"],
            "state": "already_closed_by_lifecycle",
            "next_action": "none",
        },
        {
            "record_type": "remaining_closure_debt",
            "family": "phase_4_auto_confirmed_plain_or_light",
            "count": int(recommended_plan.get("phase_4_auto_confirmed_plain_or_light_bridge") or 0),
            "state": "candidate_for_future_readonly_review",
            "next_action": "review_source_confidence_allowlist_before_any_bridge",
        },
        {
            "record_type": "remaining_closure_debt",
            "family": "debug_existing_policy_consumption",
            "count": int(recommended_plan.get("debug_existing_policy_consumption") or 0),
            "state": "debug_readonly",
            "next_action": "debug_existing_policy_consumption_before_new_bridge",
        },
        {
            "record_type": "remaining_closure_debt",
            "family": "structural_holds_non_phase3",
            "count": int(architecture_decisions.get("architecture_hold_no_lifecycle_bridge_now") or 0),
            "state": "hold_structural_or_open_issue",
            "next_action": "hold_or_targeted_architecture_later",
        },
        {
            "record_type": "remaining_global",
            "family": "pending_apply_confirmed_real",
            "count": int(current_run.get("output_apply_pending_count") or 0),
            "state": "needs_output_apply_real",
            "next_action": "protected_apply_only_when_explicitly_requested",
        },
    ]
    for row in phase3_rows:
        if row.get("record_type") == "terminal_hold_detail":
            continue
        remaining_rows.append(
            {
                "record_type": "phase3_residual_classified",
                "family": row.get("family"),
                "count": int(row.get("count") or 0),
                "state": row.get("state"),
                "next_action": row.get("next_action"),
            }
        )

    type_counts = Counter()
    for row in remaining_rows:
        type_counts[str(row["record_type"])] += int(row["count"] or 0)

    recommendation = (
        "Next safest high-gain front is read-only debug_existing_policy_consumption (371), because it may unlock already-policy-allowed rows without touching output. "
        "After that, review phase_4_auto_confirmed_plain_or_light (118). Keep phase3 terminal holds and structural holds out of lifecycle."
    )
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_post_phase3_global_diagnostic",
        "from_run_id": args.from_run_id,
        "current_run_id": current_run_id,
        "current_run": {
            "id": current_run_id,
            "started_at": current_run.get("started_at"),
            "finished_at": current_run.get("finished_at"),
            "total_segments": int(current_run.get("total_segments") or 0),
            "closed_count": int(current_run.get("closed_count") or 0),
            "pending_count": int(current_run.get("pending_count") or 0),
            "reopen_count": int(current_run.get("reopen_count") or 0),
            "output_apply_pending_count": int(current_run.get("output_apply_pending_count") or 0),
        },
        "closure_debt_512_to_current": {
            "record_count": int(closure.get("record_count") or 0),
            "classification_counts": classification,
            "architecture_decision_counts": architecture_decisions,
        },
        "closed_by_phase": phase_closed,
        "closed_by_phase_total": phase_closed_total,
        "current_state_counts": current_state,
        "remaining_rows": remaining_rows,
        "remaining_record_type_counts": dict(type_counts.most_common()),
        "phase3_final": {
            "absorbed_catalog_only_total": int(phase3_final.get("absorbed_catalog_only_total") or 0),
            "terminal_hold_total": int(phase3_final.get("terminal_hold_total") or 0),
            "small_hold_total": int(phase3_final.get("small_hold_total") or 0),
        },
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": recommendation,
    }
    return remaining_rows, summary


def write_reports(rows: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_post_phase3_global_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Post-phase3 global diagnostic",
        f"current_run_id={summary['current_run_id']}",
        f"closed_count={summary['current_run']['closed_count']}",
        f"pending_count={summary['current_run']['pending_count']}",
        f"reopen_count={summary['current_run']['reopen_count']}",
        f"needs_output_apply={summary['current_run']['output_apply_pending_count']}",
        f"closure_debt_512_to_current={summary['closure_debt_512_to_current']['record_count']}",
        "",
        "Closed by phase:",
    ]
    for key, count in summary["closed_by_phase"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Remaining rows:"])
    for row in rows:
        lines.append(f"- {row['record_type']} | {row['family']}: {row['count']} -> {row['state']}")
    lines.extend(
        [
            "",
            "Guards:",
            "candidate_generation_count=0",
            "apply_count=0",
            "lifecycle_count=0",
            "segment_state_count=0",
            "reindex_count=0",
            "production_full_count=0",
            "source_changed=false",
            "output_changed=false",
            "",
            "Recommendation:",
            summary["single_operational_recommendation"],
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    rows, summary = build(args)
    txt, jsonl, summary_path = write_reports(rows, summary)
    print(f"txt={txt}")
    print(f"jsonl={jsonl}")
    print(f"summary={summary_path}")
    print(f"current_run_id={summary['current_run_id']}")
    print(f"pending_count={summary['current_run']['pending_count']}")
    print(f"closure_debt={summary['closure_debt_512_to_current']['record_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
