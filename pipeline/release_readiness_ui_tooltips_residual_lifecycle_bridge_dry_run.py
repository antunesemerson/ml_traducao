from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens
from apply_segment_state_updates import canonical_localization_text


SOURCE = "release_readiness_ui_tooltips_residual_lifecycle_bridge_dry_run_v1"
DEFAULT_SEGMENT_STATE_RUN_ID = 551
DEFAULT_LEDGER_RUN_ID = 76
POLICY_NAME = "human_confirmed_ui_tooltips_residual_equal_output_lifecycle_bridge"
POLICY_ACTION = "close_reopen_human_confirmed_ui_tooltips_residual_equal_output_lifecycle"
FINAL_STATE = "closed_auto_confirmed_human_confirmed_ui_tooltips_residual_equal_output_lifecycle"
TARGET_SEGMENT_IDS = [
    4184,
    35050,
    35493,
    38526,
    46892,
    47168,
    49017,
    49487,
    49928,
    53322,
    58495,
    62803,
    62843,
    74518,
]


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
    parser = argparse.ArgumentParser(description="Read-only lifecycle bridge dry-run for reviewed UI/tooltips residuals.")
    parser.add_argument("--segment-state-run-id", type=int, default=DEFAULT_SEGMENT_STATE_RUN_ID)
    parser.add_argument("--ledger-run-id", type=int, default=DEFAULT_LEDGER_RUN_ID)
    return parser.parse_args()


def token_surface(text: str | None) -> str:
    text = text or ""
    if "\n" in text or "\\n" in text:
        return "multiline"
    tokens = protected_tokens(text)
    if not tokens:
        return "plain_text"
    blob = " ".join(tokens)
    if "Select_CString" in blob or "SelectLocalization" in blob:
        return "dynamic_select"
    if any(marker in blob for marker in [".Get", ".Custom", "ROOT.", "scope:", "SCOPE.", "GetScriptValue"]):
        return "dynamic_getter"
    return "light_token"


def canonical_equal(left: str | None, right: str | None) -> bool:
    return canonical_localization_text(left or "") == canonical_localization_text(right or "")


def fetch_rows(conn: sqlite3.Connection, state_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in TARGET_SEGMENT_IDS)
    rows = conn.execute(
        f"""
        WITH open_issues AS (
            SELECT
              segment_id,
              COUNT(*) AS open_issue_count,
              SUM(CASE WHEN lower(COALESCE(issue_severity,'')) IN ('high','critical','error') THEN 1 ELSE 0 END) AS high_issue_count,
              GROUP_CONCAT(DISTINCT issue_family) AS issue_families
            FROM ml_issue_ledger_items
            WHERE run_id = ?
              AND segment_id IN ({placeholders})
              AND COALESCE(status, 'open') NOT IN ('closed', 'resolved', 'dismissed')
            GROUP BY segment_id
        )
        SELECT
          state.segment_id,
          state.relative_path,
          state.source_key,
          state.final_state,
          state.state_group,
          state.review_state,
          state.apply_state,
          state.active_action,
          state.policy_action,
          state.confirmation_level,
          state.confirmation_label,
          state.locked,
          state.confirmed_matches_output,
          state.needs_output_apply,
          state.is_closed,
          output.portuguese_text AS output_text,
          confirmation.confirmed_text,
          confirmation.confirmation_source,
          COALESCE(open_issues.open_issue_count, 0) AS open_issue_count,
          COALESCE(open_issues.high_issue_count, 0) AS high_issue_count,
          COALESCE(open_issues.issue_families, '') AS issue_families
        FROM segment_state_items state
        JOIN output_segments output ON output.segment_id = state.segment_id
        JOIN segment_confirmations confirmation ON confirmation.segment_id = state.segment_id
        LEFT JOIN open_issues ON open_issues.segment_id = state.segment_id
        WHERE state.run_id = ?
          AND state.segment_id IN ({placeholders})
        ORDER BY state.segment_id
        """,
        [ledger_run_id, *TARGET_SEGMENT_IDS, state_run_id, *TARGET_SEGMENT_IDS],
    ).fetchall()
    return [dict(row) for row in rows]


def existing_policy_count(conn: sqlite3.Connection, segment_id: int) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM auto_confirmation_reopen_lifecycle_policy_items item
        JOIN auto_confirmation_reopen_lifecycle_policy_runs run ON run.id = item.run_id
        WHERE item.segment_id = ?
          AND run.policy_name = ?
          AND run.policy_status = 'active'
          AND item.policy_action = ?
          AND item.policy_allowed = 1
        """,
        (segment_id, POLICY_NAME, POLICY_ACTION),
    ).fetchone()
    return int(row["count"] or 0)


def classify(row: dict[str, Any], existing_policy_item_count: int) -> tuple[bool, list[str], str]:
    reasons: list[str] = []
    surface = token_surface(row.get("output_text") or row.get("confirmed_text"))
    if int(row.get("is_closed") or 0) != 0:
        reasons.append("already_closed")
    if row.get("final_state") != "reopen_auto_confirmed_autofix":
        reasons.append("unexpected_final_state")
    if row.get("state_group") != "pending":
        reasons.append("not_pending")
    if row.get("confirmation_level") != "human_confirmed":
        reasons.append("not_human_confirmed")
    if int(row.get("locked") or 0) != 1:
        reasons.append("not_locked")
    if row.get("review_state") != "human_locked":
        reasons.append("review_state_not_human_locked")
    if int(row.get("confirmed_matches_output") or 0) != 1:
        reasons.append("confirmed_matches_output_not_1")
    if int(row.get("needs_output_apply") or 0) != 0:
        reasons.append("needs_output_apply")
    if not canonical_equal(row.get("output_text"), row.get("confirmed_text")):
        reasons.append("canonical_output_not_confirmed")
    if int(row.get("open_issue_count") or 0) != 0:
        reasons.append("open_issue_present")
    if int(row.get("high_issue_count") or 0) != 0:
        reasons.append("high_issue_present")
    if surface not in {"plain_text", "light_token"}:
        reasons.append(f"unsupported_token_surface:{surface}")
    if existing_policy_item_count:
        reasons.append("existing_active_policy_item")
    return (not reasons), reasons, surface


def build_records(conn: sqlite3.Connection, args: argparse.Namespace) -> list[dict[str, Any]]:
    rows = fetch_rows(conn, args.segment_state_run_id, args.ledger_run_id)
    found = {int(row["segment_id"]) for row in rows}
    records: list[dict[str, Any]] = []
    for missing_id in sorted(set(TARGET_SEGMENT_IDS) - found):
        records.append(
            {
                "source": SOURCE,
                "record_type": "bridge_dry_run_item",
                "segment_id": missing_id,
                "dry_run_decision": "blocked",
                "block_reasons": ["missing_segment_state_item"],
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )
    for row in rows:
        segment_id = int(row["segment_id"])
        existing_count = existing_policy_count(conn, segment_id)
        allowed, reasons, surface = classify(row, existing_count)
        records.append(
            {
                "source": SOURCE,
                "record_type": "bridge_dry_run_item",
                "segment_id": segment_id,
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "final_state": row.get("final_state"),
                "state_group": row.get("state_group"),
                "review_state": row.get("review_state"),
                "apply_state": row.get("apply_state"),
                "active_action": row.get("active_action"),
                "policy_action": row.get("policy_action"),
                "confirmation_level": row.get("confirmation_level"),
                "confirmation_label": row.get("confirmation_label"),
                "confirmation_source": row.get("confirmation_source"),
                "locked": int(row.get("locked") or 0),
                "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
                "needs_output_apply": int(row.get("needs_output_apply") or 0),
                "canonical_output_equals_confirmed": canonical_equal(row.get("output_text"), row.get("confirmed_text")),
                "token_surface": surface,
                "open_issue_count": int(row.get("open_issue_count") or 0),
                "high_issue_count": int(row.get("high_issue_count") or 0),
                "issue_families": row.get("issue_families") or "",
                "existing_policy_item_count": existing_count,
                "policy_name": POLICY_NAME,
                "policy_action": POLICY_ACTION,
                "expected_final_state_after_materialized_segment_state": FINAL_STATE,
                "dry_run_decision": "released" if allowed else "blocked",
                "block_reasons": reasons,
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )
    return sorted(records, key=lambda item: int(item["segment_id"]))


def build_summary(records: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    released = [row for row in records if row.get("dry_run_decision") == "released"]
    blocked = [row for row in records if row.get("dry_run_decision") != "released"]
    return {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "dry_run",
        "segment_state_run_id": args.segment_state_run_id,
        "ledger_run_id": args.ledger_run_id,
        "policy_name": POLICY_NAME,
        "policy_action": POLICY_ACTION,
        "expected_final_state": FINAL_STATE,
        "record_count": len(records),
        "released_count": len(released),
        "blocked_count": len(blocked),
        "released_segment_ids": [int(row["segment_id"]) for row in released],
        "blocked_segment_ids": [int(row["segment_id"]) for row in blocked],
        "block_reason_counts": dict(Counter(reason for row in blocked for reason in row.get("block_reasons", [])).most_common()),
        "confirmation_level_counts": dict(Counter(row.get("confirmation_level") or "missing" for row in records).most_common()),
        "review_state_counts": dict(Counter(row.get("review_state") or "missing" for row in records).most_common()),
        "token_surface_counts": dict(Counter(row.get("token_surface") or "missing" for row in records).most_common()),
        "confirmed_matches_output_count": sum(1 for row in records if int(row.get("confirmed_matches_output") or 0) == 1),
        "needs_output_apply_count": sum(1 for row in records if int(row.get("needs_output_apply") or 0) == 1),
        "open_issue_count_total": sum(int(row.get("open_issue_count") or 0) for row in records),
        "high_issue_count_total": sum(int(row.get("high_issue_count") or 0) for row in records),
        "existing_policy_item_count_total": sum(int(row.get("existing_policy_item_count") or 0) for row in records),
        "expected_released_count": len(TARGET_SEGMENT_IDS),
        "guards_100_percent": len(released) == len(TARGET_SEGMENT_IDS) and not blocked,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "single_operational_recommendation": (
            "Dry-run is clean; next step may materialize this narrow bridge only after explicit approval, then run segment-state and delta."
            if len(released) == len(TARGET_SEGMENT_IDS) and not blocked
            else "Do not materialize; resolve blocked guards first."
        ),
    }


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_residual_lifecycle_bridge_dry_run"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "UI/tooltips residual lifecycle bridge dry-run",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"ledger_run_id={summary['ledger_run_id']}",
        f"policy_name={summary['policy_name']}",
        f"policy_action={summary['policy_action']}",
        f"record_count={summary['record_count']}",
        f"released_count={summary['released_count']}",
        f"blocked_count={summary['blocked_count']}",
        f"guards_100_percent={summary['guards_100_percent']}",
        f"block_reason_counts={json.dumps(summary['block_reason_counts'], ensure_ascii=False, sort_keys=True)}",
        "candidate_generation_count=0",
        "apply_count=0",
        "lifecycle_count=0",
        "segment_state_count=0",
        "reindex_count=0",
        "production_full_count=0",
        "",
        f"recommendation={summary['single_operational_recommendation']}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    with connect_readonly() as conn:
        records = build_records(conn, args)
    summary = build_summary(records, args)
    txt_path, jsonl_path, summary_path = write_reports(records, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")


if __name__ == "__main__":
    main()
