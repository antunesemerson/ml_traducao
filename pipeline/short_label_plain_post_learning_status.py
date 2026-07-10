from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "short_label_plain_post_learning_status_v1"
LEARNING_RUN_IDS = (695, 696, 697, 698)
KNOWN_HOLDS = {
    22963: "hold_custom_localization_fragment",
    22974: "hold_custom_localization_fragment",
    22977: "hold_custom_localization_fragment",
    23005: "hold_custom_localization_fragment",
    34132: "hold_truncated_or_missing_object_context",
    71234: "hold_japanese_title_possessive_policy",
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
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def latest_file(pattern: str) -> Path | None:
    paths = sorted(reports_dir().glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return paths[0] if paths else None


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def fetch_latest_segment_state(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM segment_state_runs ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        raise SystemExit("missing segment_state_runs")
    return dict(row)


def fetch_learning_runs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in LEARNING_RUN_IDS)
    return [
        dict(row)
        for row in conn.execute(
            f"SELECT * FROM local_learning_runs WHERE id IN ({placeholders}) ORDER BY id",
            LEARNING_RUN_IDS,
        )
    ]


def fetch_learning_candidates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in LEARNING_RUN_IDS)
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT
                id,
                run_id,
                segment_id,
                relative_path,
                source_key,
                suggested_text,
                source_language,
                origin,
                match_type,
                human_label,
                learned_at,
                confirmation_synced_at,
                queue_source,
                focus_group
            FROM local_learning_candidates
            WHERE run_id IN ({placeholders})
            ORDER BY run_id, segment_id
            """,
            LEARNING_RUN_IDS,
        )
    ]


def fetch_confirmations(conn: sqlite3.Connection, segment_ids: list[int]) -> list[dict[str, Any]]:
    if not segment_ids:
        return []
    placeholders = ",".join("?" for _ in segment_ids)
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT
                segment_id,
                confirmation_level,
                confirmed_text,
                confirmation_source,
                confirmation_label,
                locked,
                reviewer,
                confirmed_at,
                updated_at
            FROM segment_confirmations
            WHERE segment_id IN ({placeholders})
            ORDER BY segment_id
            """,
            tuple(segment_ids),
        )
    ]


def fetch_state_for_segments(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> list[dict[str, Any]]:
    if not segment_ids:
        return []
    placeholders = ",".join("?" for _ in segment_ids)
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT
                segment_id,
                state_group,
                final_state,
                needs_output_apply,
                confirmed_matches_output,
                review_state,
                locked
            FROM segment_state_items
            WHERE run_id = ?
              AND segment_id IN ({placeholders})
            ORDER BY segment_id
            """,
            (run_id, *segment_ids),
        )
    ]


def hold_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = []
    for segment_id, reason in KNOWN_HOLDS.items():
        source = conn.execute(
            """
            SELECT
                s.id AS segment_id,
                s.relative_path,
                s.source_key,
                s.source_line_number,
                s.english_text,
                s.spanish_text,
                o.portuguese_text AS current_output_text
            FROM source_segments s
            LEFT JOIN output_segments o ON o.segment_id = s.id
            WHERE s.id = ?
            """,
            (segment_id,),
        ).fetchone()
        item = dict(source) if source else {"segment_id": segment_id}
        item["hold_reason"] = reason
        rows.append(item)
    return rows


def build_summary() -> dict[str, Any]:
    latest_feedback = latest_file("*_apply_local_learning_feedback.txt")
    latest_review_packet = latest_file("*_short_label_plain_human_review_packet_summary.json")
    review_packet = read_json(latest_review_packet) if latest_review_packet else {}
    with connect_readonly() as conn:
        current_run = fetch_latest_segment_state(conn)
        learning_runs = fetch_learning_runs(conn)
        candidates = fetch_learning_candidates(conn)
        learned_segment_ids = sorted({int(row["segment_id"]) for row in candidates})
        all_segment_ids = learned_segment_ids + sorted(KNOWN_HOLDS)
        confirmations = fetch_confirmations(conn, all_segment_ids)
        state_rows = fetch_state_for_segments(conn, int(current_run["id"]), all_segment_ids)
        holds = hold_rows(conn)

    run_counts = Counter(int(row["run_id"]) for row in candidates)
    match_counts = Counter(str(row["match_type"]) for row in candidates)
    source_language_counts = Counter(str(row["source_language"]) for row in candidates)
    confirmation_map = {int(row["segment_id"]): row for row in confirmations}
    state_map = {int(row["segment_id"]): row for row in state_rows}
    learned_confirmation_ok = [
        segment_id
        for segment_id in learned_segment_ids
        if confirmation_map.get(segment_id, {}).get("confirmation_level") == "human_confirmed"
        and int(confirmation_map.get(segment_id, {}).get("locked") or 0) == 1
    ]
    learned_unsynced = [
        int(row["segment_id"])
        for row in candidates
        if not row.get("learned_at") or not row.get("confirmation_synced_at")
    ]
    learned_state_mismatch = [
        {
            "segment_id": segment_id,
            "state": state_map.get(segment_id),
            "confirmation": confirmation_map.get(segment_id),
        }
        for segment_id in learned_segment_ids
        if state_map.get(segment_id, {}).get("confirmed_matches_output") != 1
    ]
    needs_output_apply_segments = [
        int(row["segment_id"])
        for row in state_rows
        if int(row.get("needs_output_apply") or 0) == 1
    ]
    segment_state_recommended = bool(candidates and len(learned_state_mismatch) > 0)
    next_action = (
        "run_segment_state_only_to_measure_short_label_plain_learning_delta"
        if segment_state_recommended
        else "continue_readonly_priority_reassessment_no_segment_state_required"
    )
    return {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": int(current_run["id"]),
        "current_run": {
            "total_segments": int(current_run.get("total_segments") or 0),
            "closed_count": int(current_run.get("closed_count") or 0),
            "pending_count": int(current_run.get("pending_count") or 0),
            "output_apply_pending_count": int(current_run.get("output_apply_pending_count") or 0),
            "reopen_count": int(current_run.get("reopen_count") or 0),
        },
        "learning_run_ids": list(LEARNING_RUN_IDS),
        "learning_runs": learning_runs,
        "learned_candidate_count": len(candidates),
        "learned_segment_count": len(learned_segment_ids),
        "learned_segment_ids": learned_segment_ids,
        "learning_by_run": [{"key": str(key), "count": value} for key, value in sorted(run_counts.items())],
        "match_type_counts": [{"key": key, "count": value} for key, value in match_counts.most_common()],
        "source_language_counts": [{"key": key, "count": value} for key, value in source_language_counts.most_common()],
        "human_confirmed_locked_count": len(learned_confirmation_ok),
        "learned_unsynced_count": len(learned_unsynced),
        "learned_unsynced_segment_ids": learned_unsynced,
        "learned_state_mismatch_count": len(learned_state_mismatch),
        "learned_state_mismatches": learned_state_mismatch,
        "needs_output_apply_segments_count": len(needs_output_apply_segments),
        "needs_output_apply_segment_ids": needs_output_apply_segments,
        "hold_count": len(KNOWN_HOLDS),
        "holds": holds,
        "latest_feedback_report": str(latest_feedback) if latest_feedback else None,
        "latest_review_packet_summary": str(latest_review_packet) if latest_review_packet else None,
        "review_packet_count": review_packet.get("packet_count"),
        "review_packet_human_review_count": review_packet.get("human_review_count"),
        "review_packet_hold_count": review_packet.get("hold_count"),
        "segment_state_recommended_now": segment_state_recommended,
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "retarget_recommended_now": False,
        "discovery_recommended_now": False,
        "next_action": next_action,
    }


def write_outputs(summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_short_label_plain_post_learning_status"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in summary["learning_runs"]:
            handle.write(json.dumps({"record_type": "learning_run", **row}, ensure_ascii=False, sort_keys=True) + "\n")
        for row in summary["holds"]:
            handle.write(json.dumps({"record_type": "hold", **row}, ensure_ascii=False, sort_keys=True) + "\n")
        for row in summary["learned_state_mismatches"]:
            handle.write(json.dumps({"record_type": "state_mismatch", **row}, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "short label plain post learning status",
        f"source={SOURCE}",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"pending_count={summary['current_run']['pending_count']}",
        f"needs_output_apply={summary['current_run']['output_apply_pending_count']}",
        f"learned_candidate_count={summary['learned_candidate_count']}",
        f"learned_segment_count={summary['learned_segment_count']}",
        f"human_confirmed_locked_count={summary['human_confirmed_locked_count']}",
        f"learned_unsynced_count={summary['learned_unsynced_count']}",
        f"learned_state_mismatch_count={summary['learned_state_mismatch_count']}",
        f"needs_output_apply_segments_count={summary['needs_output_apply_segments_count']}",
        f"hold_count={summary['hold_count']}",
        "",
        "learning_by_run:",
    ]
    for item in summary["learning_by_run"]:
        lines.append(f"- {item['count']} | run {item['key']}")
    lines.extend(["", "match_type_counts:"])
    for item in summary["match_type_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(["", "holds:"])
    for row in summary["holds"]:
        lines.append(f"- {row['segment_id']} | {row['hold_reason']} | {row.get('source_key')}")
    lines.extend(
        [
            "",
            f"segment_state_recommended_now={str(summary['segment_state_recommended_now']).lower()}",
            f"apply_ready_now={summary['apply_ready_now']}",
            f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
            f"retarget_recommended_now={str(summary['retarget_recommended_now']).lower()}",
            f"discovery_recommended_now={str(summary['discovery_recommended_now']).lower()}",
            f"next_action={summary['next_action']}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    summary = build_summary()
    txt_path, jsonl_path, summary_path = write_outputs(summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"segment_state_run_id={summary['segment_state_run_id']}")
    print(f"learned_candidate_count={summary['learned_candidate_count']}")
    print(f"human_confirmed_locked_count={summary['human_confirmed_locked_count']}")
    print(f"learned_unsynced_count={summary['learned_unsynced_count']}")
    print(f"learned_state_mismatch_count={summary['learned_state_mismatch_count']}")
    print(f"needs_output_apply_segments_count={summary['needs_output_apply_segments_count']}")
    print(f"hold_count={summary['hold_count']}")
    print(f"segment_state_recommended_now={summary['segment_state_recommended_now']}")
    print(f"next_action={summary['next_action']}")


if __name__ == "__main__":
    main()
