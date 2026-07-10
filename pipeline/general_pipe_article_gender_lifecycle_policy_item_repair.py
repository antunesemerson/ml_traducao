from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "general_pipe_article_gender_lifecycle_policy_item_repair_v1"
POLICY_RUN_ID = 8
POLICY_NAME = "human_confirmed_post_apply_repair_lifecycle_bridge"
POLICY_ACTION = "close_reopen_human_confirmed_post_apply_repair_lifecycle"
TARGET_SEGMENT_IDS = (20762, 60278, 230843)
OLD_OUTPUT_MATCH_KIND = "output_confirmation_exact_match"
NEW_OUTPUT_MATCH_KIND = "output_confirmation_candidate_exact_match"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def output_paths(mode: str) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_general_pipe_article_gender_lifecycle_policy_item_repair_{mode}"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), reports_dir() / f"{base.name}_summary.json"


def readonly_conn() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def fetch_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in TARGET_SEGMENT_IDS)
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT
                run.id AS policy_run_id,
                run.policy_name,
                run.policy_status,
                item.id AS policy_item_id,
                item.segment_id,
                item.relative_path,
                item.source_key,
                item.policy_action,
                item.policy_allowed,
                item.output_match_kind,
                item.token_status,
                item.issue_count,
                item.high_issue_count
            FROM auto_confirmation_reopen_lifecycle_policy_items item
            JOIN auto_confirmation_reopen_lifecycle_policy_runs run ON run.id = item.run_id
            WHERE item.run_id = ?
              AND item.segment_id IN ({placeholders})
            ORDER BY item.segment_id
            """,
            [POLICY_RUN_ID, *TARGET_SEGMENT_IDS],
        ).fetchall()
    ]


def evaluate(row: dict[str, Any]) -> dict[str, Any]:
    guards = {
        "policy_run_matches": row.get("policy_run_id") == POLICY_RUN_ID,
        "policy_name_matches": row.get("policy_name") == POLICY_NAME,
        "policy_status_active": row.get("policy_status") == "active",
        "target_segment": int(row.get("segment_id") or 0) in TARGET_SEGMENT_IDS,
        "policy_action_matches": row.get("policy_action") == POLICY_ACTION,
        "policy_allowed": int(row.get("policy_allowed") or 0) == 1,
        "token_status_ok": row.get("token_status") == "ok",
        "issue_count_zero": int(row.get("issue_count") or 0) == 0,
        "high_issue_count_zero": int(row.get("high_issue_count") or 0) == 0,
        "old_output_match_kind": row.get("output_match_kind") == OLD_OUTPUT_MATCH_KIND,
    }
    block_reasons = [key for key, value in guards.items() if not value]
    return {
        **row,
        "guards": guards,
        "repair_allowed": not block_reasons,
        "block_reason": ";".join(block_reasons),
        "old_output_match_kind": row.get("output_match_kind"),
        "new_output_match_kind": NEW_OUTPUT_MATCH_KIND if not block_reasons else row.get("output_match_kind"),
    }


def write_reports(rows: list[dict[str, Any]], mode: str, txt_path: Path, jsonl_path: Path, summary_path: Path) -> dict[str, Any]:
    evaluated = [evaluate(row) for row in rows]
    repairable = [row for row in evaluated if row["repair_allowed"]]
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "policy_run_id": POLICY_RUN_ID,
        "target_segment_ids": list(TARGET_SEGMENT_IDS),
        "rows_found": len(evaluated),
        "repairable_count": len(repairable),
        "blocked_count": len(evaluated) - len(repairable),
        "old_output_match_kind": OLD_OUTPUT_MATCH_KIND,
        "new_output_match_kind": NEW_OUTPUT_MATCH_KIND,
        "writes_source": False,
        "writes_output": False,
        "runs_segment_state": False,
        "runs_reindex": False,
        "runs_production_full": False,
        "rows": evaluated,
    }
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in evaluated:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "General pipe article/gender lifecycle policy item repair",
        f"rule_version={RULE_VERSION}",
        f"mode={mode}",
        f"policy_run_id={POLICY_RUN_ID}",
        f"rows_found={len(evaluated)}",
        f"repairable_count={len(repairable)}",
        f"blocked_count={summary['blocked_count']}",
        f"old_output_match_kind={OLD_OUTPUT_MATCH_KIND}",
        f"new_output_match_kind={NEW_OUTPUT_MATCH_KIND}",
        "writes_source=false",
        "writes_output=false",
        "runs_segment_state=false",
        "",
        "Rows:",
    ]
    for row in evaluated:
        lines.append(
            f"- {row['segment_id']} allowed={str(row['repair_allowed']).lower()} "
            f"{row['old_output_match_kind']} -> {row['new_output_match_kind']} "
            f"block={row['block_reason'] or 'none'}"
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def apply_repair(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    evaluated = [evaluate(row) for row in rows]
    if len(evaluated) != len(TARGET_SEGMENT_IDS):
        raise SystemExit("apply blocked: not all target rows were found")
    if any(not row["repair_allowed"] for row in evaluated):
        raise SystemExit("apply blocked: at least one row is not repair_allowed")
    item_ids = [int(row["policy_item_id"]) for row in evaluated]
    placeholders = ",".join("?" for _ in item_ids)
    cursor = conn.execute(
        f"""
        UPDATE auto_confirmation_reopen_lifecycle_policy_items
        SET output_match_kind = ?
        WHERE id IN ({placeholders})
        """,
        [NEW_OUTPUT_MATCH_KIND, *item_ids],
    )
    return int(cursor.rowcount or 0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    mode = "apply" if args.apply else "dry_run"
    txt_path, jsonl_path, summary_path = output_paths(mode)

    if args.apply:
        settings = db.load_settings()
        with db.connect(settings) as conn:
            db.ensure_database(conn)
            conn.row_factory = sqlite3.Row
            rows = fetch_rows(conn)
            updated = apply_repair(conn, rows)
            summary = write_reports(rows, mode, txt_path, jsonl_path, summary_path)
            summary["updated_count"] = updated
            summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            conn.commit()
    else:
        with readonly_conn() as conn:
            rows = fetch_rows(conn)
        summary = write_reports(rows, mode, txt_path, jsonl_path, summary_path)
        summary["updated_count"] = 0
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"mode={mode}")
    print(f"rows_found={summary['rows_found']}")
    print(f"repairable_count={summary['repairable_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"updated_count={summary['updated_count']}")
    print("writes_source=false")
    print("writes_output=false")
    print("runs_segment_state=false")


if __name__ == "__main__":
    main()
