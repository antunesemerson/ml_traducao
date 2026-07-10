from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import canonical_localization_text
from release_readiness_ui_tooltips_packet2_context_sublote_decisions import DECISIONS


SOURCE = "release_readiness_ui_tooltips_packet2_context_sublote_lifecycle_bridge_v1"
POLICY_NAME = "human_confirmed_ui_tooltips_context_sublote_equal_output_lifecycle_bridge"
POLICY_ACTION = "close_reopen_human_confirmed_ui_tooltips_context_sublote_equal_output_lifecycle"
FINAL_STATE = "closed_auto_confirmed_human_confirmed_ui_tooltips_context_sublote_equal_output_lifecycle"
LABEL_FAMILY = "human_confirmed_ui_tooltips_packet2_context_sublote_equal_output"
DEFAULT_SEGMENT_STATE_RUN_ID = 562
DEFAULT_LEDGER_RUN_ID = 76
EXPECTED_COUNT = 13


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db.get_database_path(db.load_settings()), timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Materialize narrow lifecycle bridge for UI/tooltips context sublote.")
    parser.add_argument("--segment-state-run-id", type=int, default=DEFAULT_SEGMENT_STATE_RUN_ID)
    parser.add_argument("--ledger-run-id", type=int, default=DEFAULT_LEDGER_RUN_ID)
    parser.add_argument("--expected-count", type=int, default=EXPECTED_COUNT)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def open_issue_count(conn: sqlite3.Connection, segment_id: int, ledger_run_id: int) -> tuple[int, int]:
    row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN COALESCE(status, 'open') NOT IN ('closed', 'resolved', 'dismissed') THEN 1 ELSE 0 END) AS open_count,
          SUM(CASE WHEN COALESCE(status, 'open') NOT IN ('closed', 'resolved', 'dismissed')
                    AND LOWER(COALESCE(issue_severity, '')) IN ('high', 'critical', 'error')
                   THEN 1 ELSE 0 END) AS high_count
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND segment_id = ?
        """,
        (ledger_run_id, segment_id),
    ).fetchone()
    return int(row["open_count"] or 0), int(row["high_count"] or 0)


def existing_policy_item_count(conn: sqlite3.Connection, segment_id: int) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM auto_confirmation_reopen_lifecycle_policy_items item
        JOIN auto_confirmation_reopen_lifecycle_policy_runs run
          ON run.id = item.run_id
        WHERE item.segment_id = ?
          AND item.policy_allowed = 1
          AND run.policy_status = 'active'
        """,
        (segment_id,),
    ).fetchone()
    return int(row["n"] or 0)


def build_records(conn: sqlite3.Connection, args: argparse.Namespace) -> list[dict[str, Any]]:
    ids = sorted(DECISIONS)
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT
          state.segment_id,
          state.relative_path,
          state.source_key,
          state.final_state,
          state.state_group,
          state.review_state,
          state.confirmation_level,
          state.confirmation_label,
          state.locked,
          state.confirmed_matches_output,
          state.needs_output_apply,
          output.portuguese_text AS output_text,
          confirmation.confirmed_text
        FROM segment_state_items state
        JOIN output_segments output
          ON output.segment_id = state.segment_id
        JOIN segment_confirmations confirmation
          ON confirmation.segment_id = state.segment_id
        WHERE state.run_id = ?
          AND state.segment_id IN ({placeholders})
          AND state.final_state = 'reopen_auto_confirmed_autofix'
          AND state.state_group = 'pending'
        ORDER BY state.segment_id
        """,
        [args.segment_state_run_id, *ids],
    ).fetchall()
    records: list[dict[str, Any]] = []
    for row in rows:
        row_dict = dict(row)
        segment_id = int(row_dict["segment_id"])
        open_count, high_count = open_issue_count(conn, segment_id, args.ledger_run_id)
        existing_count = existing_policy_item_count(conn, segment_id)
        reasons: list[str] = []
        if row_dict["final_state"] != "reopen_auto_confirmed_autofix":
            reasons.append("not_reopen_auto_confirmed_autofix")
        if row_dict["state_group"] != "pending":
            reasons.append("not_pending")
        if row_dict["review_state"] != "human_locked":
            reasons.append("review_state_not_human_locked")
        if row_dict["confirmation_level"] != "human_confirmed":
            reasons.append("not_human_confirmed")
        if int(row_dict["locked"] or 0) != 1:
            reasons.append("not_locked")
        if int(row_dict["confirmed_matches_output"] or 0) != 1:
            reasons.append("confirmed_matches_output_not_1")
        if int(row_dict["needs_output_apply"] or 0) != 0:
            reasons.append("needs_output_apply")
        if open_count != 0:
            reasons.append("open_issue_count_not_0")
        if high_count != 0:
            reasons.append("high_issue_count_not_0")
        if existing_count != 0:
            reasons.append("existing_policy_item")
        if canonical_localization_text(row_dict["output_text"] or "") != canonical_localization_text(
            row_dict["confirmed_text"] or ""
        ):
            reasons.append("canonical_output_not_confirmed")
        records.append(
            {
                "record_type": "policy_bridge_dry_run_item",
                "source": SOURCE,
                "segment_id": segment_id,
                "relative_path": row_dict["relative_path"],
                "source_key": row_dict["source_key"],
                "human_decision": DECISIONS[segment_id][0],
                "final_state": row_dict["final_state"],
                "state_group": row_dict["state_group"],
                "review_state": row_dict["review_state"],
                "confirmation_level": row_dict["confirmation_level"],
                "confirmation_label": row_dict["confirmation_label"],
                "locked": int(row_dict["locked"] or 0),
                "confirmed_matches_output": int(row_dict["confirmed_matches_output"] or 0),
                "needs_output_apply": int(row_dict["needs_output_apply"] or 0),
                "open_issue_count": open_count,
                "high_issue_count": high_count,
                "existing_policy_item_count": existing_count,
                "policy_name": POLICY_NAME,
                "policy_action": POLICY_ACTION,
                "expected_final_state": FINAL_STATE,
                "dry_run_decision": "released" if not reasons else "blocked",
                "block_reasons": reasons,
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )
    return records


def validate_records(records: list[dict[str, Any]], args: argparse.Namespace) -> None:
    released = [record for record in records if record["dry_run_decision"] == "released"]
    blocked = [record for record in records if record["dry_run_decision"] != "released"]
    if len(released) != args.expected_count:
        raise SystemExit(f"released_count guard failed: {len(released)} != {args.expected_count}")
    if blocked:
        raise SystemExit("blocked records present; refusing materialization")


def insert_policy(conn: sqlite3.Connection, records: list[dict[str, Any]], txt_path: Path, jsonl_path: Path) -> int:
    started = now_iso()
    cursor = conn.execute(
        """
        INSERT INTO auto_confirmation_reopen_lifecycle_policy_runs (
            rule_version, queue_run_id, audit_run_id, policy_name, label_family,
            policy_status, candidate_count, released_count, blocked_count,
            manual_boundary_count, invalid_count, report_path, csv_path, jsonl_path,
            started_at, finished_at, updated_at
        )
        VALUES (?, NULL, NULL, ?, ?, 'active', ?, ?, 0, 0, 0, ?, NULL, ?, ?, ?, ?)
        """,
        (
            SOURCE,
            POLICY_NAME,
            LABEL_FAMILY,
            len(records),
            len(records),
            str(txt_path),
            str(jsonl_path),
            started,
            started,
            started,
        ),
    )
    run_id = int(cursor.lastrowid)
    for record in records:
        conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_lifecycle_policy_items (
                run_id, queue_run_id, queue_item_id, audit_run_id, audit_item_id,
                segment_id, relative_path, source_key, source_line_number,
                label_family, confirmation_label, policy_action, policy_allowed,
                block_reason, output_match_kind, token_status, issue_count,
                high_issue_count, model_safe_probability, review_priority,
                reasons_json, created_at
            )
            VALUES (?, NULL, NULL, NULL, NULL, ?, ?, ?, NULL, ?, ?, ?, 1, NULL, ?, ?, 0, 0, ?, ?, ?, ?)
            """,
            (
                run_id,
                int(record["segment_id"]),
                record["relative_path"],
                record["source_key"],
                LABEL_FAMILY,
                record["confirmation_label"],
                POLICY_ACTION,
                "canonical_l10n_output_confirmation_match",
                "ok",
                1.0,
                0.0,
                json.dumps(
                    {
                        "source": SOURCE,
                        "segment_state_run_id": DEFAULT_SEGMENT_STATE_RUN_ID,
                        "ledger_run_id": DEFAULT_LEDGER_RUN_ID,
                        "expected_final_state": FINAL_STATE,
                        "human_decision": record["human_decision"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                started,
            ),
        )
    conn.commit()
    return run_id


def output_paths(mode: str) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_packet2_context_sublote_lifecycle_bridge_{mode}"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), Path(str(base) + "_summary.json")


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any], paths: tuple[Path, Path, Path]) -> None:
    txt_path, jsonl_path, summary_path = paths
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                "UI/tooltips packet2 context sublote lifecycle bridge",
                f"mode={summary['mode']}",
                f"policy_run_id={summary['policy_run_id'] or ''}",
                f"policy_name={POLICY_NAME}",
                f"policy_action={POLICY_ACTION}",
                f"expected_final_state={FINAL_STATE}",
                f"candidate_count={summary['candidate_count']}",
                f"released_count={summary['released_count']}",
                f"blocked_count={summary['blocked_count']}",
                f"block_reason_counts={json.dumps(summary['block_reason_counts'], ensure_ascii=False, sort_keys=True)}",
                "candidate_generation_count=0",
                "apply_count=0",
                "lifecycle_count=0",
                "segment_state_count=0",
                "reindex_count=0",
                "production_full_count=0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")


def main() -> None:
    args = parse_args()
    mode = "apply" if args.apply else "dry_run"
    with connect() as conn:
        records = build_records(conn, args)
        released = [record for record in records if record["dry_run_decision"] == "released"]
        blocked = [record for record in records if record["dry_run_decision"] != "released"]
        if args.apply:
            validate_records(records, args)
        paths = output_paths(mode)
        policy_run_id = insert_policy(conn, released, paths[0], paths[1]) if args.apply else None
    block_counts = Counter(reason for record in blocked for reason in record["block_reasons"])
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": mode,
        "policy_name": POLICY_NAME,
        "policy_action": POLICY_ACTION,
        "expected_final_state": FINAL_STATE,
        "policy_run_id": policy_run_id,
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": args.ledger_run_id,
        "candidate_count": len(records),
        "released_count": len(released),
        "blocked_count": len(blocked),
        "released_segment_ids": [int(record["segment_id"]) for record in released],
        "blocked_segment_ids": [int(record["segment_id"]) for record in blocked],
        "human_decision_counts": dict(Counter(record["human_decision"] for record in released).most_common()),
        "block_reason_counts": dict(block_counts.most_common()),
        "expected_delta_after_segment_state": {
            "closed_count": len(released),
            "pending_count": -len(released),
            "reopen_count": -len(released),
            "output_apply_pending_count": 0,
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
        "single_operational_recommendation": (
            "Materialize this bridge only if released_count equals expected count and blocked_count=0."
            if not args.apply
            else "Run only segment-state and delta against the previous complete run."
        ),
    }
    write_reports(records, summary, paths)
    print(f"mode={mode}")
    print(f"policy_run_id={policy_run_id or ''}")
    print(f"candidate_count={len(records)}")
    print(f"released_count={len(released)}")
    print(f"blocked_count={len(blocked)}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
