from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "training_retarget_plan_after_bridge_checkpoint_v1"
CURRENT_RUN_ID = 404
TRACKED_SEGMENT_ID = 281562
EXPECTED_FINAL_STATE = "closed_auto_confirmed_human_confirmed_post_apply_repair_lifecycle"


CLOSED_OR_EXHAUSTED_COHORTS = {
    "semantic_review_router_single_family",
    "semantic_review_router_plus_short_label_no_structural_overlap",
}
BLOCKED_HIGH_RISK_COHORTS = {
    "short_label_style_microagent_clean_no_dynamic_parser",
    "gender_token_microagent_plus_semantic_review_router",
    "dynamic_ck3_expression_microagent_plus_gender_token_microagent",
}
HOLD_LOW_YIELD_COHORTS = {
    "autofix_unknown_microagent_single_family",
    "autofix_unknown_microagent_plus_semantic_no_structural_overlap",
    "culture_semantic_microagent_single_family",
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def latest_file(pattern: str) -> Path | None:
    paths = sorted(reports_dir().glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return paths[0] if paths else None


def latest_files(pattern: str, limit: int = 20) -> list[Path]:
    return sorted(reports_dir().glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)[:limit]


def one(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


def run_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    row = one(conn, "SELECT * FROM segment_state_runs WHERE id = ?", (CURRENT_RUN_ID,))
    if not row:
        raise SystemExit(f"missing segment_state_run_id={CURRENT_RUN_ID}")
    return {
        "run_id": CURRENT_RUN_ID,
        "total_segments": int(row.get("total_segments") or 0),
        "closed_count": int(row.get("closed_count") or 0),
        "pending_count": int(row.get("pending_count") or 0),
        "output_apply_pending_count": int(row.get("output_apply_pending_count") or 0),
        "reopen_count": int(row.get("reopen_count") or 0),
        "finished_at": row.get("finished_at"),
    }


def tracked_segment(conn: sqlite3.Connection) -> dict[str, Any]:
    row = one(
        conn,
        """
        SELECT segment_id, relative_path, source_key, final_state, state_group,
               needs_human, needs_output_apply, confirmed_matches_output, needs_reopen,
               lifecycle_policy_action, lifecycle_policy_allowed
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id = ?
        """,
        (CURRENT_RUN_ID, TRACKED_SEGMENT_ID),
    )
    if not row:
        raise SystemExit(f"missing tracked segment {TRACKED_SEGMENT_ID} in run {CURRENT_RUN_ID}")
    return row


def checkpoint_snapshot() -> dict[str, Any]:
    path = latest_file("*training_resume_after_lifecycle_bridge_success_summary.json")
    if not path:
        return {"path": None, "available": False}
    data = read_json(path)
    return {"path": str(path), "available": True, "summary": data}


def preflight_snapshot() -> dict[str, Any]:
    path = latest_file("*candidate_generation_preflight_guard_summary.json")
    if not path:
        return {"path": None, "available": False}
    data = read_json(path)
    overblock_path = latest_file("*preflight_superseded_human_correction_audit_summary.json")
    overblock = read_json(overblock_path) if overblock_path else {}
    return {
        "path": str(path),
        "available": True,
        "blocked_segment_count": int(data.get("blocked_segment_count") or 0),
        "superseded_segment_count": int(data.get("superseded_segment_count") or 0),
        "context_risk_segment_count": int(data.get("context_risk_segment_count") or 0),
        "blocked_reason_counts": data.get("blocked_reason_counts", {}),
        "overblock_audit_path": str(overblock_path) if overblock_path else None,
        "potential_overblock_count": int(overblock.get("potential_overblock_count") or 0),
    }


def retarget_history() -> dict[str, Any]:
    retargets: list[dict[str, Any]] = []
    for path in latest_files("*candidate_discovery_retarget_with_preflight_summary.json", 10):
        data = read_json(path)
        retargets.append(
            {
                "path": str(path),
                "recommended_cohort_key": data.get("recommended_cohort_key"),
                "recommended_false_safe_risk": data.get("recommended_false_safe_risk"),
                "recommended_discovery_type": data.get("recommended_discovery_type"),
                "recommended_limit": data.get("recommended_limit"),
                "viable_cohort_count": data.get("viable_cohort_count"),
            }
        )
    discovery_zero: list[dict[str, Any]] = []
    for path in latest_files("*candidate_discovery_preflight_summary.json", 10):
        data = read_json(path)
        if int(data.get("candidate_count") or 0) == 0:
            discovery_zero.append(
                {
                    "path": str(path),
                    "cohort_key": data.get("cohort_key"),
                    "total_reviewed": data.get("total_reviewed"),
                    "candidate_count": data.get("candidate_count"),
                }
            )
    return {"recent_retargets": retargets, "zero_candidate_discoveries": discovery_zero}


def candidate_backlog_snapshot() -> dict[str, Any]:
    path = latest_file("*candidate_human_review_decision_record_summary.json")
    closeout_path = latest_file("*post_apply_candidate_closeout_status_summary.json")
    data = read_json(path) if path else {}
    closeout = read_json(closeout_path) if closeout_path else {}
    return {
        "human_decision_summary_path": str(path) if path else None,
        "closeout_summary_path": str(closeout_path) if closeout_path else None,
        "approved_for_future_apply": int(data.get("approve_for_future_apply_count") or 0),
        "needs_more_context": int(data.get("needs_more_context_count") or 0),
        "duplicates": int(data.get("duplicate_of_existing_candidate_count") or 0),
        "rejected": int(data.get("reject_count") or 0),
        "closeout_status_counts": closeout.get("status_counts", {}),
    }


def build_summary() -> dict[str, Any]:
    with connect_readonly() as conn:
        run = run_snapshot(conn)
        segment = tracked_segment(conn)
    checkpoint = checkpoint_snapshot()
    preflight = preflight_snapshot()
    history = retarget_history()
    backlog = candidate_backlog_snapshot()

    checkpoint_summary = checkpoint.get("summary", {})
    checkpoint_still_valid = bool(
        checkpoint.get("available")
        and checkpoint_summary.get("architecture_bridge_validated") is True
        and checkpoint_summary.get("segment_281562_closed") is True
        and int(checkpoint_summary.get("needs_output_apply_total") or 0) == 0
        and checkpoint_summary.get("local_learning_consistent") is True
    )
    segment_not_in_backlog = (
        segment.get("final_state") == EXPECTED_FINAL_STATE
        and segment.get("state_group") == "closed"
        and int(segment.get("needs_reopen") or 0) == 0
        and int(segment.get("needs_human") or 0) == 0
    )
    overblock_risk = int(preflight.get("potential_overblock_count") or 0) > 0
    gates = {
        "checkpoint_still_valid": checkpoint_still_valid,
        "segment_281562_not_in_backlog": segment_not_in_backlog,
        "needs_output_apply_total": run["output_apply_pending_count"],
        "local_learning_consistent": bool(checkpoint_summary.get("local_learning_consistent") is True),
        "preflight_available": bool(preflight.get("available")),
        "overblock_risk_detected": overblock_risk,
        "high_risk_discovery_allowed": False,
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "ready_for_user_to_approve_readonly_retarget": bool(
            checkpoint_still_valid
            and segment_not_in_backlog
            and run["output_apply_pending_count"] == 0
            and preflight.get("available")
            and not overblock_risk
        ),
        "retarget_executed": False,
        "discovery_executed": False,
    }
    excluded_cohorts = sorted(CLOSED_OR_EXHAUSTED_COHORTS | BLOCKED_HIGH_RISK_COHORTS | HOLD_LOW_YIELD_COHORTS)
    plan = {
        "allowed_next_step_after_user_confirmation": "run_readonly_retarget_only",
        "not_allowed_in_next_step": [
            "production_full",
            "apply",
            "lifecycle",
            "reindex",
            "segment_state",
            "issue_ledger",
            "confirmations",
            "discovery",
            "human_audit_packet",
        ],
        "excluded_cohorts": excluded_cohorts,
        "required_retarget_exclusions": [
            "preflight_blocked_segments",
            "preflight_superseded_by_human_correction_segments",
            "needs_more_context_without_new_explicit_approval",
            "semantic_error",
            "duplicates",
            "context_risk_patterns",
            "closed_or_exhausted_cohorts",
            "high_risk_cohorts",
        ],
        "stop_conditions": [
            "recommended_cohort_false_safe_risk_high",
            "candidate_count_zero_in_measured_discovery",
            "architecture_gate_failure",
            "needs_output_apply_nonzero",
        ],
    }
    if gates["ready_for_user_to_approve_readonly_retarget"]:
        recommendation = (
            "Recomendar ao usuario o proximo ciclo: rodar apenas retarget read-only com as exclusoes consolidadas. "
            "Nao executar discovery no mesmo ciclo."
        )
        next_prompt = "chat_exec_training_retarget_readonly_after_bridge_checkpoint_prompt.md"
        architecture_needed = False
    else:
        recommendation = "Manter retarget/discovery/apply em hold e investigar o gate especifico."
        next_prompt = "chat_exec_training_retarget_plan_gate_failure_review_prompt.md"
        architecture_needed = True
    return {
        "schema_version": 1,
        "source": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "current_run": run,
        "segment_281562": segment,
        "checkpoint": checkpoint,
        "preflight": preflight,
        "retarget_history": history,
        "human_candidate_backlog": backlog,
        "plan": plan,
        "architecture_needed_now": architecture_needed,
        "recommendation": recommendation,
        "next_prompt_recommended": next_prompt,
        "confirmed_not_run": {
            "production_full": True,
            "apply": True,
            "lifecycle": True,
            "reindex": True,
            "segment_state": True,
            "issue_ledger": True,
            "confirmations": True,
            "retarget": True,
            "discovery": True,
            "human_audit_packet": True,
        },
        **gates,
    }


def write_outputs(summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_training_retarget_plan_after_bridge_checkpoint"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    records = [
        {"record_type": "gates", **{key: summary[key] for key in [
            "checkpoint_still_valid",
            "segment_281562_not_in_backlog",
            "needs_output_apply_total",
            "local_learning_consistent",
            "preflight_available",
            "overblock_risk_detected",
            "high_risk_discovery_allowed",
            "ready_for_user_to_approve_readonly_retarget",
            "retarget_executed",
            "discovery_executed",
        ]}},
        {"record_type": "plan", **summary["plan"]},
        {"record_type": "preflight", **summary["preflight"]},
        {"record_type": "retarget_history", **summary["retarget_history"]},
    ]
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "training retarget plan after bridge checkpoint",
        f"source={RULE_VERSION}",
        "",
        "gates:",
        f"- checkpoint_still_valid={str(summary['checkpoint_still_valid']).lower()}",
        f"- segment_281562_not_in_backlog={str(summary['segment_281562_not_in_backlog']).lower()}",
        f"- needs_output_apply_total={summary['needs_output_apply_total']}",
        f"- local_learning_consistent={str(summary['local_learning_consistent']).lower()}",
        f"- preflight_available={str(summary['preflight_available']).lower()}",
        f"- overblock_risk_detected={str(summary['overblock_risk_detected']).lower()}",
        f"- high_risk_discovery_allowed={str(summary['high_risk_discovery_allowed']).lower()}",
        f"- apply_ready_now={summary['apply_ready_now']}",
        f"- production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
        f"- ready_for_user_to_approve_readonly_retarget={str(summary['ready_for_user_to_approve_readonly_retarget']).lower()}",
        f"- retarget_executed={str(summary['retarget_executed']).lower()}",
        f"- discovery_executed={str(summary['discovery_executed']).lower()}",
        "",
        "current_state:",
        f"- closed={summary['current_run']['closed_count']}",
        f"- pending={summary['current_run']['pending_count']}",
        f"- reopen={summary['current_run']['reopen_count']}",
        f"- needs_output_apply={summary['current_run']['output_apply_pending_count']}",
        f"- segment_281562={summary['segment_281562']['state_group']} / {summary['segment_281562']['final_state']}",
        "",
        "preflight:",
        f"- blocked_segment_count={summary['preflight'].get('blocked_segment_count')}",
        f"- superseded_segment_count={summary['preflight'].get('superseded_segment_count')}",
        f"- context_risk_segment_count={summary['preflight'].get('context_risk_segment_count')}",
        f"- potential_overblock_count={summary['preflight'].get('potential_overblock_count')}",
        "",
        "excluded_cohorts:",
        *[f"- {cohort}" for cohort in summary["plan"]["excluded_cohorts"]],
        "",
        f"architecture_needed_now={str(summary['architecture_needed_now']).lower()}",
        f"recommendation={summary['recommendation']}",
        f"next_prompt_recommended={summary['next_prompt_recommended']}",
        "",
        "confirmed_not_run=production_full, apply, lifecycle, reindex, segment-state, issue-ledger, confirmations, retarget, discovery, human_audit_packet",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    summary = build_summary()
    txt_path, jsonl_path, summary_path = write_outputs(summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    for key in [
        "checkpoint_still_valid",
        "segment_281562_not_in_backlog",
        "needs_output_apply_total",
        "local_learning_consistent",
        "preflight_available",
        "overblock_risk_detected",
        "ready_for_user_to_approve_readonly_retarget",
        "retarget_executed",
        "discovery_executed",
        "architecture_needed_now",
        "next_prompt_recommended",
    ]:
        print(f"{key}={summary[key]}")


if __name__ == "__main__":
    main()
