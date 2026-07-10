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
from release_readiness_ui_tooltips_packet2_spanish_residue_3_decisions import DECISIONS


SOURCE = "release_readiness_ui_tooltips_packet2_spanish_residue_3_lifecycle_bridge_v1"
POLICY_NAME = "human_confirmed_ui_tooltips_context_sublote_equal_output_lifecycle_bridge"
POLICY_ACTION = "close_reopen_human_confirmed_ui_tooltips_context_sublote_equal_output_lifecycle"
FINAL_STATE = "closed_auto_confirmed_human_confirmed_ui_tooltips_context_sublote_equal_output_lifecycle"
LABEL_FAMILY = "human_confirmed_ui_tooltips_packet2_spanish_residue_3_equal_output"
DEFAULT_SEGMENT_STATE_RUN_ID = 566
DEFAULT_LEDGER_RUN_ID = 76
EXPECTED_COUNT = 2


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lifecycle bridge for the two remaining spanish-residue-3 UI/tooltips rows.")
    parser.add_argument("--segment-state-run-id", type=int, default=DEFAULT_SEGMENT_STATE_RUN_ID)
    parser.add_argument("--ledger-run-id", type=int, default=DEFAULT_LEDGER_RUN_ID)
    parser.add_argument("--expected-count", type=int, default=EXPECTED_COUNT)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db.get_database_path(db.load_settings()), timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def open_issue_count(conn, segment_id: int, ledger_run_id: int) -> tuple[int, int]:
    row = conn.execute(
        """
        SELECT SUM(CASE WHEN COALESCE(status,'open') NOT IN ('closed','resolved','dismissed') THEN 1 ELSE 0 END) open_count,
               SUM(CASE WHEN COALESCE(status,'open') NOT IN ('closed','resolved','dismissed')
                         AND lower(COALESCE(issue_severity,'')) IN ('high','critical','error') THEN 1 ELSE 0 END) high_count
        FROM ml_issue_ledger_items
        WHERE run_id=? AND segment_id=?
        """,
        (ledger_run_id, segment_id),
    ).fetchone()
    return int(row["open_count"] or 0), int(row["high_count"] or 0)


def existing_policy_item_count(conn, segment_id: int) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) n
        FROM auto_confirmation_reopen_lifecycle_policy_items item
        JOIN auto_confirmation_reopen_lifecycle_policy_runs run ON run.id=item.run_id
        WHERE item.segment_id=? AND item.policy_allowed=1 AND run.policy_status='active'
        """,
        (segment_id,),
    ).fetchone()
    return int(row["n"] or 0)


def build_records(conn, args) -> list[dict[str, Any]]:
    ids = sorted(DECISIONS)
    ph = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT state.segment_id, state.relative_path, state.source_key, state.final_state,
               state.state_group, state.review_state, state.confirmation_level,
               state.confirmation_label, state.locked, state.confirmed_matches_output,
               state.needs_output_apply, output.portuguese_text AS output_text,
               confirmation.confirmed_text
        FROM segment_state_items state
        JOIN output_segments output ON output.segment_id=state.segment_id
        JOIN segment_confirmations confirmation ON confirmation.segment_id=state.segment_id
        WHERE state.run_id=?
          AND state.segment_id IN ({ph})
          AND state.final_state='reopen_auto_confirmed_autofix'
          AND state.state_group='pending'
        ORDER BY state.segment_id
        """,
        [args.segment_state_run_id, *ids],
    ).fetchall()
    records = []
    for row in rows:
        r = dict(row)
        segment_id = int(r["segment_id"])
        open_count, high_count = open_issue_count(conn, segment_id, args.ledger_run_id)
        existing = existing_policy_item_count(conn, segment_id)
        reasons: list[str] = []
        if r["review_state"] != "human_locked" or r["confirmation_level"] != "human_confirmed" or int(r["locked"] or 0) != 1:
            reasons.append("not_human_locked")
        if int(r["confirmed_matches_output"] or 0) != 1 or int(r["needs_output_apply"] or 0) != 0:
            reasons.append("output_state_guard_failed")
        if open_count or high_count:
            reasons.append("open_or_high_issue")
        if existing:
            reasons.append("existing_policy_item")
        if canonical_localization_text(r["output_text"] or "") != canonical_localization_text(r["confirmed_text"] or ""):
            reasons.append("canonical_output_not_confirmed")
        records.append({
            "source": SOURCE,
            "record_type": "policy_bridge_item",
            "segment_id": segment_id,
            "relative_path": r["relative_path"],
            "source_key": r["source_key"],
            "human_decision": DECISIONS[segment_id][0],
            "confirmation_label": r["confirmation_label"],
            "policy_name": POLICY_NAME,
            "policy_action": POLICY_ACTION,
            "expected_final_state": FINAL_STATE,
            "open_issue_count": open_count,
            "high_issue_count": high_count,
            "existing_policy_item_count": existing,
            "dry_run_decision": "released" if not reasons else "blocked",
            "block_reasons": reasons,
            "candidate_generation_count": 0,
            "apply_count": 0,
            "lifecycle_count": 0,
            "segment_state_count": 0,
            "reindex_count": 0,
            "production_full_count": 0,
        })
    return records


def insert_policy(conn, records: list[dict[str, Any]], txt: Path, jsonl: Path) -> int:
    started = now_iso()
    cur = conn.execute(
        """
        INSERT INTO auto_confirmation_reopen_lifecycle_policy_runs (
          rule_version, queue_run_id, audit_run_id, policy_name, label_family,
          policy_status, candidate_count, released_count, blocked_count,
          manual_boundary_count, invalid_count, report_path, csv_path, jsonl_path,
          started_at, finished_at, updated_at
        )
        VALUES (?, NULL, NULL, ?, ?, 'active', ?, ?, 0, 0, 0, ?, NULL, ?, ?, ?, ?)
        """,
        (SOURCE, POLICY_NAME, LABEL_FAMILY, len(records), len(records), str(txt), str(jsonl), started, started, started),
    )
    run_id = int(cur.lastrowid)
    for record in records:
        conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_lifecycle_policy_items (
              run_id, queue_run_id, queue_item_id, audit_run_id, audit_item_id,
              segment_id, relative_path, source_key, source_line_number,
              label_family, confirmation_label, policy_action, policy_allowed,
              block_reason, output_match_kind, token_status, issue_count,
              high_issue_count, model_safe_probability, review_priority, reasons_json, created_at
            )
            VALUES (?, NULL, NULL, NULL, NULL, ?, ?, ?, NULL, ?, ?, ?, 1, NULL,
                    'canonical_l10n_output_confirmation_match', 'ok', 0, 0, 1.0, 0.0, ?, ?)
            """,
            (
                run_id,
                int(record["segment_id"]),
                record["relative_path"],
                record["source_key"],
                LABEL_FAMILY,
                record["confirmation_label"],
                POLICY_ACTION,
                json.dumps({"source": SOURCE, "segment_state_run_id": DEFAULT_SEGMENT_STATE_RUN_ID, "expected_final_state": FINAL_STATE}, ensure_ascii=False, sort_keys=True),
                started,
            ),
        )
    conn.commit()
    return run_id


def write(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_packet2_spanish_residue_3_lifecycle_bridge_{summary['mode']}"
    txt = base.with_suffix(".txt")
    jsonl = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt), "jsonl": str(jsonl), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt.write_text("\n".join([
        "UI/tooltips packet2 spanish residue 3 lifecycle bridge",
        f"mode={summary['mode']}",
        f"policy_run_id={summary.get('policy_run_id') or ''}",
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
    ]) + "\n", encoding="utf-8")
    return txt, jsonl, summary_path


def main() -> None:
    args = parse_args()
    mode = "apply" if args.apply else "dry_run"
    with connect() as conn:
        records = build_records(conn, args)
        released = [r for r in records if r["dry_run_decision"] == "released"]
        blocked = [r for r in records if r["dry_run_decision"] != "released"]
        if args.apply and (len(released) != args.expected_count or blocked):
            raise SystemExit("bridge guards failed; refusing apply")
        summary_stub = {"mode": mode, "candidate_count": len(records), "released_count": len(released), "blocked_count": len(blocked), "block_reason_counts": dict(Counter(x for r in blocked for x in r["block_reasons"]).most_common())}
        txt, jsonl, summary_path = write(records, {**summary_stub, "schema_version": 1, "source": SOURCE, "policy_name": POLICY_NAME, "policy_action": POLICY_ACTION, "expected_final_state": FINAL_STATE, "policy_run_id": None, "segment_state_run_id": args.segment_state_run_id, "ledger_run_id": args.ledger_run_id, "released_segment_ids": [r["segment_id"] for r in released], "candidate_generation_count": 0, "apply_count": 0, "lifecycle_count": 0, "segment_state_count": 0, "reindex_count": 0, "production_full_count": 0, "source_changed": False, "output_changed": False})
        policy_run_id = insert_policy(conn, released, txt, jsonl) if args.apply else None
        if args.apply:
            write(records, {**summary_stub, "schema_version": 1, "source": SOURCE, "policy_name": POLICY_NAME, "policy_action": POLICY_ACTION, "expected_final_state": FINAL_STATE, "policy_run_id": policy_run_id, "segment_state_run_id": args.segment_state_run_id, "ledger_run_id": args.ledger_run_id, "released_segment_ids": [r["segment_id"] for r in released], "candidate_generation_count": 0, "apply_count": 0, "lifecycle_count": 0, "segment_state_count": 0, "reindex_count": 0, "production_full_count": 0, "source_changed": False, "output_changed": False})
    print(f"txt={txt}")
    print(f"jsonl={jsonl}")
    print(f"summary={summary_path}")
    print(f"mode={mode}")
    print(f"policy_run_id={policy_run_id or ''}")
    print(f"candidate_count={len(records)}")
    print(f"released_count={len(released)}")
    print(f"blocked_count={len(blocked)}")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
