from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "acclaimed_unlock_article_gender_post_learning_status_v1"
LEARNING_RUN_IDS = (701,)


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


def fetch_latest_segment_state(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM segment_state_runs ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        raise SystemExit("missing segment_state_runs")
    return dict(row)


def fetch_learning_candidates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in LEARNING_RUN_IDS)
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT id, run_id, segment_id, match_type, source_language, human_label,
                   local_status, learned_at, confirmation_synced_at
            FROM local_learning_candidates
            WHERE run_id IN ({placeholders})
            ORDER BY run_id, segment_id
            """,
            LEARNING_RUN_IDS,
        )
    ]


def fetch_confirmations(conn: sqlite3.Connection, candidate_ids: list[int]) -> list[dict[str, Any]]:
    if not candidate_ids:
        return []
    placeholders = ",".join("?" for _ in candidate_ids)
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT segment_id, confirmation_level, confirmation_source, confirmation_label,
                   locked, confidence_score, candidate_id, reviewer, confirmed_at, updated_at
            FROM segment_confirmations
            WHERE candidate_id IN ({placeholders})
            ORDER BY segment_id
            """,
            tuple(candidate_ids),
        )
    ]


def fetch_state_rows(conn: sqlite3.Connection, run_id: int, segment_ids: list[int]) -> list[dict[str, Any]]:
    if not segment_ids:
        return []
    placeholders = ",".join("?" for _ in segment_ids)
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT segment_id, state_group, final_state, needs_output_apply,
                   confirmed_matches_output, review_state, locked
            FROM segment_state_items
            WHERE run_id = ?
              AND segment_id IN ({placeholders})
            ORDER BY segment_id
            """,
            (run_id, *segment_ids),
        )
    ]


def build_summary() -> dict[str, Any]:
    latest_feedback = latest_file("*_apply_local_learning_feedback.txt")
    latest_ingest = latest_file("*_acclaimed_unlock_article_gender_batch1_learning_ingest_summary.json")
    with connect_readonly() as conn:
        current_run = fetch_latest_segment_state(conn)
        candidates = fetch_learning_candidates(conn)
        candidate_ids = [int(row["id"]) for row in candidates]
        segment_ids = sorted({int(row["segment_id"]) for row in candidates})
        confirmations = fetch_confirmations(conn, candidate_ids)
        state_rows = fetch_state_rows(conn, int(current_run["id"]), segment_ids)

    match_counts = Counter(str(row["match_type"]) for row in candidates)
    source_language_counts = Counter(str(row["source_language"]) for row in candidates)
    synced = [row for row in candidates if row.get("learned_at") and row.get("confirmation_synced_at")]
    locked_confirmations = [
        row for row in confirmations if int(row.get("locked") or 0) == 1 and float(row.get("confidence_score") or 0) == 1.0
    ]
    state_mismatches = [
        row for row in state_rows if int(row.get("confirmed_matches_output") or 0) != 1
    ]
    needs_output_apply = [int(row["segment_id"]) for row in state_rows if int(row.get("needs_output_apply") or 0) == 1]
    segment_state_recommended = bool(state_mismatches)
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
        "learned_candidate_count": len(candidates),
        "learned_segment_count": len(segment_ids),
        "match_type_counts": [{"key": key, "count": value} for key, value in match_counts.most_common()],
        "source_language_counts": [{"key": key, "count": value} for key, value in source_language_counts.most_common()],
        "learned_synced_count": len(synced),
        "human_confirmed_locked_count": len(locked_confirmations),
        "learned_state_mismatch_count": len(state_mismatches),
        "needs_output_apply_segments_count": len(needs_output_apply),
        "needs_output_apply_segment_ids": needs_output_apply,
        "latest_feedback_report": str(latest_feedback) if latest_feedback else None,
        "latest_ingest_summary": str(latest_ingest) if latest_ingest else None,
        "segment_state_recommended_now": segment_state_recommended,
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "retarget_recommended_now": False,
        "discovery_recommended_now": False,
        "next_action": (
            "run_segment_state_only_to_measure_acclaimed_unlock_article_gender_delta"
            if segment_state_recommended
            else "reassess_accolade_article_gender_pipe_token_sublane_readonly"
        ),
    }


def write_outputs(summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_acclaimed_unlock_article_gender_post_learning_status"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"record_type": "summary", **summary}, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "acclaimed unlock article/gender post learning status",
        f"source={SOURCE}",
        f"segment_state_run_id={summary['segment_state_run_id']}",
        f"pending_count={summary['current_run']['pending_count']}",
        f"needs_output_apply={summary['current_run']['output_apply_pending_count']}",
        f"learned_candidate_count={summary['learned_candidate_count']}",
        f"learned_synced_count={summary['learned_synced_count']}",
        f"human_confirmed_locked_count={summary['human_confirmed_locked_count']}",
        f"learned_state_mismatch_count={summary['learned_state_mismatch_count']}",
        f"needs_output_apply_segments_count={summary['needs_output_apply_segments_count']}",
        "",
        "match_type_counts:",
    ]
    for item in summary["match_type_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(
        [
            "",
            f"segment_state_recommended_now={str(summary['segment_state_recommended_now']).lower()}",
            f"apply_ready_now={summary['apply_ready_now']}",
            f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
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
    print(f"learned_synced_count={summary['learned_synced_count']}")
    print(f"human_confirmed_locked_count={summary['human_confirmed_locked_count']}")
    print(f"learned_state_mismatch_count={summary['learned_state_mismatch_count']}")
    print(f"needs_output_apply_segments_count={summary['needs_output_apply_segments_count']}")
    print(f"segment_state_recommended_now={summary['segment_state_recommended_now']}")
    print(f"next_action={summary['next_action']}")


if __name__ == "__main__":
    main()
