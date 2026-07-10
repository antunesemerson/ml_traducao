from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "semantic_plain_lowrisk_batch18_learning_ingest_v1"
QUEUE_SOURCE = "semantic-plain-lowrisk-human-review-batch18"
ORIGIN = "human_confirmed_semantic_plain_lowrisk_batch18"
MATCH_TYPE_ALREADY_OK = "semantic_plain_lowrisk_human_confirmed_already_ok"
REVIEWER = "user_human_review_semantic_plain_lowrisk_batch18"
FOCUS_GROUP = "semantic_plain_lowrisk_batch18"

APPROVED_ALREADY_OK_SEGMENT_IDS = [
    281304,
    36407,
    36490,
    101412,
]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def fetch_context(conn, segment_id: int):
    return conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS current_output_text,
            c.confirmed_text,
            c.confirmation_level,
            c.locked,
            state.state_group,
            state.final_state,
            state.needs_output_apply,
            state.confirmed_matches_output
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        LEFT JOIN segment_confirmations c ON c.segment_id = s.id
        LEFT JOIN segment_state_items state
          ON state.segment_id = s.id
         AND state.run_id = 446
        WHERE s.id = ?
        """,
        (segment_id,),
    ).fetchone()


def fetch_snapshot(conn, segment_ids: list[int]) -> dict[str, Any]:
    placeholders = ",".join("?" for _ in segment_ids)
    return {
        "local_learning_candidates": [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM local_learning_candidates WHERE segment_id IN ({placeholders}) ORDER BY segment_id, id",
                tuple(segment_ids),
            )
        ],
        "segment_confirmations": [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM segment_confirmations WHERE segment_id IN ({placeholders}) ORDER BY segment_id, id",
                tuple(segment_ids),
            )
        ],
        "segment_state_items_run_446": [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM segment_state_items WHERE run_id = 446 AND segment_id IN ({placeholders}) ORDER BY segment_id",
                tuple(segment_ids),
            )
        ],
    }


def existing_candidate(conn, segment_id: int, suggested_hash: str):
    return conn.execute(
        """
        SELECT id
        FROM local_learning_candidates
        WHERE segment_id = ?
          AND suggested_hash = ?
          AND queue_source = ?
          AND origin = ?
        ORDER BY id ASC
        LIMIT 1
        """,
        (segment_id, suggested_hash, QUEUE_SOURCE, ORIGIN),
    ).fetchone()


def create_run(conn, inserted_count: int, skipped_count: int, blocked_count: int, timestamp: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO local_learning_runs (
            mode, limit_count, auto_confidence_threshold, candidate_count,
            high_confidence_count, pending_human_count, status, notes,
            started_at, finished_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            QUEUE_SOURCE,
            inserted_count + skipped_count + blocked_count,
            1.0,
            inserted_count,
            inserted_count,
            0,
            "completed",
            f"{RULE_VERSION}; skipped_existing={skipped_count}; blocked={blocked_count}; no_source_output_write",
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    return int(cursor.lastrowid)


def insert_candidate(conn, run_id: int, context, timestamp: str) -> int:
    current = str(context["current_output_text"] or "")
    reasons = [
        "human_confirmed_semantic_plain_lowrisk_batch18",
        "no_source_write",
        "no_output_write",
        "approved_in_chat",
        "semantic_review_router_general_semantic_prose_low_plain_text",
    ]
    cursor = conn.execute(
        """
        INSERT INTO local_learning_candidates (
            run_id, feedback_id, suggestion_id, segment_id, relative_path,
            source_key, source_line_number, english_text, spanish_text, old_text,
            current_output_text, suggested_text, suggested_hash, source_language,
            origin, match_type, match_score, token_status, suggestion_status,
            local_confidence_score, local_status, human_label, corrected_text,
            reason, reviewer, reviewed_at, reasons_json, created_at, updated_at,
            queue_source, focus_group
        )
        VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            int(context["segment_id"]),
            context["relative_path"],
            context["source_key"],
            context["source_line_number"],
            context["english_text"],
            context["spanish_text"],
            context["old_text"],
            current,
            current,
            sha256_text(current),
            "human_already_ok",
            ORIGIN,
            MATCH_TYPE_ALREADY_OK,
            1.0,
            "ok",
            "safe",
            1.0,
            "high_confidence",
            "correct",
            "human-approved semantic plain lowrisk batch18 learning signal",
            REVIEWER,
            timestamp,
            json.dumps(reasons, ensure_ascii=True),
            timestamp,
            timestamp,
            QUEUE_SOURCE,
            FOCUS_GROUP,
        ),
    )
    return int(cursor.lastrowid)


def write_outputs(records: list[dict[str, Any]], summary: dict[str, Any], before_snapshot: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_semantic_plain_lowrisk_batch18_learning_ingest"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    snapshot_path = reports_dir() / f"{base.name}_before_snapshot.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    snapshot_path.write_text(json.dumps(before_snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "semantic plain lowrisk batch18 learning ingest",
        f"rule_version={RULE_VERSION}",
        f"run_id={summary['run_id']}",
        f"inserted_count={summary['inserted_count']}",
        f"skipped_existing_count={summary['skipped_existing_count']}",
        f"blocked_count={summary['blocked_count']}",
        f"snapshot_path={snapshot_path}",
        "source_changed=false",
        "output_changed=false",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path, snapshot_path


def main() -> None:
    timestamp = now()
    records: list[dict[str, Any]] = []
    plan: list[Any] = []
    skipped_existing_count = 0
    blocked_count = 0

    with db.connect(db.load_settings()) as conn:
        db.ensure_database(conn)
        before_snapshot = fetch_snapshot(conn, APPROVED_ALREADY_OK_SEGMENT_IDS)
        for segment_id in APPROVED_ALREADY_OK_SEGMENT_IDS:
            context = fetch_context(conn, segment_id)
            if not context:
                blocked_count += 1
                records.append({"segment_id": segment_id, "status": "blocked_missing_context"})
                continue
            current = str(context["current_output_text"] or "")
            confirmed = str(context["confirmed_text"] or "")
            reasons: list[str] = []
            if not current.strip():
                reasons.append("blank_current_output")
            if current != confirmed:
                reasons.append("output_confirmed_mismatch")
            if context["state_group"] != "pending" or context["final_state"] != "reopen_auto_confirmed_autofix":
                reasons.append("unexpected_segment_state")
            if int(context["needs_output_apply"] or 0) != 0:
                reasons.append("needs_output_apply_not_zero")
            if int(context["confirmed_matches_output"] or 0) != 1:
                reasons.append("confirmed_matches_output_not_one")
            if int(context["locked"] or 0) == 1:
                reasons.append("already_locked")
            existing = existing_candidate(conn, segment_id, sha256_text(current))
            if existing:
                skipped_existing_count += 1
                records.append({"segment_id": segment_id, "status": "skipped_existing", "existing_candidate_id": int(existing["id"])})
                continue
            if reasons:
                blocked_count += 1
                records.append({"segment_id": segment_id, "status": "blocked", "block_reasons": reasons})
                continue
            plan.append(context)

        run_id = create_run(conn, len(plan), skipped_existing_count, blocked_count, timestamp)
        for context in plan:
            candidate_id = insert_candidate(conn, run_id, context, timestamp)
            records.append(
                {
                    "segment_id": int(context["segment_id"]),
                    "candidate_id": candidate_id,
                    "run_id": run_id,
                    "status": "inserted",
                    "decision": "already_ok",
                    "human_label": "correct",
                    "current_output_text": context["current_output_text"],
                    "suggested_text": context["current_output_text"],
                }
            )
        conn.commit()

    summary = {
        "schema_version": 1,
        "rule_version": RULE_VERSION,
        "generated_at": timestamp,
        "run_id": run_id,
        "approved_already_ok_segment_ids": APPROVED_ALREADY_OK_SEGMENT_IDS,
        "inserted_count": len(plan),
        "skipped_existing_count": skipped_existing_count,
        "blocked_count": blocked_count,
        "source_changed": False,
        "output_changed": False,
        "apply_executed": False,
        "production_full_recommended_now": False,
        "next_action": "apply_local_learning_feedback_then_lifecycle_policy_materializer",
    }
    txt_path, jsonl_path, summary_path, snapshot_path = write_outputs(records, summary, before_snapshot)
    print(f"run_id={run_id}")
    print(f"inserted_count={summary['inserted_count']}")
    print(f"blocked_count={blocked_count}")
    print(f"skipped_existing_count={skipped_existing_count}")
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"snapshot={snapshot_path}")


if __name__ == "__main__":
    main()
