from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "holy_site_effect_name_singular_token_lifecycle_delta_v1"
SEGMENT_IDS = [237388, 239477, 239479, 239507, 239509, 239511]
EXPECTED_CLOSED_STATE = "closed_auto_confirmed_human_confirmed_post_apply_repair_lifecycle"


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


def fetch_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM segment_state_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise SystemExit(f"missing segment_state_run_id={run_id}")
    return dict(row)


def fetch_records(conn: sqlite3.Connection, before_run_id: int, after_run_id: int) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in SEGMENT_IDS)
    rows = conn.execute(
        f"""
        SELECT
            before.segment_id,
            before.relative_path,
            before.source_key,
            before.final_state AS before_final_state,
            after.final_state AS after_final_state,
            before.state_group AS before_state_group,
            after.state_group AS after_state_group,
            before.needs_output_apply AS before_needs_output_apply,
            after.needs_output_apply AS after_needs_output_apply,
            before.confirmed_matches_output AS before_confirmed_matches_output,
            after.confirmed_matches_output AS after_confirmed_matches_output,
            before.is_closed AS before_is_closed,
            after.is_closed AS after_is_closed,
            after.lifecycle_policy_allowed,
            after.lifecycle_policy_action,
            o.portuguese_text AS output_text,
            c.confirmed_text
        FROM segment_state_items before
        JOIN segment_state_items after
          ON after.segment_id = before.segment_id
         AND after.run_id = ?
        JOIN output_segments o ON o.segment_id = before.segment_id
        JOIN segment_confirmations c ON c.segment_id = before.segment_id
        WHERE before.run_id = ?
          AND before.segment_id IN ({placeholders})
        ORDER BY before.segment_id
        """,
        (after_run_id, before_run_id, *SEGMENT_IDS),
    ).fetchall()
    if len(rows) != len(SEGMENT_IDS):
        raise SystemExit(f"record count guard failed: {len(rows)}")
    records: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        ok = (
            item["before_final_state"] == "reopen_auto_confirmed_autofix"
            and item["after_final_state"] == EXPECTED_CLOSED_STATE
            and int(item["before_needs_output_apply"] or 0) == 0
            and int(item["after_needs_output_apply"] or 0) == 0
            and int(item["before_confirmed_matches_output"] or 0) == 1
            and int(item["after_confirmed_matches_output"] or 0) == 1
            and int(item["after_is_closed"] or 0) == 1
            and item["output_text"] == item["confirmed_text"]
        )
        item.update(
            {
                "source": SOURCE,
                "before_run_id": before_run_id,
                "after_run_id": after_run_id,
                "expected_closed_state": EXPECTED_CLOSED_STATE,
                "validation_ok": ok,
            }
        )
        records.append(item)
    return records


def fetch_remaining_pending_apply(conn: sqlite3.Connection, after_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT segment_id, relative_path, source_key, final_state, needs_output_apply,
               confirmed_matches_output, confirmation_level, confirmation_label, locked
        FROM segment_state_items
        WHERE run_id = ?
          AND needs_output_apply = 1
        ORDER BY segment_id
        """,
        (after_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def write_reports(
    before_run: dict[str, Any],
    after_run: dict[str, Any],
    records: list[dict[str, Any]],
    remaining: list[dict[str, Any]],
    before_run_id: int,
    after_run_id: int,
) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_holy_site_effect_name_singular_token_lifecycle_delta_{before_run_id}_{after_run_id}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    transition_counts = Counter(f"{record['before_final_state']} -> {record['after_final_state']}" for record in records)
    remaining_state_counts = Counter(str(row["final_state"]) for row in remaining)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "before_run_id": before_run_id,
        "after_run_id": after_run_id,
        "global_delta": {
            "closed_count": int(after_run["closed_count"] or 0) - int(before_run["closed_count"] or 0),
            "pending_count": int(after_run["pending_count"] or 0) - int(before_run["pending_count"] or 0),
            "output_apply_pending_count": int(after_run["output_apply_pending_count"] or 0)
            - int(before_run["output_apply_pending_count"] or 0),
            "reopen_count": int(after_run["reopen_count"] or 0) - int(before_run["reopen_count"] or 0),
        },
        "focus_segment_count": len(records),
        "focus_validation_ok_count": sum(1 for record in records if record["validation_ok"]),
        "focus_closed_count": sum(1 for record in records if int(record["after_is_closed"] or 0) == 1),
        "transition_counts": dict(sorted(transition_counts.items())),
        "remaining_needs_output_apply_count": len(remaining),
        "remaining_needs_output_apply_state_counts": dict(sorted(remaining_state_counts.items())),
        "remaining_needs_output_apply_segment_ids": [int(row["segment_id"]) for row in remaining],
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": (
            "Return to closure debt architecture planning; keep the 12 remaining needs_output_apply items on protected apply/hold tracks."
        ),
    }
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps({"record_type": "focus_delta", **record}, ensure_ascii=False, sort_keys=True) + "\n")
        for row in remaining:
            handle.write(json.dumps({"record_type": "remaining_needs_output_apply", **row}, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        f"Holy-site lifecycle delta {before_run_id} -> {after_run_id}",
        f"focus_segment_count={summary['focus_segment_count']}",
        f"focus_validation_ok_count={summary['focus_validation_ok_count']}",
        f"focus_closed_count={summary['focus_closed_count']}",
        f"remaining_needs_output_apply_count={summary['remaining_needs_output_apply_count']}",
        f"global_delta={json.dumps(summary['global_delta'], ensure_ascii=False, sort_keys=True)}",
        "",
        "Transitions:",
    ]
    for key, count in sorted(transition_counts.items()):
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Remaining needs_output_apply segment_ids:"])
    lines.append(", ".join(str(row["segment_id"]) for row in remaining))
    lines.extend(
        [
            "",
            "Guards:",
            "candidate_generation_count=0",
            "apply_count=0",
            "lifecycle_count=0",
            "reindex_count=0",
            "production_full_count=0",
            "",
            "Recommendation:",
            summary["single_operational_recommendation"],
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before-run-id", type=int, required=True)
    parser.add_argument("--after-run-id", type=int, required=True)
    args = parser.parse_args()
    with connect_readonly() as conn:
        before_run = fetch_run(conn, args.before_run_id)
        after_run = fetch_run(conn, args.after_run_id)
        records = fetch_records(conn, args.before_run_id, args.after_run_id)
        remaining = fetch_remaining_pending_apply(conn, args.after_run_id)
    txt_path, jsonl_path, summary_path = write_reports(
        before_run,
        after_run,
        records,
        remaining,
        args.before_run_id,
        args.after_run_id,
    )
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"focus_validation_ok_count={sum(1 for record in records if record['validation_ok'])}")
    print(f"remaining_needs_output_apply_count={len(remaining)}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
