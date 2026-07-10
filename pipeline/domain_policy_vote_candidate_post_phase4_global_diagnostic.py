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


SOURCE = "domain_policy_vote_candidate_post_phase4_global_diagnostic_v1"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only global diagnostic after phase 4 closure.")
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--closure-packet-summary", type=Path, required=True)
    parser.add_argument("--closure-packet-jsonl", type=Path, required=True)
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


def token_surface(text: str | None) -> str:
    tokens = protected_tokens(text or "")
    if not tokens:
        return "plain_text"
    token_blob = " ".join(tokens)
    if "\\n" in tokens or "\n" in (text or ""):
        return "multiline"
    if "Select_CString" in token_blob or "SelectLocalization" in token_blob:
        return "dynamic_select"
    if any(part in token_blob for part in [".Get", ".Custom", "ROOT.", "scope:", "SCOPE.", "GetScriptValue"]):
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
            state.segment_id,
            state.relative_path,
            state.source_key,
            state.source_line_number,
            state.final_state,
            state.confirmation_level,
            state.confirmation_label,
            state.locked,
            state.confirmed_matches_output,
            state.needs_output_apply,
            output.portuguese_text AS output_text,
            conf.confirmed_text,
            conf.confirmation_source
        FROM segment_state_items state
        LEFT JOIN output_segments output ON output.segment_id = state.segment_id
        LEFT JOIN segment_confirmations conf ON conf.segment_id = state.segment_id
        WHERE state.run_id = ?
          AND state.final_state = 'pending_apply_confirmed'
        ORDER BY state.segment_id
        """,
        (run_id,),
    ).fetchall()
    records = []
    for row in rows:
        record = dict(row)
        record["token_surface"] = token_surface(record.get("confirmed_text") or record.get("output_text"))
        records.append(record)
    return records


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    packet_summary = read_json(args.closure_packet_summary)
    packet_rows = read_jsonl(args.closure_packet_jsonl)
    with connect_readonly() as conn:
        run = fetch_run(conn, args.run_id)
        pending_apply = fetch_pending_apply(conn, args.run_id)

    phase_counts = packet_summary.get("phase_counts") or {}
    classification_counts = packet_summary.get("classification_counts") or {}
    decision_counts = packet_summary.get("architecture_decision_counts") or {}
    hold_count = int(phase_counts.get("hold_auto_structural_surface") or 0) + int(
        phase_counts.get("hold_policy_absent_or_issue_risk") or 0
    )
    phase3_human_misc_count = int(phase_counts.get("phase_3_human_misc_equal_output_bridge") or 0)
    debug_count = int(phase_counts.get("debug_existing_policy_consumption") or 0)
    phase4_residual_count = int(phase_counts.get("phase_4_auto_confirmed_plain_or_light_bridge") or 0)

    pending_surface_counts = Counter(str(row["token_surface"]) for row in pending_apply)
    pending_path_counts = Counter(str(row["relative_path"]) for row in pending_apply)
    records: list[dict[str, Any]] = [
        {
            "source": SOURCE,
            "record_type": "decision_option",
            "option": "pending_apply_confirmed_protected_apply",
            "count": len(pending_apply),
            "gain_type": "real_output_drift_removed",
            "requires_output_write": True,
            "requires_snapshot_rollback_post_validation": True,
            "risk_note": "small batch; must use protected apply only",
            "recommendation_rank": 1,
        },
        {
            "source": SOURCE,
            "record_type": "decision_option",
            "option": "closure_debt_remaining_organization",
            "count": int(packet_summary.get("record_count") or 0),
            "phase3_human_misc_equal_output_count": phase3_human_misc_count,
            "debug_existing_policy_consumption_count": debug_count,
            "phase4_residual_count": phase4_residual_count,
            "requires_output_write": False,
            "risk_note": "read-only organization can prepare next lifecycle bridge/debug tranche",
            "recommendation_rank": 2,
        },
        {
            "source": SOURCE,
            "record_type": "decision_option",
            "option": "structural_policy_risk_holds_analysis",
            "count": hold_count,
            "requires_output_write": False,
            "risk_note": "large volume but structurally risky; needs targeted architecture/human classification",
            "recommendation_rank": 3,
        },
    ]
    for row in pending_apply:
        records.append(
            {
                "source": SOURCE,
                "record_type": "pending_apply_confirmed_candidate",
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "confirmation_level": row["confirmation_level"],
                "confirmation_source": row["confirmation_source"],
                "confirmation_label": row["confirmation_label"],
                "token_surface": row["token_surface"],
                "needs_output_apply": int(row["needs_output_apply"] or 0),
                "confirmed_matches_output": int(row["confirmed_matches_output"] or 0),
                "requires_protected_apply": True,
            }
        )

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_post_phase4_global_diagnostic",
        "run_id": args.run_id,
        "current_run": {
            "closed_count": int(run.get("closed_count") or 0),
            "pending_count": int(run.get("pending_count") or 0),
            "reopen_count": int(run.get("reopen_count") or 0),
            "output_apply_pending_count": int(run.get("output_apply_pending_count") or 0),
        },
        "closure_debt_record_count": int(packet_summary.get("record_count") or 0),
        "phase_counts": phase_counts,
        "classification_counts": classification_counts,
        "architecture_decision_counts": decision_counts,
        "pending_apply_confirmed_count": len(pending_apply),
        "pending_apply_token_surface_counts": dict(pending_surface_counts.most_common()),
        "pending_apply_top_paths": dict(pending_path_counts.most_common(20)),
        "hold_structural_or_policy_risk_count": hold_count,
        "phase3_human_misc_equal_output_count": phase3_human_misc_count,
        "debug_existing_policy_consumption_count": debug_count,
        "phase4_residual_count": phase4_residual_count,
        "recommended_next_front": "pending_apply_confirmed_protected_apply_preview",
        "single_operational_recommendation": (
            "Next safest concrete step is a read-only diff preview for the 12 pending_apply_confirmed, followed by protected apply only if all token/structure/current-output guards pass. "
            "After that, organize closure debt remaining: phase3 human misc equal-output and debug consumption are lifecycle/read-only lanes; structural holds should stay in architecture/human classification."
        ),
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
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_post_phase4_global_diagnostic_run{summary['run_id']}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Post-phase4 global diagnostic",
        f"run_id={summary['run_id']}",
        f"current_run={json.dumps(summary['current_run'], ensure_ascii=False, sort_keys=True)}",
        f"closure_debt_record_count={summary['closure_debt_record_count']}",
        f"phase_counts={json.dumps(summary['phase_counts'], ensure_ascii=False, sort_keys=True)}",
        f"pending_apply_confirmed_count={summary['pending_apply_confirmed_count']}",
        f"hold_structural_or_policy_risk_count={summary['hold_structural_or_policy_risk_count']}",
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
    print(f"pending_apply_confirmed_count={summary['pending_apply_confirmed_count']}")
    print(f"hold_structural_or_policy_risk_count={summary['hold_structural_or_policy_risk_count']}")
    print(f"closure_debt_record_count={summary['closure_debt_record_count']}")
    print(f"recommended_next_front={summary['recommended_next_front']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
