from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


EXPECTED_SEGMENT_STATE_RUN_ID = 400
EXPECTED_LEDGER_RUN_ID = 76


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


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def latest_file(pattern: str) -> Path | None:
    paths = sorted(reports_dir().glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return paths[0] if paths else None


def all_recent(pattern: str, limit: int = 20) -> list[Path]:
    return sorted(reports_dir().glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)[:limit]


def state_snapshot(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    run = conn.execute("SELECT * FROM segment_state_runs WHERE id = ?", (run_id,)).fetchone()
    if not run:
        raise SystemExit(f"missing segment_state_run_id={run_id}")
    grouped = {
        row["state_group"]: int(row["count"])
        for row in conn.execute(
            """
            SELECT state_group, COUNT(*) AS count
            FROM segment_state_items
            WHERE run_id = ?
            GROUP BY state_group
            """,
            (run_id,),
        )
    }
    human = conn.execute(
        """
        SELECT
            COUNT(*) AS pending_human,
            SUM(CASE WHEN needs_output_apply = 1 THEN 1 ELSE 0 END) AS needs_output_apply,
            SUM(CASE WHEN confirmed_matches_output = 0 THEN 1 ELSE 0 END) AS output_mismatch,
            SUM(CASE WHEN lifecycle_policy_allowed = 1 THEN 1 ELSE 0 END) AS lifecycle_allowed
        FROM segment_state_items
        WHERE run_id = ?
          AND state_group = 'pending'
        """,
        (run_id,),
    ).fetchone()
    return {
        "run_id": int(run["id"]),
        "finished_at": run["finished_at"],
        "total_segments": int(run["total_segments"] or 0),
        "closed_count": int(run["closed_count"] or 0),
        "pending_count": int(run["pending_count"] or 0),
        "output_apply_pending_count": int(run["output_apply_pending_count"] or 0),
        "reopen_count": int(run["reopen_count"] or 0),
        "state_group_counts": grouped,
        "pending_human_remaining": int(human["pending_human"] or 0),
        "pending_needs_output_apply": int(human["needs_output_apply"] or 0),
        "pending_output_mismatch": int(human["output_mismatch"] or 0),
        "pending_lifecycle_allowed": int(human["lifecycle_allowed"] or 0),
    }


def issue_snapshot(conn: sqlite3.Connection, ledger_run_id: int) -> dict[str, Any]:
    top = [
        {"issue_family": row["issue_family"], "open_count": int(row["count"])}
        for row in conn.execute(
            """
            SELECT issue_family, COUNT(*) AS count
            FROM ml_issue_ledger_items
            WHERE run_id = ?
              AND status = 'open'
            GROUP BY issue_family
            ORDER BY count DESC
            LIMIT 12
            """,
            (ledger_run_id,),
        )
    ]
    total = conn.execute(
        "SELECT COUNT(*) FROM ml_issue_ledger_items WHERE run_id = ? AND status = 'open'",
        (ledger_run_id,),
    ).fetchone()[0]
    return {"ledger_run_id": ledger_run_id, "open_issue_total": int(total or 0), "top_open_families": top}


def learning_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    latest_run = conn.execute("SELECT * FROM local_learning_runs ORDER BY id DESC LIMIT 1").fetchone()
    rows = conn.execute(
        """
        SELECT
            queue_source,
            origin,
            human_label,
            COUNT(*) AS count
        FROM local_learning_candidates
        GROUP BY queue_source, origin, human_label
        ORDER BY count DESC
        """
    ).fetchall()
    post_apply = conn.execute(
        """
        SELECT
            COUNT(*) AS count,
            SUM(CASE WHEN human_label = 'correct' THEN 1 ELSE 0 END) AS correct_count,
            SUM(CASE WHEN human_label = 'semantic_error' THEN 1 ELSE 0 END) AS semantic_error_count,
            MAX(updated_at) AS last_updated_at
        FROM local_learning_candidates
        WHERE queue_source = 'post-apply-protected'
           OR origin = 'candidate_apply_protected'
        """
    ).fetchone()
    return {
        "latest_learning_run": dict(latest_run) if latest_run else None,
        "post_apply_learning_rows": int(post_apply["count"] or 0),
        "post_apply_learning_correct_rows": int(post_apply["correct_count"] or 0),
        "post_apply_learning_semantic_error_rows": int(post_apply["semantic_error_count"] or 0),
        "post_apply_learning_last_updated_at": post_apply["last_updated_at"],
        "learning_distribution": [dict(row) for row in rows],
    }


def confirmation_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT confirmation_source, confirmation_label, locked, COUNT(*) AS count
            FROM segment_confirmations
            GROUP BY confirmation_source, confirmation_label, locked
            ORDER BY count DESC
            LIMIT 15
            """
        )
    ]
    recent = [
        dict(row)
        for row in conn.execute(
            """
            SELECT segment_id, confirmation_source, confirmation_label, locked, candidate_id, updated_at
            FROM segment_confirmations
            ORDER BY updated_at DESC
            LIMIT 15
            """
        )
    ]
    return {"confirmation_distribution": rows, "recent_confirmations": recent}


def applied_since_last_lifecycle(conn: sqlite3.Connection) -> dict[str, Any]:
    latest_state = conn.execute("SELECT MAX(finished_at) FROM segment_state_runs").fetchone()[0]
    db_applies = [
        dict(row)
        for row in conn.execute(
            """
            SELECT id, state_run_id, apply, applied_count, files_touched_count, report_path, finished_at
            FROM segment_output_apply_runs
            WHERE apply = 1
              AND (? IS NULL OR finished_at >= ?)
            ORDER BY id DESC
            """,
            (latest_state, latest_state),
        )
    ]
    report_applies: list[dict[str, Any]] = []
    for path in all_recent("*apply_summary.json", limit=30):
        try:
            data = read_json(path)
        except Exception:
            continue
        applied = data.get("applied_count", data.get("applied"))
        if applied in (None, 0, False):
            continue
        report_applies.append(
            {
                "path": str(path),
                "applied_count": int(applied) if isinstance(applied, int) else 1,
                "post_validation_ok": data.get("post_validation_ok"),
                "post_validation_fail_count": data.get("post_validation_fail_count"),
                "source": data.get("source"),
            }
        )
    return {
        "latest_segment_state_finished_at": latest_state,
        "db_output_apply_runs_since_latest_state": db_applies,
        "protected_apply_reports_recent": report_applies,
        "protected_apply_report_count_recent": len(report_applies),
    }


def cohort_decisions() -> dict[str, Any]:
    closed: set[str] = set()
    blocked: set[str] = set()
    hold: set[str] = set()
    exhausted: set[str] = set()
    notes: list[str] = []

    closeout_path = latest_file("*post_apply_candidate_closeout_status_summary.json")
    if closeout_path:
        closeout = read_json(closeout_path)
        if closeout.get("known_candidates_closed_out") is True:
            closed.update(
                {
                    "semantic_review_router_plus_short_label_no_structural_overlap",
                    "semantic_review_router_single_family",
                }
            )
            notes.append(f"known applied candidates closed by {closeout_path.name}")

    dynamic_micro_path = latest_file("*dynamic_gender_micro_audit_summary.json")
    if dynamic_micro_path:
        dynamic = read_json(dynamic_micro_path)
        if dynamic.get("safe_for_future_apply_batch_count") == 0:
            blocked.add("dynamic_ck3_expression_microagent_plus_gender_token_microagent")
            hold.add("dynamic_gender_policy_design")
            notes.append(f"dynamic/gender hold by {dynamic_micro_path.name}")

    retarget_path = latest_file("*candidate_discovery_cohort_retarget_summary.json")
    if retarget_path:
        retarget = read_json(retarget_path)
        if retarget.get("recommended_cohort_key") == "short_label_style_microagent_clean_no_dynamic_parser":
            blocked.add("short_label_style_microagent_clean_no_dynamic_parser")
            notes.append("short_label clean retarget had high structural overlap/false-safe risk")

    single_family_path = latest_file("*autofix_unknown_single_family_review_post_training_low_risk_full.txt")
    if single_family_path:
        exhausted.add("autofix_unknown_microagent_single_family_apply")
        notes.append(f"single-family autofix review produced no apply candidates: {single_family_path.name}")

    dynamic_split_path = latest_file("*autofix_unknown_single_dynamic_batch2_sublane_review.txt")
    if dynamic_split_path:
        hold.add("single_dynamic_domain_context")
        hold.add("single_dynamic_requirement_tooltip")
        hold.add("single_dynamic_concept_expression")
        notes.append(f"dynamic split requires subpolicy work: {dynamic_split_path.name}")

    return {
        "closed_or_exhausted_cohorts": sorted(closed | exhausted),
        "closed_cohorts": sorted(closed),
        "exhausted_cohorts": sorted(exhausted),
        "blocked_or_hold_cohorts": sorted(blocked | hold),
        "blocked_cohorts": sorted(blocked),
        "hold_topics": sorted(hold),
        "retarget_exclude_cohorts": sorted(closed | blocked),
        "decision_notes": notes,
    }


def human_pending_snapshot(conn: sqlite3.Connection, segment_state_run_id: int) -> dict[str, Any]:
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT review_state, final_state, COUNT(*) AS count
            FROM segment_state_items
            WHERE run_id = ?
              AND state_group = 'pending'
              AND needs_human = 1
            GROUP BY review_state, final_state
            ORDER BY count DESC
            LIMIT 20
            """,
            (segment_state_run_id,),
        )
    ]
    total = sum(int(row["count"] or 0) for row in rows)
    return {"pending_human_grouped_sample_total": total, "pending_human_top_states": rows}


def lifecycle_decision(summary: dict[str, Any]) -> dict[str, Any]:
    apply_reports = summary["applied_since_last_lifecycle"]["protected_apply_reports_recent"]
    post_validation_ok = all(
        row.get("post_validation_ok") in (True, None) and row.get("post_validation_fail_count") in (0, None)
        for row in apply_reports
    )
    closed_out = bool(summary["cohort_decisions"]["closed_cohorts"])
    if apply_reports and post_validation_ok and closed_out:
        recommendation = "eligible_for_separate_lifecycle_reindex_planning_prompt"
        rationale = "protected applies/corrections exist and recent closeout is clean, but lifecycle must run in a separate prompt"
    else:
        recommendation = "hold_lifecycle_reindex"
        rationale = "no separate lifecycle prompt should run until apply/correction closure and report guards are explicitly selected"
    return {
        "recommended_decision": recommendation,
        "rationale": rationale,
        "run_now": False,
        "production_full_recommended_now": False,
    }


def write_outputs(summary: dict[str, Any]) -> tuple[Path, Path]:
    base = reports_dir() / f"{stamp()}_post_learning_apply_readonly_status"
    summary_path = reports_dir() / f"{base.name}_summary.json"
    txt_path = base.with_suffix(".txt")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Post-learning/post-apply read-only status",
        f"segment_state_run_id={summary['segment_state']['run_id']}",
        f"ledger_run_id={summary['issues']['ledger_run_id']}",
        "",
        f"closed_or_exhausted_cohorts={summary['cohort_decisions']['closed_or_exhausted_cohorts']}",
        f"blocked_or_hold_cohorts={summary['cohort_decisions']['blocked_or_hold_cohorts']}",
        f"retarget_exclude_cohorts={summary['cohort_decisions']['retarget_exclude_cohorts']}",
        "",
        f"protected_apply_report_count_recent={summary['applied_since_last_lifecycle']['protected_apply_report_count_recent']}",
        f"post_apply_learning_rows={summary['learning']['post_apply_learning_rows']}",
        f"post_apply_learning_correct_rows={summary['learning']['post_apply_learning_correct_rows']}",
        f"post_apply_learning_semantic_error_rows={summary['learning']['post_apply_learning_semantic_error_rows']}",
        f"pending_human_remaining={summary['segment_state']['pending_human_remaining']}",
        "",
        f"lifecycle_reindex_decision={summary['lifecycle_reindex_decision']['recommended_decision']}",
        f"lifecycle_reindex_run_now={summary['lifecycle_reindex_decision']['run_now']}",
        f"production_full_recommended_now={summary['lifecycle_reindex_decision']['production_full_recommended_now']}",
        "",
        "next_action=run_retarget_read_only_with_exclusions_no_apply",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-state-run-id", type=int, default=EXPECTED_SEGMENT_STATE_RUN_ID)
    parser.add_argument("--ledger-run-id", type=int, default=EXPECTED_LEDGER_RUN_ID)
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")
    if args.ledger_run_id != EXPECTED_LEDGER_RUN_ID:
        raise SystemExit("ledger_run_id guard failed")

    with connect_readonly() as conn:
        summary: dict[str, Any] = {
            "schema_version": 1,
            "source": "post_learning_apply_readonly_status_v1",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "segment_state": state_snapshot(conn, args.segment_state_run_id),
            "issues": issue_snapshot(conn, args.ledger_run_id),
            "learning": learning_snapshot(conn),
            "confirmations": confirmation_snapshot(conn),
            "applied_since_last_lifecycle": applied_since_last_lifecycle(conn),
            "cohort_decisions": cohort_decisions(),
            "human_pending": human_pending_snapshot(conn, args.segment_state_run_id),
        }
    summary["lifecycle_reindex_decision"] = lifecycle_decision(summary)
    txt_path, summary_path = write_outputs(summary)
    print(f"txt={txt_path}")
    print(f"summary={summary_path}")
    print(f"closed_or_exhausted_cohorts={summary['cohort_decisions']['closed_or_exhausted_cohorts']}")
    print(f"blocked_or_hold_cohorts={summary['cohort_decisions']['blocked_or_hold_cohorts']}")
    print(f"retarget_exclude_cohorts={summary['cohort_decisions']['retarget_exclude_cohorts']}")
    print(f"protected_apply_report_count_recent={summary['applied_since_last_lifecycle']['protected_apply_report_count_recent']}")
    print(f"post_apply_learning_rows={summary['learning']['post_apply_learning_rows']}")
    print(f"pending_human_remaining={summary['segment_state']['pending_human_remaining']}")
    print(f"lifecycle_reindex_decision={summary['lifecycle_reindex_decision']['recommended_decision']}")
    print("next_action=run_retarget_read_only_with_exclusions_no_apply")


if __name__ == "__main__":
    main()
