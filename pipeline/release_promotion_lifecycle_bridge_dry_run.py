from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import canonical_localization_text, structural_tokens


SOURCE = "release_promotion_lifecycle_bridge_dry_run_v1"
POLICY_NAME = "validated_release_promotion_lifecycle_bridge"
POLICY_ACTION = "close_reopen_validated_release_promotion_lifecycle"
EXPECTED_FINAL_STATE = "closed_auto_confirmed_validated_release_promotion_lifecycle"
ALLOWED_LANES = {
    "deterministic_bold_microrepair",
    "human_locked_evidence",
    "clean_score_gain",
    "reviewed_score_issue_superseded",
    "reviewed_token_false_mismatch",
    "reviewed_context_false_hold",
}
ALLOWED_GATES = {
    "allow_candidate_score_improved",
    "allow_score_calibrated_repair",
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
    return conn


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run lifecycle bridge for audited release promotions.")
    parser.add_argument("--validation-jsonl", type=Path, required=True)
    parser.add_argument("--expected-count", type=int, required=True)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def latest_ledger_run_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT MAX(id) AS id FROM ml_issue_ledger_runs WHERE finished_at IS NOT NULL"
    ).fetchone()
    return int(row["id"]) if row and row["id"] is not None else None


def fetch_current(
    conn: sqlite3.Connection,
    state_run_id: int,
    ledger_run_id: int | None,
    segment_ids: list[int],
) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    ledger_join = ""
    ledger_column = "0 AS open_ledger_issue_count"
    ledger_params: list[Any] = []
    if ledger_run_id is not None:
        ledger_join = f"""
        LEFT JOIN (
            SELECT segment_id, COUNT(*) AS open_ledger_issue_count
            FROM ml_issue_ledger_items
            WHERE run_id = ?
              AND lower(COALESCE(status, 'open')) NOT LIKE 'closed%'
            GROUP BY segment_id
        ) ledger ON ledger.segment_id = source.id
        """
        ledger_column = "COALESCE(ledger.open_ledger_issue_count, 0) AS open_ledger_issue_count"
        ledger_params.append(ledger_run_id)
    rows = conn.execute(
        f"""
        SELECT
            source.id AS segment_id,
            source.is_active,
            state.final_state,
            state.state_group,
            state.review_state,
            state.needs_output_apply,
            state.confirmed_matches_output,
            state.locked AS state_locked,
            output.portuguese_text AS output_text,
            confirmation.confirmed_text,
            confirmation.confirmation_level,
            confirmation.confirmation_source,
            confirmation.confirmation_label,
            confirmation.locked AS confirmation_locked,
            {ledger_column}
        FROM source_segments source
        JOIN segment_state_items state
          ON state.segment_id = source.id
         AND state.run_id = ?
        JOIN output_segments output ON output.segment_id = source.id
        JOIN segment_confirmations confirmation ON confirmation.segment_id = source.id
        {ledger_join}
        WHERE source.id IN ({placeholders})
        """,
        [state_run_id, *ledger_params, *segment_ids],
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def evaluate(validation: dict[str, Any], current: dict[str, Any] | None) -> dict[str, Any]:
    reasons: list[str] = []
    lane = str(validation.get("lane") or "")
    gate = str(validation.get("promotion_gate") or "")
    if validation.get("readiness") != "ready_for_lifecycle_dry_run":
        reasons.append("validation_not_ready_for_lifecycle")
    if lane not in ALLOWED_LANES:
        reasons.append("lane_not_allowed")
    if gate not in ALLOWED_GATES:
        reasons.append("promotion_gate_not_allowed")
    if current is None:
        reasons.append("missing_current_state")
    else:
        if int(current.get("is_active") or 0) != 1:
            reasons.append("source_not_active")
        if current.get("final_state") != "reopen_auto_confirmed_autofix":
            reasons.append("unexpected_final_state")
        if current.get("state_group") != "pending":
            reasons.append("state_group_not_pending")
        if int(current.get("needs_output_apply") or 0) != 0:
            reasons.append("needs_output_apply_not_zero")
        if int(current.get("confirmed_matches_output") or 0) != 1:
            reasons.append("confirmed_matches_output_not_one")
        if int(current.get("open_ledger_issue_count") or 0) != 0:
            reasons.append("open_ledger_issue_present")
        output_text = str(current.get("output_text") or "")
        confirmed_text = str(current.get("confirmed_text") or "")
        if canonical_localization_text(output_text) != canonical_localization_text(confirmed_text):
            reasons.append("output_confirmation_canonical_mismatch")
        if structural_tokens(output_text) != structural_tokens(confirmed_text):
            reasons.append("output_confirmation_token_mismatch")
        if str(current.get("confirmation_source") or "") != str(validation.get("confirmation_source") or ""):
            reasons.append("confirmation_source_changed_since_audit")
        if lane == "human_locked_evidence":
            if int(current.get("confirmation_locked") or 0) != 1:
                reasons.append("human_evidence_not_locked")
        elif int(current.get("confirmation_locked") or 0) == 1:
            reasons.append("unexpected_lock_on_non_human_lane")

    released = not reasons
    return {
        "source": SOURCE,
        "record_type": "release_promotion_lifecycle_bridge_dry_run_item",
        "segment_id": int(validation.get("segment_id") or 0),
        "relative_path": validation.get("relative_path"),
        "source_key": validation.get("source_key"),
        "lane": lane,
        "promotion_gate": gate,
        "confirmation_source": validation.get("confirmation_source"),
        "policy_name": POLICY_NAME,
        "policy_action": POLICY_ACTION,
        "expected_final_state": EXPECTED_FINAL_STATE,
        "policy_allowed": released,
        "dry_run_decision": "released" if released else "blocked",
        "block_reasons": reasons,
        "current_final_state": current.get("final_state") if current else None,
        "open_ledger_issue_count": int(current.get("open_ledger_issue_count") or 0) if current else None,
        "ready_for_apply": False,
        "apply_count": 0,
        "policy_write_count": 0,
        "segment_state_count": 0,
        "output_changed": False,
    }


def build(rows: list[dict[str, Any]], expected_count: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if len(rows) != expected_count:
        raise SystemExit(f"Validation count mismatch: found {len(rows)}, expected {expected_count}.")
    run_ids = {int(row.get("segment_state_run_id") or 0) for row in rows}
    if len(run_ids) != 1:
        raise SystemExit(f"Expected one segment-state run, found {sorted(run_ids)}")
    state_run_id = next(iter(run_ids))
    segment_ids = [int(row["segment_id"]) for row in rows]
    with connect_readonly() as conn:
        ledger_run_id = latest_ledger_run_id(conn)
        current = fetch_current(conn, state_run_id, ledger_run_id, segment_ids)
    records = [evaluate(row, current.get(int(row["segment_id"]))) for row in rows]
    decisions = Counter(record["dry_run_decision"] for record in records)
    block_counts = Counter(reason for record in records for reason in record["block_reasons"])
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_lifecycle_bridge_dry_run",
        "segment_state_run_id": state_run_id,
        "ledger_run_id": ledger_run_id,
        "policy_name": POLICY_NAME,
        "policy_action": POLICY_ACTION,
        "expected_final_state": EXPECTED_FINAL_STATE,
        "candidate_count": len(records),
        "released_count": decisions["released"],
        "blocked_count": decisions["blocked"],
        "block_reason_counts": dict(block_counts),
        "released_segment_ids": [record["segment_id"] for record in records if record["policy_allowed"]],
        "blocked_segment_ids": [record["segment_id"] for record in records if not record["policy_allowed"]],
        "expected_delta_after_policy_and_segment_state": {
            "closed": decisions["released"],
            "pending": -decisions["released"],
            "reopen": -decisions["released"],
            "needs_output_apply": 0,
        },
        "policy_write_count": 0,
        "apply_count": 0,
        "segment_state_count": 0,
        "source_changed": False,
        "output_changed": False,
        "recommendation": (
            "Materialize this exact audited set only if released_count equals candidate_count and blocked_count is zero. "
            "Then run segment-state separately and verify the exact expected delta."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_promotion_lifecycle_bridge_dry_run"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    lines = [
        "# Release Promotion Lifecycle Bridge Dry-run",
        "",
        f"- segment-state: `{summary['segment_state_run_id']}`",
        f"- ledger: `{summary['ledger_run_id']}`",
        f"- candidates: `{summary['candidate_count']}`",
        f"- released: `{summary['released_count']}`",
        f"- blocked: `{summary['blocked_count']}`",
        "",
        "| ID | lane | decision | reasons |",
        "|---:|---|---|---|",
    ]
    for record in records:
        reasons = ", ".join(record["block_reasons"]) or "-"
        lines.append(
            f"| {record['segment_id']} | `{record['lane']}` | `{record['dry_run_decision']}` | {reasons} |"
        )
    lines.extend(
        [
            "",
            "## Guards",
            "",
            "- policy writes: `0`",
            "- apply: `0`",
            "- segment-state: `0`",
            "- source/output changed: `false`",
            "",
            f"Recommendation: {summary['recommendation']}",
        ]
    )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return md_path, jsonl_path, summary_path


def main() -> int:
    args = parse_args()
    rows = read_jsonl(args.validation_jsonl)
    records, summary = build(rows, args.expected_count)
    paths = write_reports(records, summary)
    print(json.dumps({"summary": summary, "artifacts": [str(path) for path in paths]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
