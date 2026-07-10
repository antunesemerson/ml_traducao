from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_accolade_formula_run516_segment_state_delta_v1"
FROM_RUN_ID = 515
TO_RUN_ID = 516
SEGMENT_IDS = [594, 603, 606, 614, 620, 623, 626]


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


def fetch_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM segment_state_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None or row["finished_at"] is None:
        raise SystemExit(f"segment_state_run {run_id} missing or incomplete")
    return dict(row)


def fetch_items(conn: sqlite3.Connection, run_id: int) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in SEGMENT_IDS)
    rows = conn.execute(
        f"""
        SELECT
            state.run_id,
            state.segment_id,
            state.relative_path,
            state.source_key,
            state.output_state,
            state.review_state,
            state.apply_state,
            state.final_state,
            state.state_group,
            state.confirmed_matches_output,
            state.needs_output_apply,
            state.is_closed,
            state.reasons_json,
            o.portuguese_text AS output_text,
            c.confirmed_text,
            o.portuguese_text = c.confirmed_text AS raw_output_equals_confirmed
        FROM segment_state_items state
        JOIN output_segments o ON o.segment_id = state.segment_id
        JOIN segment_confirmations c ON c.segment_id = state.segment_id
        WHERE state.run_id = ?
          AND state.segment_id IN ({placeholders})
        ORDER BY state.segment_id
        """,
        (run_id, *SEGMENT_IDS),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> None:
    with connect_readonly() as conn:
        from_run = fetch_run(conn, FROM_RUN_ID)
        to_run = fetch_run(conn, TO_RUN_ID)
        before = fetch_items(conn, FROM_RUN_ID)
        after = fetch_items(conn, TO_RUN_ID)

    missing = sorted(set(SEGMENT_IDS) - set(before) | (set(SEGMENT_IDS) - set(after)))
    if missing:
        raise SystemExit(f"missing segment ids: {missing}")

    records: list[dict[str, Any]] = []
    for segment_id in SEGMENT_IDS:
        old = before[segment_id]
        new = after[segment_id]
        raw_equal = bool(new["raw_output_equals_confirmed"])
        stale_state_mismatch = (
            raw_equal
            and int(new["confirmed_matches_output"] or 0) == 0
            and int(new["needs_output_apply"] or 0) == 1
        )
        records.append(
            {
                "source": SOURCE,
                "segment_id": segment_id,
                "relative_path": new["relative_path"],
                "source_key": new["source_key"],
                "from_final_state": old["final_state"],
                "to_final_state": new["final_state"],
                "from_state_group": old["state_group"],
                "to_state_group": new["state_group"],
                "from_confirmed_matches_output": int(old["confirmed_matches_output"] or 0),
                "to_confirmed_matches_output": int(new["confirmed_matches_output"] or 0),
                "from_needs_output_apply": int(old["needs_output_apply"] or 0),
                "to_needs_output_apply": int(new["needs_output_apply"] or 0),
                "raw_output_equals_confirmed": raw_equal,
                "stale_state_mismatch": stale_state_mismatch,
                "to_reasons_json": new["reasons_json"],
                "output_text": new["output_text"],
                "confirmed_text": new["confirmed_text"],
            }
        )

    final_counts = Counter(row["to_final_state"] for row in records)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_segment_state_delta_after_protected_apply",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "from_run_id": FROM_RUN_ID,
        "to_run_id": TO_RUN_ID,
        "from_run_finished_at": from_run["finished_at"],
        "to_run_finished_at": to_run["finished_at"],
        "global_before": {
            "closed_count": int(from_run["closed_count"] or 0),
            "pending_count": int(from_run["pending_count"] or 0),
            "output_apply_pending_count": int(from_run["output_apply_pending_count"] or 0),
            "reopen_count": int(from_run["reopen_count"] or 0),
        },
        "global_after": {
            "closed_count": int(to_run["closed_count"] or 0),
            "pending_count": int(to_run["pending_count"] or 0),
            "output_apply_pending_count": int(to_run["output_apply_pending_count"] or 0),
            "reopen_count": int(to_run["reopen_count"] or 0),
        },
        "global_delta": {
            "closed_count": int(to_run["closed_count"] or 0) - int(from_run["closed_count"] or 0),
            "pending_count": int(to_run["pending_count"] or 0) - int(from_run["pending_count"] or 0),
            "output_apply_pending_count": int(to_run["output_apply_pending_count"] or 0)
            - int(from_run["output_apply_pending_count"] or 0),
            "reopen_count": int(to_run["reopen_count"] or 0) - int(from_run["reopen_count"] or 0),
        },
        "checked_segment_count": len(records),
        "to_final_state_counts": dict(final_counts),
        "raw_output_equals_confirmed_count": sum(1 for row in records if row["raw_output_equals_confirmed"]),
        "confirmed_matches_output_ok_count": sum(1 for row in records if row["to_confirmed_matches_output"] == 1),
        "needs_output_apply_zero_count": sum(1 for row in records if row["to_needs_output_apply"] == 0),
        "stale_state_mismatch_count": sum(1 for row in records if row["stale_state_mismatch"]),
        "closed_count": sum(1 for row in records if row["to_state_group"] == "closed"),
        "validation_passed": False,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 1,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": "",
        "output_files": {},
    }
    summary["validation_passed"] = (
        summary["raw_output_equals_confirmed_count"] == len(records)
        and summary["confirmed_matches_output_ok_count"] == len(records)
        and summary["needs_output_apply_zero_count"] == len(records)
        and summary["closed_count"] == len(records)
    )
    if summary["validation_passed"]:
        summary["single_operational_recommendation"] = "Continue with lifecycle learning/materializer if needed."
    else:
        summary["single_operational_recommendation"] = (
            "Hold further apply/lifecycle for this block and ask architecture to fix segment_state_snapshot: "
            "these rows have output_text == confirmed_text but remain confirmed_matches_output=0 and needs_output_apply=1."
        )

    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_accolade_formula_run516_segment_state_delta"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, records)
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "domain_policy_vote_candidate accolade formula run516 segment-state delta",
        "",
        f"from_run_id: {FROM_RUN_ID}",
        f"to_run_id: {TO_RUN_ID}",
        f"validation_passed: {str(summary['validation_passed']).lower()}",
        "",
        "global_delta:",
        f"- closed_count: {summary['global_delta']['closed_count']}",
        f"- pending_count: {summary['global_delta']['pending_count']}",
        f"- output_apply_pending_count: {summary['global_delta']['output_apply_pending_count']}",
        f"- reopen_count: {summary['global_delta']['reopen_count']}",
        "",
        "focus:",
        f"- checked_segment_count: {summary['checked_segment_count']}",
        f"- raw_output_equals_confirmed_count: {summary['raw_output_equals_confirmed_count']}",
        f"- confirmed_matches_output_ok_count: {summary['confirmed_matches_output_ok_count']}",
        f"- needs_output_apply_zero_count: {summary['needs_output_apply_zero_count']}",
        f"- stale_state_mismatch_count: {summary['stale_state_mismatch_count']}",
        f"- closed_count: {summary['closed_count']}",
        "",
        "guards:",
        "- candidate_generation: not_run",
        "- apply: not_run",
        "- lifecycle: not_run",
        "- reindex: not_run",
        "- full_production: not_run",
        "",
        f"recommendation: {summary['single_operational_recommendation']}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
