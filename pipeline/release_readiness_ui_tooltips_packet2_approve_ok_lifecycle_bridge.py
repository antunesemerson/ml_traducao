from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens
from apply_segment_state_updates import canonical_localization_text


SOURCE = "release_readiness_ui_tooltips_packet2_approve_ok_lifecycle_bridge_v1"
INPUT_JSONL = Path("reports/20260702_203723_521233_release_readiness_ui_tooltips_packet2_approve_ok_learning_ingest_apply.jsonl")
DEFAULT_SEGMENT_STATE_RUN_ID = 558
DEFAULT_LEDGER_RUN_ID = 76
EXPECTED_COUNT = 29
POLICY_NAME = "human_confirmed_ui_tooltips_residual_equal_output_lifecycle_bridge"
POLICY_ACTION = "close_reopen_human_confirmed_ui_tooltips_residual_equal_output_lifecycle"
FINAL_STATE = "closed_auto_confirmed_human_confirmed_ui_tooltips_residual_equal_output_lifecycle"
LABEL_FAMILY = "human_confirmed_ui_tooltips_packet2_approve_ok_equal_output"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run/materialize lifecycle bridge for UI/tooltips packet2 approve-ok rows.")
    parser.add_argument("--input-jsonl", type=Path, default=INPUT_JSONL)
    parser.add_argument("--segment-state-run-id", type=int, default=DEFAULT_SEGMENT_STATE_RUN_ID)
    parser.add_argument("--ledger-run-id", type=int, default=DEFAULT_LEDGER_RUN_ID)
    parser.add_argument("--expected-count", type=int, default=EXPECTED_COUNT)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def read_ids(path: Path) -> list[int]:
    ids: list[int] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                ids.append(int(json.loads(line)["segment_id"]))
    return sorted(set(ids))


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


def fetch_rows(conn: sqlite3.Connection, ids: list[int], state_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in ids)
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
        [ledger_run_id, *ids, state_run_id, *ids],
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


def classify(row: dict[str, Any], existing_count: int) -> tuple[str, list[str], str]:
    reasons: list[str] = []
    surface = token_surface(row.get("output_text") or row.get("confirmed_text"))
    if int(row.get("is_closed") or 0) != 0:
        reasons.append("already_closed")
    if row.get("final_state") != "reopen_auto_confirmed_autofix":
        reasons.append("unexpected_final_state")
    if row.get("state_group") != "pending":
        reasons.append("not_pending")
    if row.get("review_state") != "human_locked":
        reasons.append("review_state_not_human_locked")
    if row.get("confirmation_level") != "human_confirmed":
        reasons.append("not_human_confirmed")
    if int(row.get("locked") or 0) != 1:
        reasons.append("not_locked")
    if int(row.get("confirmed_matches_output") or 0) != 1:
        reasons.append("confirmed_matches_output_not_1")
    if int(row.get("needs_output_apply") or 0) != 0:
        reasons.append("needs_output_apply")
    if canonical_localization_text(row.get("output_text") or "") != canonical_localization_text(row.get("confirmed_text") or ""):
        reasons.append("canonical_output_not_confirmed")
    if int(row.get("open_issue_count") or 0) != 0:
        reasons.append("open_issue_present")
    if int(row.get("high_issue_count") or 0) != 0:
        reasons.append("high_issue_present")
    if surface not in {"plain_text", "light_token"}:
        reasons.append(f"unsupported_token_surface:{surface}")
    if existing_count:
        reasons.append("existing_active_policy_item")
    return ("released" if not reasons else "blocked"), reasons, surface


def build_records(conn: sqlite3.Connection, ids: list[int], args: argparse.Namespace) -> list[dict[str, Any]]:
    rows = fetch_rows(conn, ids, args.segment_state_run_id, args.ledger_run_id)
    found = {int(row["segment_id"]) for row in rows}
    records: list[dict[str, Any]] = []
    for missing_id in sorted(set(ids) - found):
        records.append({"segment_id": missing_id, "dry_run_decision": "blocked", "block_reasons": ["missing_segment_state_item"]})
    for row in rows:
        existing_count = existing_policy_count(conn, int(row["segment_id"]))
        decision, reasons, surface = classify(row, existing_count)
        records.append(
            {
                "source": SOURCE,
                "record_type": "bridge_item",
                "segment_id": int(row["segment_id"]),
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "final_state": row["final_state"],
                "state_group": row["state_group"],
                "review_state": row["review_state"],
                "apply_state": row["apply_state"],
                "confirmation_level": row["confirmation_level"],
                "confirmation_label": row["confirmation_label"],
                "confirmation_source": row["confirmation_source"],
                "locked": int(row["locked"] or 0),
                "confirmed_matches_output": int(row["confirmed_matches_output"] or 0),
                "needs_output_apply": int(row["needs_output_apply"] or 0),
                "canonical_output_equals_confirmed": canonical_localization_text(row.get("output_text") or "")
                == canonical_localization_text(row.get("confirmed_text") or ""),
                "token_surface": surface,
                "open_issue_count": int(row["open_issue_count"] or 0),
                "high_issue_count": int(row["high_issue_count"] or 0),
                "issue_families": row["issue_families"] or "",
                "existing_policy_item_count": existing_count,
                "policy_name": POLICY_NAME,
                "policy_action": POLICY_ACTION,
                "expected_final_state": FINAL_STATE,
                "dry_run_decision": decision,
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
        (SOURCE, POLICY_NAME, LABEL_FAMILY, len(records), len(records), str(txt_path), str(jsonl_path), started, started, started),
    )
    run_id = int(cursor.lastrowid)
    for row in records:
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
                int(row["segment_id"]),
                row["relative_path"],
                row["source_key"],
                LABEL_FAMILY,
                row["confirmation_label"],
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
                        "token_surface": row.get("token_surface"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                started,
            ),
        )
    conn.commit()
    return run_id


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_packet2_approve_ok_lifecycle_bridge_{summary['mode']}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                "UI/tooltips packet2 approve-ok lifecycle bridge",
                f"mode={summary['mode']}",
                f"policy_run_id={summary['policy_run_id'] or ''}",
                f"record_count={summary['record_count']}",
                f"released_count={summary['released_count']}",
                f"blocked_count={summary['blocked_count']}",
                f"policy_name={POLICY_NAME}",
                f"policy_action={POLICY_ACTION}",
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
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    mode = "apply" if args.apply else "dry_run"
    ids = read_ids(args.input_jsonl)
    if len(ids) != args.expected_count:
        raise SystemExit(f"expected {args.expected_count} ids, got {len(ids)}")
    with db.connect(db.load_settings()) as conn:
        records = build_records(conn, ids, args)
        released = [row for row in records if row.get("dry_run_decision") == "released"]
        blocked = [row for row in records if row.get("dry_run_decision") != "released"]
        if args.apply and blocked:
            raise SystemExit("blocked records present; refusing apply")
        policy_run_id = insert_policy(conn, released, reports_dir() / "pending.txt", reports_dir() / "pending.jsonl") if False else None
        summary = {
            "schema_version": 1,
            "source": SOURCE,
            "mode": mode,
            "input_jsonl": str(args.input_jsonl),
            "segment_state_run_id": args.segment_state_run_id,
            "ledger_run_id": args.ledger_run_id,
            "policy_name": POLICY_NAME,
            "policy_action": POLICY_ACTION,
            "expected_final_state": FINAL_STATE,
            "policy_run_id": None,
            "record_count": len(records),
            "released_count": len(released),
            "blocked_count": len(blocked),
            "released_segment_ids": [int(row["segment_id"]) for row in released],
            "block_reason_counts": dict(Counter(reason for row in blocked for reason in row.get("block_reasons", [])).most_common()),
            "token_surface_counts": dict(Counter(row.get("token_surface") for row in records).most_common()),
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
        txt_path, jsonl_path, summary_path = write_reports(records, summary)
        if args.apply:
            policy_run_id = insert_policy(conn, released, txt_path, jsonl_path)
            summary["policy_run_id"] = policy_run_id
            summary["expected_delta_after_segment_state"] = {
                "closed_count": len(released),
                "pending_count": -len(released),
                "reopen_count": -len(released),
                "output_apply_pending_count": 0,
            }
            summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            txt_path.write_text(txt_path.read_text(encoding="utf-8").replace("policy_run_id=", f"policy_run_id={policy_run_id}"), encoding="utf-8")
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"mode={mode}")
    print(f"policy_run_id={summary['policy_run_id'] or ''}")
    print(f"record_count={summary['record_count']}")
    print(f"released_count={summary['released_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
