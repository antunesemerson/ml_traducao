from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_policy_item_absent_materializer_v1"
INPUT_JSONL = Path("reports/20260701_220631_549784_domain_policy_vote_candidate_policy_item_absent_materialization_dry_run_9.jsonl")
CURRENT_RUN_ID = 535
EXPECTED_SEGMENT_COUNT = 9
EXPECTED_ITEM_COUNT = 10
POLICY_NAME = "policy_item_absent_boundary_same_token_backfill"
LABEL_FAMILY = "domain_policy_vote_candidate_policy_item_absent_backfill"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Materialize missing lifecycle policy items for reviewed policy_item_absent rows.")
    parser.add_argument("--input-jsonl", type=Path, default=INPUT_JSONL)
    parser.add_argument("--current-run-id", type=int, default=CURRENT_RUN_ID)
    parser.add_argument("--expected-segment-count", type=int, default=EXPECTED_SEGMENT_COUNT)
    parser.add_argument("--expected-item-count", type=int, default=EXPECTED_ITEM_COUNT)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db.get_database_path(db.load_settings()), timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def canonical_l10n(text: str | None) -> str:
    value = str(text or "")
    value = value.replace("\\\"", '"')
    value = value.replace("\\n", "\n")
    return value.strip()


def fetch_current(conn: sqlite3.Connection, segment_ids: list[int], run_id: int) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
          s.id AS segment_id,
          s.relative_path,
          s.source_line_number,
          s.source_key,
          st.final_state,
          st.confirmed_matches_output,
          st.needs_output_apply,
          c.confirmed_text,
          c.confirmation_label,
          o.portuguese_text AS output_text
        FROM source_segments s
        JOIN segment_state_items st ON st.segment_id = s.id AND st.run_id = ?
        LEFT JOIN segment_confirmations c ON c.segment_id = s.id
        LEFT JOIN output_segments o ON o.segment_id = s.id
        WHERE s.id IN ({placeholders})
        """,
        (run_id, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def existing_allowed_items(conn: sqlite3.Connection, segment_id: int, action: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM auto_confirmation_reopen_lifecycle_policy_items
        WHERE segment_id = ?
          AND policy_action = ?
          AND policy_allowed = 1
        """,
        (segment_id, action),
    ).fetchone()
    return int(row["count"] or 0)


def validate_row(row: dict[str, Any], current: dict[str, Any], conn: sqlite3.Connection) -> list[str]:
    failures: list[str] = []
    actions = list(row.get("proposed_policy_actions") or [])
    if not actions:
        failures.append("missing_proposed_policy_actions")
    if current.get("final_state") != "reopen_auto_confirmed_autofix":
        failures.append("current_state_not_reopen_auto_confirmed_autofix")
    if int(current.get("confirmed_matches_output") or 0) != 1:
        failures.append("confirmed_matches_output_not_1")
    if int(current.get("needs_output_apply") or 0) != 0:
        failures.append("needs_output_apply_not_0")
    if int(row.get("open_issue_count") or 0) != 0:
        failures.append("open_issue_count_not_0")
    if int(row.get("high_issue_count") or 0) != 0:
        failures.append("high_issue_count_not_0")
    if canonical_l10n(current.get("output_text")) != canonical_l10n(current.get("confirmed_text")):
        failures.append("canonical_output_confirmed_mismatch")
    if row.get("token_status") != "ok":
        failures.append("token_status_not_ok")
    for action in actions:
        if existing_allowed_items(conn, int(row["segment_id"]), str(action)):
            failures.append(f"duplicate_allowed_policy_item:{action}")
    return failures


def build_records(rows: list[dict[str, Any]], current_rows: dict[int, dict[str, Any]], conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    items: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for row in rows:
        sid = int(row["segment_id"])
        current = current_rows.get(sid, {})
        failures = validate_row(row, current, conn)
        if failures:
            blocked.append({"segment_id": sid, "failures": failures, "row": row})
            continue
        for action in row["proposed_policy_actions"]:
            items.append(
                {
                    "segment_id": sid,
                    "relative_path": current.get("relative_path") or row.get("relative_path"),
                    "source_key": current.get("source_key") or row.get("source_key"),
                    "source_line_number": current.get("source_line_number"),
                    "label_family": LABEL_FAMILY,
                    "confirmation_label": current.get("confirmation_label"),
                    "policy_action": str(action),
                    "policy_allowed": 1,
                    "block_reason": None,
                    "output_match_kind": "canonical_equal",
                    "token_status": "ok",
                    "issue_count": 0,
                    "high_issue_count": 0,
                    "model_safe_probability": 1.0,
                    "review_priority": 0,
                    "reasons_json": json.dumps(
                        {
                            "source": SOURCE,
                            "input_segment_decision_mode": row.get("decision_mode"),
                            "materialized_from": str(INPUT_JSONL),
                            "guarded_current_run_id": CURRENT_RUN_ID,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                }
            )
    return items, blocked


def write_reports(summary: dict[str, Any], items: list[dict[str, Any]], blocked: list[dict[str, Any]]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_policy_item_absent_materializer_{'apply' if summary['apply'] else 'dry_run'}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write(json.dumps({"record_type": "policy_item", **item}, ensure_ascii=False, sort_keys=True) + "\n")
        for item in blocked:
            handle.write(json.dumps({"record_type": "blocked", **item}, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Policy item absent materializer",
        f"apply={summary['apply']}",
        f"policy_run_id={summary.get('policy_run_id')}",
        f"segment_count={summary['segment_count']}",
        f"item_count={summary['item_count']}",
        f"blocked_count={summary['blocked_count']}",
        "",
        "Guards:",
        "confirmed_matches_output=1",
        "needs_output_apply=0",
        "open/high issue=0",
        "canonical output == confirmed",
        "token_status=ok",
        "current_state=reopen_auto_confirmed_autofix",
        "",
        "Operation counters:",
        "candidate_generation_count=0",
        "apply_count=0",
        "lifecycle_count=0",
        "segment_state_count=0",
        "reindex_count=0",
        "production_full_count=0",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    rows = read_jsonl(args.input_jsonl)
    if len(rows) != args.expected_segment_count:
        raise SystemExit(f"segment count guard failed: {len(rows)}")
    segment_ids = [int(row["segment_id"]) for row in rows]
    with connect() as conn:
        current_rows = fetch_current(conn, segment_ids, args.current_run_id)
        items, blocked = build_records(rows, current_rows, conn)
        if len(items) != args.expected_item_count:
            raise SystemExit(f"item count guard failed: {len(items)}")
        if blocked:
            raise SystemExit(f"blocked guard failed: {len(blocked)}")
        policy_run_id = None
        if args.apply:
            started = now_iso()
            cur = conn.execute(
                """
                INSERT INTO auto_confirmation_reopen_lifecycle_policy_runs (
                  rule_version, queue_run_id, audit_run_id, policy_name, label_family, policy_status,
                  candidate_count, released_count, blocked_count, manual_boundary_count, invalid_count,
                  report_path, csv_path, jsonl_path, started_at, finished_at, updated_at
                ) VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?)
                """,
                (
                    SOURCE,
                    POLICY_NAME,
                    LABEL_FAMILY,
                    "released",
                    args.expected_segment_count,
                    args.expected_segment_count,
                    0,
                    1,
                    0,
                    started,
                    started,
                    started,
                ),
            )
            policy_run_id = int(cur.lastrowid)
            for item in items:
                conn.execute(
                    """
                    INSERT INTO auto_confirmation_reopen_lifecycle_policy_items (
                      run_id, queue_run_id, queue_item_id, audit_run_id, audit_item_id,
                      segment_id, relative_path, source_key, source_line_number, label_family,
                      confirmation_label, policy_action, policy_allowed, block_reason,
                      output_match_kind, token_status, issue_count, high_issue_count,
                      model_safe_probability, review_priority, reasons_json, created_at
                    ) VALUES (?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        policy_run_id,
                        item["segment_id"],
                        item["relative_path"],
                        item["source_key"],
                        item["source_line_number"],
                        item["label_family"],
                        item["confirmation_label"],
                        item["policy_action"],
                        item["policy_allowed"],
                        item["block_reason"],
                        item["output_match_kind"],
                        item["token_status"],
                        item["issue_count"],
                        item["high_issue_count"],
                        item["model_safe_probability"],
                        item["review_priority"],
                        item["reasons_json"],
                        started,
                    ),
                )
            conn.commit()
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "apply" if args.apply else "dry_run",
        "apply": bool(args.apply),
        "policy_name": POLICY_NAME,
        "policy_run_id": policy_run_id,
        "current_run_id": args.current_run_id,
        "segment_count": len(rows),
        "item_count": len(items),
        "blocked_count": len(blocked),
        "boundary_item_count": sum(1 for item in items if item["policy_action"] == "close_reopen_boundary_token_subpolicy_controlled"),
        "same_token_item_count": sum(1 for item in items if item["policy_action"] == "close_reopen_same_token_boundary_repair_controlled"),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
    }
    txt, jsonl, summary_path = write_reports(summary, items, blocked)
    print(f"txt={txt}")
    print(f"jsonl={jsonl}")
    print(f"summary={summary_path}")
    print(f"policy_run_id={policy_run_id}")
    print(f"segment_count={summary['segment_count']}")
    print(f"item_count={summary['item_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
