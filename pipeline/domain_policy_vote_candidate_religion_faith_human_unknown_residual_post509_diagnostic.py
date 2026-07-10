from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_religion_faith_human_unknown_residual_post509_diagnostic_v1"
INPUT_REVIEW_JSONL = Path("reports/20260630_115930_392736_domain_policy_vote_candidate_religion_faith_human_unknown_hold_review.jsonl")
SEGMENT_STATE_RUN_ID = 509
EXPLICIT_CONTEXT_HOLD_SEGMENT_IDS = {62887}


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


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def fetch_state(conn: sqlite3.Connection, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            i.segment_id,
            i.state_group,
            i.final_state,
            i.needs_output_apply,
            i.confirmed_matches_output,
            i.confirmation_level,
            i.confirmation_label,
            i.locked,
            o.portuguese_text AS output_text,
            c.confirmed_text
        FROM segment_state_items i
        LEFT JOIN output_segments o ON o.segment_id = i.segment_id
        LEFT JOIN segment_confirmations c ON c.segment_id = i.segment_id
        WHERE i.run_id = ?
          AND i.segment_id IN ({placeholders})
        ORDER BY i.segment_id
        """,
        (SEGMENT_STATE_RUN_ID, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def operational_bucket(row: dict[str, Any], state: dict[str, Any]) -> tuple[str, str, str]:
    segment_id = int(row["segment_id"])
    subtype = str(row.get("operational_subtype") or "")
    if state.get("state_group") == "closed":
        return ("closed_excluded", "no_action_closed", "Segment is already closed in run 509.")
    if segment_id in EXPLICIT_CONTEXT_HOLD_SEGMENT_IDS:
        return ("explicit_context_hold", "keep_needs_more_context", "Human review kept this long/truncated item in context hold.")
    if subtype == "building_place_long_narrative":
        return (
            "terminal_hold_building_place_long_narrative",
            "register_or_update_terminal_hold_readonly",
            "Long building/place narrative with dynamic surface; should stay out of automatic candidate generation.",
        )
    if subtype == "dense_unknown_token_context":
        return (
            "architecture_later_dense_unknown_token_context",
            "prepare_architecture_samples_later",
            "Dense unknown token surface needs parser/policy design before humans or candidates.",
        )
    return (
        "residual_unknown",
        "hold_context",
        "Residual subtype not covered by the post-509 diagnostic.",
    )


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> None:
    source_rows = read_jsonl(INPUT_REVIEW_JSONL)
    segment_ids = sorted({int(row["segment_id"]) for row in source_rows})
    with connect_readonly() as conn:
        state_by_id = fetch_state(conn, segment_ids)

    records: list[dict[str, Any]] = []
    for row in source_rows:
        segment_id = int(row["segment_id"])
        state = state_by_id.get(segment_id)
        if state is None:
            raise SystemExit(f"missing segment_state run {SEGMENT_STATE_RUN_ID}: {segment_id}")
        bucket, action, reason = operational_bucket(row, state)
        records.append(
            {
                "record_type": "human_unknown_residual_post509_diagnostic",
                "source": SOURCE,
                "input_review": str(INPUT_REVIEW_JSONL),
                "segment_state_run_id": SEGMENT_STATE_RUN_ID,
                "segment_id": segment_id,
                "source_key": row.get("source_key"),
                "relative_path": row.get("relative_path"),
                "surface_bucket": row.get("surface_bucket"),
                "previous_operational_subtype": row.get("operational_subtype"),
                "previous_recommended_action": row.get("recommended_action"),
                "risk_bucket": row.get("risk_bucket"),
                "state_group": state.get("state_group"),
                "final_state": state.get("final_state"),
                "needs_output_apply": int(state.get("needs_output_apply") or 0),
                "confirmed_matches_output": int(state.get("confirmed_matches_output") or 0),
                "confirmation_level": state.get("confirmation_level"),
                "confirmation_label": state.get("confirmation_label"),
                "locked": int(state.get("locked") or 0),
                "operational_bucket": bucket,
                "recommended_action": action,
                "diagnostic_reason": reason,
                "current_output_text": state.get("output_text") or row.get("current_output_text"),
                "english_text": row.get("english_text"),
                "candidate_generation_allowed": False,
                "auto_apply_allowed": False,
                "lifecycle_allowed": False,
                "production_release_allowed": False,
            }
        )

    active_records = [row for row in records if row["operational_bucket"] != "closed_excluded"]
    bucket_counts = Counter(row["operational_bucket"] for row in records)
    active_bucket_counts = Counter(row["operational_bucket"] for row in active_records)
    action_counts = Counter(row["recommended_action"] for row in active_records)
    subtype_counts = Counter(row["previous_operational_subtype"] for row in active_records)
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_religion_faith_human_unknown_residual_post509_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, records)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_residual_diagnostic",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "input_review": str(INPUT_REVIEW_JSONL),
        "input_count": len(records),
        "closed_excluded_count": bucket_counts.get("closed_excluded", 0),
        "active_residual_count": len(active_records),
        "explicit_context_hold_count": active_bucket_counts.get("explicit_context_hold", 0),
        "terminal_hold_recommended_count": active_bucket_counts.get("terminal_hold_building_place_long_narrative", 0),
        "architecture_later_count": active_bucket_counts.get("architecture_later_dense_unknown_token_context", 0),
        "active_bucket_counts": dict(active_bucket_counts),
        "all_bucket_counts": dict(bucket_counts),
        "active_action_counts": dict(action_counts),
        "active_previous_subtype_counts": dict(subtype_counts),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": (
            "Register a read-only terminal hold for building_place_long_narrative, "
            "then prepare a small architecture sample for dense_unknown_token_context; keep 62887 in needs_more_context."
        ),
        "output_files": {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)},
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "domain_policy_vote_candidate religion_faith human unknown residual post-509 diagnostic",
        "",
        f"input_count: {len(records)}",
        f"closed_excluded_count: {summary['closed_excluded_count']}",
        f"active_residual_count: {summary['active_residual_count']}",
        "",
        "active_bucket_counts:",
        *[f"- {count} | {key}" for key, count in active_bucket_counts.most_common()],
        "",
        "active_action_counts:",
        *[f"- {count} | {key}" for key, count in action_counts.most_common()],
        "",
        "active_items:",
    ]
    for record in active_records:
        lines.extend(
            [
                "",
                f"## {record['segment_id']} | {record['source_key']}",
                f"- bucket: {record['operational_bucket']}",
                f"- action: {record['recommended_action']}",
                f"- reason: {record['diagnostic_reason']}",
            ]
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
