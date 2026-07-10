from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "training_resume_after_lifecycle_bridge_success_v1"
CURRENT_RUN_ID = 404
PREVIOUS_RUN_ID = 403
TRACKED_SEGMENT_ID = 281562
POLICY_RUN_ID = 6
POLICY_NAME = "human_confirmed_post_apply_repair_lifecycle_bridge"
POLICY_ACTION = "close_reopen_human_confirmed_post_apply_repair_lifecycle"
EXPECTED_FINAL_STATE = "closed_auto_confirmed_human_confirmed_post_apply_repair_lifecycle"


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


def one(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


def all_rows(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def run_snapshot(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = one(conn, "SELECT * FROM segment_state_runs WHERE id = ?", (run_id,))
    if not row:
        raise SystemExit(f"missing segment_state_run_id={run_id}")
    return {
        "run_id": run_id,
        "total_segments": int(row.get("total_segments") or 0),
        "closed_count": int(row.get("closed_count") or 0),
        "pending_count": int(row.get("pending_count") or 0),
        "output_apply_pending_count": int(row.get("output_apply_pending_count") or 0),
        "reopen_count": int(row.get("reopen_count") or 0),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
    }


def segment_state(conn: sqlite3.Connection, run_id: int, segment_id: int) -> dict[str, Any]:
    row = one(
        conn,
        """
        SELECT segment_id, relative_path, source_key, final_state, state_group,
               review_state, apply_state, needs_human, needs_output_apply,
               confirmed_matches_output, needs_reopen, lifecycle_policy_action,
               lifecycle_policy_allowed, reasons_json
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id = ?
        """,
        (run_id, segment_id),
    )
    if not row:
        raise SystemExit(f"missing segment_state_item run_id={run_id} segment_id={segment_id}")
    return row


def policy_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    run = one(
        conn,
        """
        SELECT *
        FROM auto_confirmation_reopen_lifecycle_policy_runs
        WHERE id = ?
        """,
        (POLICY_RUN_ID,),
    )
    if not run:
        raise SystemExit(f"missing policy_run_id={POLICY_RUN_ID}")
    items = all_rows(
        conn,
        """
        SELECT item.*, state.final_state, state.state_group, state.needs_output_apply,
               state.lifecycle_policy_action, state.lifecycle_policy_allowed
        FROM auto_confirmation_reopen_lifecycle_policy_items item
        LEFT JOIN segment_state_items state
          ON state.segment_id = item.segment_id
         AND state.run_id = ?
        WHERE item.run_id = ?
        ORDER BY item.segment_id
        """,
        (CURRENT_RUN_ID, POLICY_RUN_ID),
    )
    unclosed = [
        row
        for row in items
        if int(row.get("policy_allowed") or 0) == 1
        and row.get("final_state") != EXPECTED_FINAL_STATE
    ]
    return {
        "run": run,
        "items": items,
        "released_item_count": sum(1 for row in items if int(row.get("policy_allowed") or 0) == 1),
        "unclosed_released_items": unclosed,
    }


def local_learning_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    post_apply = all_rows(
        conn,
        """
        SELECT id, segment_id, human_label, corrected_text, learned_at,
               confirmation_synced_at, queue_source, origin, reviewed_at, updated_at
        FROM local_learning_candidates
        WHERE queue_source = 'post-apply-protected'
           OR origin = 'candidate_apply_protected'
        ORDER BY id
        """,
    )
    latest_ingest = latest_file("*post_apply_learning_ingest_summary.json")
    latest_feedback = latest_file("*apply_local_learning_feedback*.txt")
    label_counts = Counter(str(row.get("human_label") or "") for row in post_apply)
    divergences: list[dict[str, Any]] = []
    for row in post_apply:
        if not row.get("learned_at"):
            divergences.append({"segment_id": row["segment_id"], "reason": "missing_learned_at"})
        if not row.get("confirmation_synced_at"):
            divergences.append({"segment_id": row["segment_id"], "reason": "missing_confirmation_synced_at"})
    return {
        "latest_ingest_summary_path": str(latest_ingest) if latest_ingest else None,
        "latest_feedback_report_path": str(latest_feedback) if latest_feedback else None,
        "post_apply_learning_rows": len(post_apply),
        "post_apply_unique_segments": len({int(row["segment_id"]) for row in post_apply}),
        "learned_count": sum(1 for row in post_apply if row.get("learned_at")),
        "confirmation_synced_count": sum(1 for row in post_apply if row.get("confirmation_synced_at")),
        "label_counts": dict(sorted(label_counts.items())),
        "divergences": divergences,
    }


def human_candidate_backlog_snapshot() -> dict[str, Any]:
    paths = sorted(
        reports_dir().glob("*candidate_human_review_decision_record_summary.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not paths:
        return {
            "summary_path": None,
            "approved_already_applied": 0,
            "needs_more_context": 0,
            "duplicates": 0,
            "rejected": 0,
            "raw": {},
        }
    data = read_json(paths[0])
    closeout_path = latest_file("*post_apply_candidate_closeout_status_summary.json")
    closeout = read_json(closeout_path) if closeout_path else {}
    status_counts = closeout.get("status_counts", {})
    approved_count = int(data.get("approve_for_future_apply_count") or 0)
    applied_or_corrected = int(status_counts.get("applied") or 0) + int(status_counts.get("human_corrected") or 0)
    return {
        "summary_path": str(paths[0]),
        "closeout_summary_path": str(closeout_path) if closeout_path else None,
        "approved_already_applied": min(approved_count, applied_or_corrected),
        "needs_more_context": int(data.get("needs_more_context_count") or 0),
        "duplicates": int(data.get("duplicate_of_existing_candidate_count") or 0),
        "rejected": int(data.get("reject_count") or 0),
        "raw": data,
        "closeout_status_counts": status_counts,
    }


def delta_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    previous = run_snapshot(conn, PREVIOUS_RUN_ID)
    current = run_snapshot(conn, CURRENT_RUN_ID)
    changed_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM segment_state_items before
        JOIN segment_state_items after ON after.segment_id = before.segment_id
        WHERE before.run_id = ?
          AND after.run_id = ?
          AND (
            before.final_state != after.final_state
            OR before.state_group != after.state_group
            OR before.needs_output_apply != after.needs_output_apply
            OR before.confirmed_matches_output != after.confirmed_matches_output
          )
        """,
        (PREVIOUS_RUN_ID, CURRENT_RUN_ID),
    ).fetchone()[0]
    return {
        "previous_run_id": PREVIOUS_RUN_ID,
        "current_run_id": CURRENT_RUN_ID,
        "delta": {
            "total_segments": current["total_segments"] - previous["total_segments"],
            "closed_count": current["closed_count"] - previous["closed_count"],
            "pending_count": current["pending_count"] - previous["pending_count"],
            "output_apply_pending_count": current["output_apply_pending_count"]
            - previous["output_apply_pending_count"],
            "reopen_count": current["reopen_count"] - previous["reopen_count"],
            "changed_segment_count": int(changed_count or 0),
        },
    }


def build_summary() -> dict[str, Any]:
    with connect_readonly() as conn:
        current = run_snapshot(conn, CURRENT_RUN_ID)
        tracked = segment_state(conn, CURRENT_RUN_ID, TRACKED_SEGMENT_ID)
        previous_tracked = segment_state(conn, PREVIOUS_RUN_ID, TRACKED_SEGMENT_ID)
        policy = policy_snapshot(conn)
        learning_snapshot = local_learning_snapshot(conn)
        backlog = human_candidate_backlog_snapshot()
        delta = delta_snapshot(conn)
        lifecycle_action_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM segment_state_items
            WHERE run_id = ?
              AND lifecycle_policy_action = ?
            """,
            (CURRENT_RUN_ID, POLICY_ACTION),
        ).fetchone()[0]
        pending_reopen_tracked_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM segment_state_items
            WHERE run_id = ?
              AND segment_id = ?
              AND (state_group = 'pending' OR needs_reopen = 1)
            """,
            (CURRENT_RUN_ID, TRACKED_SEGMENT_ID),
        ).fetchone()[0]

    segment_closed = (
        tracked.get("state_group") == "closed"
        and tracked.get("final_state") == EXPECTED_FINAL_STATE
        and int(tracked.get("needs_reopen") or 0) == 0
        and int(pending_reopen_tracked_count or 0) == 0
    )
    policy_run = policy["run"]
    policy_active = (
        policy_run.get("policy_name") == POLICY_NAME
        and policy_run.get("policy_status") == "active"
        and int(policy_run.get("released_count") or 0) > 0
        and int(policy_run.get("blocked_count") or 0) == 0
    )
    policy_has_unclosed = bool(policy["unclosed_released_items"])
    local_learning_consistent = (
        learning_snapshot["post_apply_learning_rows"] >= 12
        and learning_snapshot["learned_count"] == learning_snapshot["post_apply_learning_rows"]
        and learning_snapshot["confirmation_synced_count"] == learning_snapshot["post_apply_learning_rows"]
        and not learning_snapshot["divergences"]
    )
    architecture_bridge_validated = (
        segment_closed
        and delta["delta"]["closed_count"] == 1
        and delta["delta"]["pending_count"] == -1
        and delta["delta"]["reopen_count"] == -1
        and delta["delta"]["output_apply_pending_count"] == 0
        and delta["delta"]["changed_segment_count"] == 1
        and policy_active
        and not policy_has_unclosed
    )
    gates = {
        "architecture_bridge_validated": architecture_bridge_validated,
        "segment_281562_closed": segment_closed,
        "needs_output_apply_total": current["output_apply_pending_count"],
        "policy_has_unclosed_released_items": policy_has_unclosed,
        "local_learning_consistent": local_learning_consistent,
        "safe_to_resume_training_diagnostics": architecture_bridge_validated and local_learning_consistent,
        "safe_to_resume_discovery": False,
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
    }
    if gates["architecture_bridge_validated"] and gates["local_learning_consistent"]:
        recommendation = (
            "Manter apply/producao full bloqueados. A frente de treino pode sair do hold operacional "
            "para planejar o proximo retarget read-only, mas sem executar discovery no mesmo ciclo."
        )
        next_prompt = "chat_exec_training_retarget_plan_after_bridge_checkpoint_prompt.md"
    else:
        recommendation = "Manter discovery/apply em hold e voltar para arquitetura com o gate especifico que falhou."
        next_prompt = "architecture_gate_failure_review_prompt.md"
    return {
        "schema_version": 1,
        "source": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "current_run": current,
        "previous_segment_281562": previous_tracked,
        "segment_281562": tracked,
        "delta_403_404": delta,
        "policy": {
            "run": policy_run,
            "released_item_count": policy["released_item_count"],
            "unclosed_released_items": policy["unclosed_released_items"],
            "lifecycle_action_count_in_run_404": int(lifecycle_action_count or 0),
        },
        "local_learning": learning_snapshot,
        "human_candidate_backlog": backlog,
        **gates,
        "recommendation": recommendation,
        "next_prompt_recommended": next_prompt,
        "confirmed_not_run": {
            "production_full": True,
            "apply": True,
            "lifecycle": True,
            "reindex": True,
            "segment_state_new": True,
            "issue_ledger": True,
            "confirmations": True,
            "retarget": True,
            "discovery": True,
        },
    }


def write_outputs(summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_training_resume_after_lifecycle_bridge_success"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    records = [
        {"record_type": "current_run", **summary["current_run"]},
        {"record_type": "segment_281562", **summary["segment_281562"]},
        {"record_type": "policy", **summary["policy"]},
        {"record_type": "local_learning", **summary["local_learning"]},
        {"record_type": "human_candidate_backlog", **summary["human_candidate_backlog"]},
    ]
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "training resume after lifecycle bridge success",
        f"source={RULE_VERSION}",
        "",
        "gates:",
        f"- architecture_bridge_validated={str(summary['architecture_bridge_validated']).lower()}",
        f"- segment_281562_closed={str(summary['segment_281562_closed']).lower()}",
        f"- needs_output_apply_total={summary['needs_output_apply_total']}",
        f"- policy_has_unclosed_released_items={str(summary['policy_has_unclosed_released_items']).lower()}",
        f"- local_learning_consistent={str(summary['local_learning_consistent']).lower()}",
        f"- safe_to_resume_training_diagnostics={str(summary['safe_to_resume_training_diagnostics']).lower()}",
        f"- safe_to_resume_discovery={str(summary['safe_to_resume_discovery']).lower()}",
        f"- apply_ready_now={summary['apply_ready_now']}",
        f"- production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
        "",
        "segment_281562:",
        f"- previous={summary['previous_segment_281562']['state_group']} / {summary['previous_segment_281562']['final_state']}",
        f"- current={summary['segment_281562']['state_group']} / {summary['segment_281562']['final_state']}",
        f"- needs_output_apply={summary['segment_281562']['needs_output_apply']}",
        f"- lifecycle_policy_action={summary['segment_281562']['lifecycle_policy_action']}",
        "",
        "current_totals:",
        f"- closed={summary['current_run']['closed_count']}",
        f"- pending={summary['current_run']['pending_count']}",
        f"- reopen={summary['current_run']['reopen_count']}",
        f"- needs_output_apply={summary['current_run']['output_apply_pending_count']}",
        f"- lifecycle_policy_action_count={summary['policy']['lifecycle_action_count_in_run_404']}",
        "",
        "local_learning:",
        f"- post_apply_learning_rows={summary['local_learning']['post_apply_learning_rows']}",
        f"- learned_count={summary['local_learning']['learned_count']}",
        f"- confirmation_synced_count={summary['local_learning']['confirmation_synced_count']}",
        f"- divergences={len(summary['local_learning']['divergences'])}",
        "",
        "human_candidate_backlog:",
        f"- approved_already_applied={summary['human_candidate_backlog']['approved_already_applied']}",
        f"- needs_more_context={summary['human_candidate_backlog']['needs_more_context']}",
        f"- duplicates={summary['human_candidate_backlog']['duplicates']}",
        f"- rejected={summary['human_candidate_backlog']['rejected']}",
        "",
        f"recommendation={summary['recommendation']}",
        f"next_prompt_recommended={summary['next_prompt_recommended']}",
        "",
        "confirmed_not_run=production_full, apply, lifecycle, reindex, segment-state novo, issue-ledger, confirmations, retarget, discovery",
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
        "architecture_bridge_validated",
        "segment_281562_closed",
        "needs_output_apply_total",
        "policy_has_unclosed_released_items",
        "local_learning_consistent",
        "safe_to_resume_training_diagnostics",
        "safe_to_resume_discovery",
        "apply_ready_now",
        "production_full_recommended_now",
        "next_prompt_recommended",
    ]:
        print(f"{key}={summary[key]}")


if __name__ == "__main__":
    main()
