from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "human_confirmed_post_apply_lifecycle_readonly_plan_v1"
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


def one(conn: sqlite3.Connection, query: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


def rows(conn: sqlite3.Connection, query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def latest_segment_state_run_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise SystemExit("missing completed segment_state_run")
    return int(row["id"])


def run_totals(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
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
    }


def policy_run(conn: sqlite3.Connection, policy_run_id: int) -> dict[str, Any]:
    run = one(
        conn,
        """
        SELECT *
        FROM auto_confirmation_reopen_lifecycle_policy_runs
        WHERE id = ?
        """,
        (policy_run_id,),
    )
    if not run:
        raise SystemExit(f"missing lifecycle policy_run_id={policy_run_id}")
    return run


def build_segment_probe(
    conn: sqlite3.Connection,
    segment_state_run_id: int,
    policy_run_id: int,
    segment_id: int,
) -> dict[str, Any]:
    policy_item = one(
        conn,
        """
        SELECT *
        FROM auto_confirmation_reopen_lifecycle_policy_items
        WHERE run_id = ?
          AND segment_id = ?
        """,
        (policy_run_id, segment_id),
    )
    state = one(
        conn,
        """
        SELECT *
        FROM segment_state_items
        WHERE run_id = ?
          AND segment_id = ?
        """,
        (segment_state_run_id, segment_id),
    )
    confirmation = one(
        conn,
        """
        SELECT *
        FROM segment_confirmations
        WHERE segment_id = ?
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """,
        (segment_id,),
    )
    output = one(
        conn,
        """
        SELECT segment_id, relative_path, output_line_number, portuguese_text
        FROM output_segments
        WHERE segment_id = ?
        """,
        (segment_id,),
    )
    open_issues_latest = rows(
        conn,
        """
        SELECT l.run_id, l.issue_family, l.issue_kind, l.issue_severity, l.status
        FROM ml_issue_ledger_items l
        JOIN (
            SELECT MAX(id) AS run_id
            FROM ml_issue_ledger_runs
            WHERE finished_at IS NOT NULL
        ) latest ON latest.run_id = l.run_id
        WHERE l.segment_id = ?
          AND l.status = 'open'
        ORDER BY l.issue_family, l.issue_kind
        """,
        (segment_id,),
    )
    output_text = (output or {}).get("portuguese_text")
    confirmed_text = (confirmation or {}).get("confirmed_text")
    guards = {
        "policy_item_present": policy_item is not None,
        "policy_allowed": bool(policy_item and int(policy_item.get("policy_allowed") or 0) == 1),
        "policy_action_matches": bool(policy_item and policy_item.get("policy_action") == POLICY_ACTION),
        "policy_token_status_ok": bool(policy_item and policy_item.get("token_status") == "ok"),
        "policy_issue_count_zero": bool(policy_item and int(policy_item.get("issue_count") or 0) == 0),
        "policy_high_issue_count_zero": bool(policy_item and int(policy_item.get("high_issue_count") or 0) == 0),
        "current_state_pending": bool(state and state.get("state_group") == "pending"),
        "current_state_reopen": bool(state and int(state.get("needs_reopen") or 0) == 1),
        "current_needs_output_apply_zero": bool(state and int(state.get("needs_output_apply") or 0) == 0),
        "current_confirmed_matches_output": bool(state and int(state.get("confirmed_matches_output") or 0) == 1),
        "confirmation_human_confirmed": bool(confirmation and confirmation.get("confirmation_level") == "human_confirmed"),
        "confirmation_local_learning": bool(confirmation and confirmation.get("confirmation_source") == "local_learning"),
        "confirmation_label_correct": bool(confirmation and confirmation.get("confirmation_label") == "correct"),
        "confirmation_locked": bool(confirmation and int(confirmation.get("locked") or 0) == 1),
        "output_matches_confirmation": bool(output_text and confirmed_text and output_text == confirmed_text),
        "latest_ledger_open_issues_zero": len(open_issues_latest) == 0,
    }
    eligible = all(guards.values())
    return {
        "segment_id": segment_id,
        "policy_item": policy_item,
        "current_state": state,
        "latest_confirmation": confirmation,
        "output": output,
        "latest_open_issues": open_issues_latest,
        "guards": guards,
        "expected_close_eligible": eligible,
        "expected_final_state": EXPECTED_FINAL_STATE if eligible else None,
        "expected_state_group": "closed" if eligible else None,
    }


def build_summary(args: argparse.Namespace) -> dict[str, Any]:
    with connect_readonly() as conn:
        state_run_id = args.segment_state_run_id or latest_segment_state_run_id(conn)
        policy = policy_run(conn, args.policy_run_id)
        totals = run_totals(conn, state_run_id)
        policy_items = rows(
            conn,
            """
            SELECT *
            FROM auto_confirmation_reopen_lifecycle_policy_items
            WHERE run_id = ?
            ORDER BY segment_id
            """,
            (args.policy_run_id,),
        )
        segment_ids = [int(row["segment_id"]) for row in policy_items]
        probes = [
            build_segment_probe(conn, state_run_id, args.policy_run_id, segment_id)
            for segment_id in segment_ids
        ]
    eligible_count = sum(1 for probe in probes if probe["expected_close_eligible"])
    expected_delta = {
        "total_segments": 0,
        "closed_count": eligible_count,
        "pending_count": -eligible_count,
        "output_apply_pending_count": 0,
        "reopen_count": -eligible_count,
    }
    hard_blocks: list[str] = []
    if policy.get("policy_name") != POLICY_NAME:
        hard_blocks.append("policy_name_mismatch")
    if policy.get("policy_status") != "active":
        hard_blocks.append("policy_not_active")
    if int(policy.get("released_count") or 0) != len(policy_items):
        hard_blocks.append("released_count_does_not_match_items")
    if int(policy.get("blocked_count") or 0) != 0:
        hard_blocks.append("policy_blocked_count_nonzero")
    if eligible_count != len(policy_items):
        hard_blocks.append("not_all_policy_items_are_expected_close_eligible")
    return {
        "schema_version": 1,
        "source": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": state_run_id,
        "policy_run_id": args.policy_run_id,
        "policy_run": policy,
        "policy_item_count": len(policy_items),
        "expected_close_eligible_count": eligible_count,
        "expected_final_state": EXPECTED_FINAL_STATE,
        "expected_policy_action": POLICY_ACTION,
        "current_totals": totals,
        "expected_delta_after_segment_state": expected_delta,
        "expected_totals_after_segment_state": {
            key: totals[key] + expected_delta[key]
            for key in ["total_segments", "closed_count", "pending_count", "output_apply_pending_count", "reopen_count"]
        },
        "segment_probes": probes,
        "hard_blocks": hard_blocks,
        "read_only_plan_ready": not hard_blocks,
        "run_segment_state_now": False,
        "run_reindex_now": False,
        "run_lifecycle_now": False,
        "run_issue_ledger_now": False,
        "run_production_full_now": False,
        "discovery_apply_hold": True,
        "recommended_next_action": (
            "after_user_approval_run_segment_state_only_then_delta_report"
            if not hard_blocks
            else "fix_policy_or_guard_blocks_before_any_segment_state_run"
        ),
    }


def write_outputs(summary: dict[str, Any]) -> tuple[Path, Path]:
    base = reports_dir() / f"{stamp()}_human_confirmed_post_apply_lifecycle_readonly_plan"
    txt_path = base.with_suffix(".txt")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "human-confirmed post-apply lifecycle read-only plan",
        f"source={RULE_VERSION}",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"policy_run_id={summary['policy_run_id']}",
        f"policy_name={summary['policy_run'].get('policy_name')}",
        f"policy_status={summary['policy_run'].get('policy_status')}",
        f"policy_item_count={summary['policy_item_count']}",
        f"expected_close_eligible_count={summary['expected_close_eligible_count']}",
        f"expected_final_state={summary['expected_final_state']}",
        f"hard_blocks={summary['hard_blocks']}",
        "",
        "current_totals:",
        *[f"- {key}: {value}" for key, value in summary["current_totals"].items()],
        "",
        "expected_delta_after_segment_state:",
        *[f"- {key}: {value}" for key, value in summary["expected_delta_after_segment_state"].items()],
        "",
        "expected_totals_after_segment_state:",
        *[f"- {key}: {value}" for key, value in summary["expected_totals_after_segment_state"].items()],
        "",
        "segment_probes:",
    ]
    for probe in summary["segment_probes"]:
        state = probe["current_state"] or {}
        lines.extend(
            [
                f"- segment_id={probe['segment_id']}",
                f"  current={state.get('state_group')} / {state.get('final_state')}",
                f"  expected_close_eligible={probe['expected_close_eligible']}",
                f"  expected_final_state={probe['expected_final_state']}",
                f"  guards={json.dumps(probe['guards'], ensure_ascii=False, sort_keys=True)}",
            ]
        )
    lines.extend(
        [
            "",
            "execution_flags:",
            f"- run_segment_state_now={str(summary['run_segment_state_now']).lower()}",
            f"- run_reindex_now={str(summary['run_reindex_now']).lower()}",
            f"- run_lifecycle_now={str(summary['run_lifecycle_now']).lower()}",
            f"- run_issue_ledger_now={str(summary['run_issue_ledger_now']).lower()}",
            f"- run_production_full_now={str(summary['run_production_full_now']).lower()}",
            f"- discovery_apply_hold={str(summary['discovery_apply_hold']).lower()}",
            f"recommended_next_action={summary['recommended_next_action']}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy-run-id", type=int, required=True)
    parser.add_argument("--segment-state-run-id", type=int)
    args = parser.parse_args()
    summary = build_summary(args)
    txt_path, summary_path = write_outputs(summary)
    print(f"txt={txt_path}")
    print(f"summary={summary_path}")
    for key in [
        "segment_state_run_id",
        "policy_run_id",
        "policy_item_count",
        "expected_close_eligible_count",
        "expected_delta_after_segment_state",
        "hard_blocks",
        "read_only_plan_ready",
        "run_segment_state_now",
        "run_reindex_now",
        "run_production_full_now",
        "recommended_next_action",
    ]:
        print(f"{key}={summary[key]}")


if __name__ == "__main__":
    main()
