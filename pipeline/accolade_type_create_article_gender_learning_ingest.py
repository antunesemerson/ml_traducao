from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "accolade_type_create_article_gender_learning_ingest_v1"
QUEUE_SOURCE = "accolade-type-create-article-gender-human-review"
ORIGIN = "human_confirmed_accolade_type_create_article_gender"
MATCH_TYPE_ALREADY_OK = "accolade_type_create_article_gender_human_confirmed_already_ok"
REVIEWER = "user_human_review_accolade_type_create_article_gender"
FOCUS_GROUP = "accolade_type_create_article_gender"
SEGMENT_ID = 286


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


def latest_reassessment() -> Path:
    matches = sorted(
        reports_dir().glob("*_post_acclaimed_unlock_article_gender_policy_reassessment.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise SystemExit("missing post_acclaimed_unlock_article_gender_policy_reassessment jsonl")
    return matches[0]


def read_row(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if int(row.get("segment_id") or 0) == SEGMENT_ID:
                return row
    raise SystemExit(f"segment {SEGMENT_ID} missing from reassessment")


def fetch_context(conn):
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
            o.portuguese_text AS current_output_text
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        WHERE s.id = ?
        """,
        (SEGMENT_ID,),
    ).fetchone()


def fetch_snapshot(conn) -> dict[str, Any]:
    return {
        "local_learning_candidates": [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM local_learning_candidates WHERE segment_id = ? ORDER BY id",
                (SEGMENT_ID,),
            )
        ],
        "segment_confirmations": [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM segment_confirmations WHERE segment_id = ? ORDER BY id",
                (SEGMENT_ID,),
            )
        ],
    }


def token_guard(review_row: dict[str, Any], suggested: str) -> bool:
    tokens = [str(token) for token in review_row.get("pipe_tokens") or []]
    return all(token in suggested for token in tokens)


def existing_candidate(conn, suggested_hash: str):
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
        (SEGMENT_ID, suggested_hash, QUEUE_SOURCE, ORIGIN),
    ).fetchone()


def create_run(conn, inserted_count: int, skipped_count: int, timestamp: str) -> int:
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
            inserted_count + skipped_count,
            1.0,
            inserted_count,
            inserted_count,
            0,
            "completed",
            f"{RULE_VERSION}; skipped_or_blocked={skipped_count}",
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    return int(cursor.lastrowid)


def insert_candidate(conn, run_id: int, context, review_row: dict[str, Any], suggested: str, timestamp: str) -> int:
    reasons = [
        "human_confirmed_accolade_type_create_article_gender",
        f"policy_decision:{review_row.get('policy_decision')}",
        "pipe_tokens_preserved_by_guard",
        "no_source_write",
        "no_output_write",
        "approved_in_chat",
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
            context["current_output_text"],
            suggested,
            sha256_text(suggested),
            "human_already_ok",
            ORIGIN,
            MATCH_TYPE_ALREADY_OK,
            1.0,
            "ok",
            "safe",
            1.0,
            "high_confidence",
            "correct",
            "human-approved accolade type create article/gender already-ok signal",
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


def write_outputs(record: dict[str, Any], summary: dict[str, Any], before_snapshot: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_accolade_type_create_article_gender_learning_ingest"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    snapshot_path = reports_dir() / f"{base.name}_before_snapshot.json"
    jsonl_path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    snapshot_path.write_text(json.dumps(before_snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "accolade type create article/gender learning ingest",
        f"rule_version={RULE_VERSION}",
        f"segment_id={SEGMENT_ID}",
        f"run_id={summary['run_id']}",
        f"inserted_count={summary['inserted_count']}",
        f"skipped_existing_count={summary['skipped_existing_count']}",
        f"blocked_count={summary['blocked_count']}",
        f"snapshot_path={snapshot_path}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path, snapshot_path


def main() -> None:
    review_path = latest_reassessment()
    review_row = read_row(review_path)
    timestamp = now()
    blocked_count = 0
    skipped_existing_count = 0
    record: dict[str, Any]

    with db.connect(db.load_settings()) as conn:
        before_snapshot = fetch_snapshot(conn)
        context = fetch_context(conn)
        if not context:
            raise SystemExit(f"missing context for segment {SEGMENT_ID}")
        suggested = str(context["current_output_text"] or "")
        if not token_guard(review_row, suggested):
            blocked_count = 1
            run_id = create_run(conn, 0, blocked_count, timestamp)
            record = {"segment_id": SEGMENT_ID, "status": "blocked_token_guard", "run_id": run_id}
        else:
            existing = existing_candidate(conn, sha256_text(suggested))
            if existing:
                skipped_existing_count = 1
                run_id = create_run(conn, 0, skipped_existing_count, timestamp)
                record = {"segment_id": SEGMENT_ID, "status": "skipped_existing", "existing_candidate_id": int(existing["id"]), "run_id": run_id}
            else:
                run_id = create_run(conn, 1, 0, timestamp)
                candidate_id = insert_candidate(conn, run_id, context, review_row, suggested, timestamp)
                record = {"segment_id": SEGMENT_ID, "status": "inserted", "candidate_id": candidate_id, "run_id": run_id, "suggested_hash": sha256_text(suggested)}
        conn.commit()

    inserted_count = 1 if record["status"] == "inserted" else 0
    summary = {
        "schema_version": 1,
        "rule_version": RULE_VERSION,
        "generated_at": timestamp,
        "input_review": str(review_path),
        "run_id": run_id,
        "approved_count": 1,
        "inserted_count": inserted_count,
        "inserted_already_ok": inserted_count,
        "inserted_corrections": 0,
        "skipped_existing_count": skipped_existing_count,
        "blocked_count": blocked_count,
        "source_output_write": False,
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "next_action": "apply_local_learning_feedback_for_accolade_type_create_article_gender",
    }
    txt_path, jsonl_path, summary_path, snapshot_path = write_outputs(record, summary, before_snapshot)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"snapshot={snapshot_path}")
    print(f"run_id={run_id}")
    print(f"inserted_count={inserted_count}")
    print(f"skipped_existing_count={skipped_existing_count}")
    print(f"blocked_count={blocked_count}")
    print(f"next_action={summary['next_action']}")


if __name__ == "__main__":
    main()
