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


SOURCE = "domain_policy_vote_candidate_post_debug_consumption_global_decision_v1"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only decision diagnostic after debug consumption checkpoint.")
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--closure-packet-jsonl", type=Path, required=True)
    parser.add_argument("--closure-packet-summary", type=Path, required=True)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(db.project_path(path).read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def token_surface(text: str) -> str:
    tokens = protected_tokens(text or "")
    if not tokens:
        return "plain_text"
    token_blob = " ".join(tokens)
    if "\\n" in tokens:
        return "multiline"
    if "Select_CString" in token_blob or "SelectLocalization" in token_blob:
        return "dynamic_select"
    if any(part in token_blob for part in [".Get", ".Custom", "ROOT.", "scope:", "GetScriptValue"]):
        return "dynamic_getter"
    return "light_token"


def fetch_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM segment_state_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise SystemExit(f"missing segment_state_run {run_id}")
    return dict(row)


def fetch_pending_apply(conn: sqlite3.Connection, run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.source_line_number,
            item.final_state,
            item.confirmation_level,
            item.confirmation_label,
            item.locked,
            item.confirmed_matches_output,
            item.needs_output_apply,
            item.policy_action,
            item.lifecycle_policy_allowed,
            s.english_text,
            s.spanish_text,
            output.portuguese_text AS output_text,
            conf.confirmed_text,
            conf.confirmation_source
        FROM segment_state_items item
        JOIN source_segments s ON s.id = item.segment_id
        LEFT JOIN output_segments output ON output.segment_id = item.segment_id
        LEFT JOIN segment_confirmations conf ON conf.segment_id = item.segment_id
        WHERE item.run_id = ?
          AND item.final_state = 'pending_apply_confirmed'
        ORDER BY item.segment_id
        """,
        (run_id,),
    ).fetchall()
    records: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        record["token_surface"] = token_surface(str(record.get("confirmed_text") or record.get("output_text") or ""))
        records.append(record)
    return records


def compact_phase_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "segment_id": row.get("segment_id"),
        "phase": row.get("phase"),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "token_surface": row.get("token_surface"),
        "confirmation_level": row.get("confirmation_level"),
        "confirmation_source": row.get("confirmation_source"),
        "confirmation_label": row.get("confirmation_label"),
        "open_issue_count": row.get("open_issue_count"),
        "high_issue_count": row.get("high_issue_count"),
        "lifecycle_policy_allowed": row.get("lifecycle_policy_allowed"),
        "lifecycle_policy_action": row.get("lifecycle_policy_action"),
    }


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    packet_summary = read_json(args.closure_packet_summary)
    packet_rows = read_jsonl(args.closure_packet_jsonl)

    with connect_readonly() as conn:
        run = fetch_run(conn, args.run_id)
        pending_apply = fetch_pending_apply(conn, args.run_id)

    phase_rows = {
        "debug_existing_policy_consumption": [
            row for row in packet_rows if row.get("phase") == "debug_existing_policy_consumption"
        ],
        "phase_4_auto_confirmed_plain_or_light_bridge": [
            row for row in packet_rows if row.get("phase") == "phase_4_auto_confirmed_plain_or_light_bridge"
        ],
        "hold_auto_structural_surface": [
            row for row in packet_rows if row.get("phase") == "hold_auto_structural_surface"
        ],
        "hold_policy_absent_or_issue_risk": [
            row for row in packet_rows if row.get("phase") == "hold_policy_absent_or_issue_risk"
        ],
        "phase_3_human_misc_equal_output_bridge": [
            row for row in packet_rows if row.get("phase") == "phase_3_human_misc_equal_output_bridge"
        ],
    }

    records: list[dict[str, Any]] = []
    for phase, rows in phase_rows.items():
        surface_counts = Counter(str(row.get("token_surface")) for row in rows)
        label_counts = Counter(str(row.get("confirmation_label")) for row in rows)
        records.append(
            {
                "source": SOURCE,
                "record_type": "closure_debt_phase_summary",
                "phase": phase,
                "count": len(rows),
                "token_surface_counts": dict(surface_counts.most_common()),
                "top_confirmation_labels": dict(label_counts.most_common(20)),
                "sample_segments": [compact_phase_record(row) for row in rows[:10]],
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )

    pending_surface_counts = Counter(str(row.get("token_surface")) for row in pending_apply)
    pending_label_counts = Counter(str(row.get("confirmation_label")) for row in pending_apply)
    records.append(
        {
            "source": SOURCE,
            "record_type": "pending_apply_confirmed_real_summary",
            "phase": "pending_apply_confirmed_real",
            "count": len(pending_apply),
            "token_surface_counts": dict(pending_surface_counts.most_common()),
            "top_confirmation_labels": dict(pending_label_counts.most_common(20)),
            "sample_segments": [
                {
                    "segment_id": row.get("segment_id"),
                    "relative_path": row.get("relative_path"),
                    "source_key": row.get("source_key"),
                    "token_surface": row.get("token_surface"),
                    "confirmation_level": row.get("confirmation_level"),
                    "confirmation_source": row.get("confirmation_source"),
                    "confirmation_label": row.get("confirmation_label"),
                    "policy_action": row.get("policy_action"),
                    "confirmed_matches_output": row.get("confirmed_matches_output"),
                    "needs_output_apply": row.get("needs_output_apply"),
                }
                for row in pending_apply
            ],
            "candidate_generation_count": 0,
            "apply_count": 0,
            "lifecycle_count": 0,
            "segment_state_count": 0,
            "reindex_count": 0,
            "production_full_count": 0,
        }
    )

    phase4_count = len(phase_rows["phase_4_auto_confirmed_plain_or_light_bridge"])
    pending_apply_count = len(pending_apply)
    debug_count = len(phase_rows["debug_existing_policy_consumption"])
    if debug_count > phase4_count:
        recommendation = (
            "Prefer a read-only debug_existing_policy_consumption tranche first, because 280 rows still have policy consumption debt. "
            "If choosing strictly between phase 4 and pending_apply_confirmed, phase 4 is the safer next measurement: it is read-only, higher gain, and does not write output. "
            "Keep the 12 pending_apply_confirmed for a separate protected apply cycle with diff preview, snapshots, rollback, and post-validation."
        )
        next_front = "debug_existing_policy_consumption_then_phase_4"
    elif phase4_count >= pending_apply_count:
        recommendation = (
            "Prefer phase_4_auto_confirmed_plain_or_light read-only dry-run before pending_apply_confirmed. "
            "It has larger potential closure gain and avoids output writes."
        )
        next_front = "phase_4_auto_confirmed_plain_or_light"
    else:
        recommendation = (
            "Prefer pending_apply_confirmed only if the goal is to remove real output drift now; it requires protected apply and post-validation."
        )
        next_front = "pending_apply_confirmed_protected_apply"

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_post_debug_consumption_global_decision",
        "run_id": args.run_id,
        "current_run": {
            "closed_count": int(run.get("closed_count") or 0),
            "pending_count": int(run.get("pending_count") or 0),
            "reopen_count": int(run.get("reopen_count") or 0),
            "output_apply_pending_count": int(run.get("output_apply_pending_count") or 0),
        },
        "closure_packet_summary": str(args.closure_packet_summary),
        "closure_packet_record_count": int(packet_summary.get("record_count") or 0),
        "phase_counts": {phase: len(rows) for phase, rows in phase_rows.items()},
        "pending_apply_confirmed_real_count": pending_apply_count,
        "phase_4_auto_confirmed_plain_or_light_count": phase4_count,
        "debug_existing_policy_consumption_remaining_count": debug_count,
        "hold_count": len(phase_rows["hold_auto_structural_surface"])
        + len(phase_rows["hold_policy_absent_or_issue_risk"]),
        "decision_axis": {
            "phase_4_gain_potential": phase4_count,
            "phase_4_requires_output_write": False,
            "pending_apply_confirmed_gain_potential": pending_apply_count,
            "pending_apply_confirmed_requires_output_write": True,
            "debug_consumption_remaining_gain_potential": debug_count,
        },
        "recommended_next_front": next_front,
        "single_operational_recommendation": recommendation,
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
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_post_debug_consumption_global_decision_run{summary['run_id']}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Post-debug-consumption global decision",
        f"run_id={summary['run_id']}",
        f"current_run={json.dumps(summary['current_run'], ensure_ascii=False, sort_keys=True)}",
        f"phase_counts={json.dumps(summary['phase_counts'], ensure_ascii=False, sort_keys=True)}",
        f"pending_apply_confirmed_real_count={summary['pending_apply_confirmed_real_count']}",
        f"recommended_next_front={summary['recommended_next_front']}",
        "",
        "Recommendation:",
        summary["single_operational_recommendation"],
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records, summary = build(args)
    txt_path, jsonl_path, summary_path = write_reports(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"recommended_next_front={summary['recommended_next_front']}")
    print(f"phase_4_auto_confirmed_plain_or_light_count={summary['phase_4_auto_confirmed_plain_or_light_count']}")
    print(f"pending_apply_confirmed_real_count={summary['pending_apply_confirmed_real_count']}")
    print(f"debug_existing_policy_consumption_remaining_count={summary['debug_existing_policy_consumption_remaining_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
