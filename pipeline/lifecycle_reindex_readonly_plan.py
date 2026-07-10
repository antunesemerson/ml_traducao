from __future__ import annotations

import argparse
import json
import sqlite3
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


def latest(pattern: str) -> Path | None:
    paths = sorted(reports_dir().glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return paths[0] if paths else None


def recent(pattern: str, limit: int = 20) -> list[Path]:
    return sorted(reports_dir().glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)[:limit]


def state_stats(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    run = conn.execute("SELECT * FROM segment_state_runs WHERE id = ?", (run_id,)).fetchone()
    if not run:
        raise SystemExit(f"missing segment_state_run_id={run_id}")
    pending = conn.execute(
        """
        SELECT
            COUNT(*) AS pending_total,
            SUM(CASE WHEN needs_human = 1 THEN 1 ELSE 0 END) AS needs_human,
            SUM(CASE WHEN needs_output_apply = 1 THEN 1 ELSE 0 END) AS needs_output_apply,
            SUM(CASE WHEN confirmed_matches_output = 0 THEN 1 ELSE 0 END) AS output_mismatch,
            SUM(CASE WHEN lifecycle_policy_allowed = 1 THEN 1 ELSE 0 END) AS lifecycle_allowed
        FROM segment_state_items
        WHERE run_id = ?
          AND state_group = 'pending'
        """,
        (run_id,),
    ).fetchone()
    groups = [
        dict(row)
        for row in conn.execute(
            """
            SELECT state_group, final_state, COUNT(*) AS count
            FROM segment_state_items
            WHERE run_id = ?
            GROUP BY state_group, final_state
            ORDER BY count DESC
            LIMIT 20
            """,
            (run_id,),
        )
    ]
    return {
        "run_id": int(run["id"]),
        "finished_at": run["finished_at"],
        "total_segments": int(run["total_segments"] or 0),
        "closed_count": int(run["closed_count"] or 0),
        "pending_count": int(run["pending_count"] or 0),
        "output_apply_pending_count": int(run["output_apply_pending_count"] or 0),
        "reopen_count": int(run["reopen_count"] or 0),
        "pending_total": int(pending["pending_total"] or 0),
        "pending_needs_human": int(pending["needs_human"] or 0),
        "pending_needs_output_apply": int(pending["needs_output_apply"] or 0),
        "pending_output_mismatch": int(pending["output_mismatch"] or 0),
        "pending_lifecycle_allowed": int(pending["lifecycle_allowed"] or 0),
        "top_state_groups": groups,
    }


def issue_stats(conn: sqlite3.Connection, ledger_run_id: int) -> dict[str, Any]:
    top = [
        dict(row)
        for row in conn.execute(
            """
            SELECT issue_family, COUNT(*) AS count
            FROM ml_issue_ledger_items
            WHERE run_id = ?
              AND status = 'open'
            GROUP BY issue_family
            ORDER BY count DESC
            LIMIT 15
            """,
            (ledger_run_id,),
        )
    ]
    total = conn.execute(
        "SELECT COUNT(*) FROM ml_issue_ledger_items WHERE run_id = ? AND status = 'open'",
        (ledger_run_id,),
    ).fetchone()[0]
    return {"ledger_run_id": ledger_run_id, "open_issue_total": int(total or 0), "top_open_issue_families": top}


def learning_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN human_label = 'correct' THEN 1 ELSE 0 END) AS correct,
            SUM(CASE WHEN human_label = 'semantic_error' THEN 1 ELSE 0 END) AS semantic_error,
            MAX(updated_at) AS last_updated_at
        FROM local_learning_candidates
        WHERE queue_source = 'post-apply-protected'
           OR origin = 'candidate_apply_protected'
        """
    ).fetchone()
    return {
        "post_apply_learning_rows": int(row["total"] or 0),
        "post_apply_learning_correct_rows": int(row["correct"] or 0),
        "post_apply_learning_semantic_error_rows": int(row["semantic_error"] or 0),
        "post_apply_learning_last_updated_at": row["last_updated_at"],
    }


def apply_report_stats() -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    for path in recent("*apply_summary.json", limit=50):
        try:
            data = read_json(path)
        except Exception:
            continue
        applied = data.get("applied_count", data.get("applied"))
        if applied in (None, 0, False):
            continue
        jsonl_path = Path(str(path).replace("_summary.json", ".jsonl"))
        legacy_snapshot_paths: list[str] = []
        if jsonl_path.exists():
            with jsonl_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    snapshot_path = row.get("snapshot_path") or row.get("rollback_path")
                    if snapshot_path:
                        legacy_snapshot_paths.append(str(snapshot_path))
        has_summary_rollback = bool(data.get("rollback_path_documented")) or bool(data.get("snapshot_dir"))
        has_legacy_rollback = bool(legacy_snapshot_paths) or int(data.get("snapshot_count") or 0) > 0
        post_validation_fail_count = int(data.get("post_validation_fail_count") or 0)
        if data.get("post_apply_validation_ok") is False or data.get("post_validation_ok") is False:
            post_validation_fail_count += 1
        reports.append(
            {
                "path": str(path),
                "applied_count": int(applied) if isinstance(applied, int) else 1,
                "guard_fail_count": int(data.get("guard_fail_count") or 0),
                "post_validation_fail_count": post_validation_fail_count,
                "rollback_path_documented": has_summary_rollback or has_legacy_rollback,
                "snapshot_dir": data.get("snapshot_dir", ""),
                "legacy_snapshot_path_count": len(legacy_snapshot_paths),
                "production_full_recommended_now": bool(data.get("production_full_recommended_now")),
            }
        )
    return {
        "recent_protected_apply_reports": reports,
        "recent_protected_apply_report_count": len(reports),
        "recent_applied_count_total": sum(row["applied_count"] for row in reports),
        "recent_guard_fail_count_total": sum(row["guard_fail_count"] for row in reports),
        "recent_post_validation_fail_count_total": sum(row["post_validation_fail_count"] for row in reports),
        "all_recent_have_rollback_path": all(row["rollback_path_documented"] for row in reports) if reports else False,
    }


def status_inputs() -> dict[str, Any]:
    status_path = latest("*post_learning_apply_readonly_status_summary.json")
    if not status_path:
        return {"latest_status_summary": None}
    data = read_json(status_path)
    return {
        "latest_status_summary": str(status_path),
        "status_lifecycle_decision": data.get("lifecycle_reindex_decision", {}),
        "status_cohort_decisions": data.get("cohort_decisions", {}),
    }


def build_plan(summary: dict[str, Any]) -> dict[str, Any]:
    applies = summary["apply_reports"]
    state = summary["segment_state"]
    hard_blocks: list[str] = []
    if applies["recent_protected_apply_report_count"] == 0:
        hard_blocks.append("no_recent_protected_apply_reports")
    if applies["recent_guard_fail_count_total"] != 0:
        hard_blocks.append("guard_failures_present")
    if applies["recent_post_validation_fail_count_total"] != 0:
        hard_blocks.append("post_validation_failures_present")
    if not applies["all_recent_have_rollback_path"]:
        hard_blocks.append("not_all_recent_apply_reports_have_rollback_path")
    if state["pending_needs_output_apply"] != 0:
        hard_blocks.append("current_segment_state_already_has_output_apply_pending")

    eligible = not hard_blocks
    return {
        "eligible_for_controlled_lifecycle_reindex": eligible,
        "hard_blocks": hard_blocks,
        "run_now": False,
        "production_full_recommended_now": False,
        "recommended_order": [
            "1. Confirm no new apply will be mixed into this lifecycle/reindex cycle.",
            "2. Preserve reports, apply JSONL, summaries, diff previews, and snapshot directories.",
            "3. Run index/reindex only if explicitly approved for this separate cycle.",
            "4. Run inline/index companion stage only if the project command would normally pair it with index.",
            "5. Run segment-state after reindex to create a new state snapshot.",
            "6. Compare old segment_state_run_id=400 with the new segment-state run: closed/pending/reopen/output_apply deltas.",
            "7. Do not run production full from chat; if full integration is needed, ask the user to run it via GUI.",
        ],
        "candidate_commands_for_user_review": [
            "python pipeline\\main.py segment-state",
            "python pipeline\\main.py cycle --force-index",
        ],
        "preferred_next_prompt": "explicit_lifecycle_reindex_execution_or_dry_run_prompt",
        "decision": "ready_to_request_explicit_lifecycle_reindex_prompt" if eligible else "hold_lifecycle_reindex_until_blocks_resolved",
    }


def write_outputs(summary: dict[str, Any]) -> tuple[Path, Path]:
    base = reports_dir() / f"{stamp()}_lifecycle_reindex_readonly_plan"
    txt_path = base.with_suffix(".txt")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    plan = summary["plan"]
    lines = [
        "Lifecycle/reindex read-only plan",
        f"segment_state_run_id={summary['segment_state']['run_id']}",
        f"ledger_run_id={summary['issues']['ledger_run_id']}",
        "",
        "Execution stats:",
        f"- recent_protected_apply_report_count: {summary['apply_reports']['recent_protected_apply_report_count']}",
        f"- recent_applied_count_total: {summary['apply_reports']['recent_applied_count_total']}",
        f"- recent_guard_fail_count_total: {summary['apply_reports']['recent_guard_fail_count_total']}",
        f"- recent_post_validation_fail_count_total: {summary['apply_reports']['recent_post_validation_fail_count_total']}",
        f"- post_apply_learning_rows: {summary['learning']['post_apply_learning_rows']}",
        f"- post_apply_learning_correct_rows: {summary['learning']['post_apply_learning_correct_rows']}",
        f"- post_apply_learning_semantic_error_rows: {summary['learning']['post_apply_learning_semantic_error_rows']}",
        "",
        "Remaining pendencies:",
        f"- pending_total: {summary['segment_state']['pending_total']}",
        f"- pending_needs_human: {summary['segment_state']['pending_needs_human']}",
        f"- pending_needs_output_apply: {summary['segment_state']['pending_needs_output_apply']}",
        f"- pending_output_mismatch: {summary['segment_state']['pending_output_mismatch']}",
        f"- open_issue_total: {summary['issues']['open_issue_total']}",
        "",
        f"eligible_for_controlled_lifecycle_reindex={plan['eligible_for_controlled_lifecycle_reindex']}",
        f"hard_blocks={plan['hard_blocks']}",
        f"run_now={plan['run_now']}",
        f"production_full_recommended_now={plan['production_full_recommended_now']}",
        f"decision={plan['decision']}",
        "",
        "Recommended order:",
        *plan["recommended_order"],
        "",
        "Candidate commands for review only:",
        *[f"- {command}" for command in plan["candidate_commands_for_user_review"]],
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
            "source": "lifecycle_reindex_readonly_plan_v1",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "segment_state": state_stats(conn, args.segment_state_run_id),
            "issues": issue_stats(conn, args.ledger_run_id),
            "learning": learning_stats(conn),
            "apply_reports": apply_report_stats(),
            "status_inputs": status_inputs(),
        }
    summary["plan"] = build_plan(summary)
    txt_path, summary_path = write_outputs(summary)
    print(f"txt={txt_path}")
    print(f"summary={summary_path}")
    print(f"recent_protected_apply_report_count={summary['apply_reports']['recent_protected_apply_report_count']}")
    print(f"recent_applied_count_total={summary['apply_reports']['recent_applied_count_total']}")
    print(f"post_apply_learning_rows={summary['learning']['post_apply_learning_rows']}")
    print(f"pending_total={summary['segment_state']['pending_total']}")
    print(f"pending_needs_human={summary['segment_state']['pending_needs_human']}")
    print(f"open_issue_total={summary['issues']['open_issue_total']}")
    print(f"eligible_for_controlled_lifecycle_reindex={summary['plan']['eligible_for_controlled_lifecycle_reindex']}")
    print(f"decision={summary['plan']['decision']}")


if __name__ == "__main__":
    main()
