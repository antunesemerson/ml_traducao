from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "culture_religion_parameter_modifier_batch1_learning_ingest_v1"
QUEUE_SOURCE = "culture-religion-parameter-modifier-human-review-batch1"
ORIGIN = "human_confirmed_culture_religion_parameter_modifier_batch1"
MATCH_TYPE_ALREADY_OK = "culture_religion_parameter_modifier_human_confirmed_already_ok"
REVIEWER = "user_human_review_culture_religion_parameter_modifier_batch1"
FOCUS_GROUP = "culture_religion_parameter_modifier_batch1"
APPROVED_SEGMENT_IDS = [20279, 20281, 20287, 20288]


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


def latest_sample() -> Path:
    matches = sorted(
        reports_dir().glob("*_culture_religion_parameter_modifier_readonly_sample.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise SystemExit("missing culture/religion parameter modifier sample jsonl")
    return matches[0]


def read_sample_rows(path: Path) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            segment_id = int(row.get("segment_id") or 0)
            if segment_id in APPROVED_SEGMENT_IDS:
                rows[segment_id] = row
    missing = sorted(set(APPROVED_SEGMENT_IDS) - set(rows))
    if missing:
        raise SystemExit(f"approved segment ids missing from sample: {missing}")
    return rows


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
            o.portuguese_text AS current_output_text
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
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
            f"{RULE_VERSION}; skipped_existing={skipped_count}; blocked={blocked_count}",
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    return int(cursor.lastrowid)


def insert_candidate(conn, run_id: int, context, sample_row: dict[str, Any], timestamp: str) -> int:
    current = str(context["current_output_text"] or "")
    reasons = [
        "human_confirmed_culture_religion_parameter_modifier_batch1",
        f"sample_bucket:{sample_row.get('sample_bucket')}",
        f"preliminary_decision:{sample_row.get('preliminary_decision')}",
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
            "human-approved culture/religion parameter modifier already-ok signal",
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
    base = reports_dir() / f"{stamp()}_culture_religion_parameter_modifier_batch1_learning_ingest"
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
        "culture/religion parameter modifier batch1 learning ingest",
        f"rule_version={RULE_VERSION}",
        f"input_sample={summary['input_sample']}",
        f"run_id={summary['run_id']}",
        f"inserted_count={summary['inserted_count']}",
        f"skipped_existing_count={summary['skipped_existing_count']}",
        f"blocked_count={summary['blocked_count']}",
        f"snapshot_path={snapshot_path}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path, snapshot_path


def main() -> None:
    sample_path = latest_sample()
    sample_rows = read_sample_rows(sample_path)
    timestamp = now()
    records: list[dict[str, Any]] = []
    plan: list[tuple[int, Any, dict[str, Any]]] = []
    skipped_existing_count = 0
    blocked_count = 0

    with db.connect(db.load_settings()) as conn:
        db.ensure_database(conn)
        before_snapshot = fetch_snapshot(conn, APPROVED_SEGMENT_IDS)
        for segment_id in APPROVED_SEGMENT_IDS:
            context = fetch_context(conn, segment_id)
            if not context:
                blocked_count += 1
                records.append({"segment_id": segment_id, "status": "blocked_missing_context"})
                continue
            current = str(context["current_output_text"] or "")
            if not current.strip():
                blocked_count += 1
                records.append({"segment_id": segment_id, "status": "blocked_blank_current_output"})
                continue
            existing = existing_candidate(conn, segment_id, sha256_text(current))
            if existing:
                skipped_existing_count += 1
                records.append(
                    {
                        "segment_id": segment_id,
                        "status": "skipped_existing",
                        "existing_candidate_id": int(existing["id"]),
                    }
                )
                continue
            plan.append((segment_id, context, sample_rows[segment_id]))

        run_id = create_run(conn, len(plan), skipped_existing_count, blocked_count, timestamp)
        for segment_id, context, sample_row in plan:
            candidate_id = insert_candidate(conn, run_id, context, sample_row, timestamp)
            records.append(
                {
                    "segment_id": segment_id,
                    "candidate_id": candidate_id,
                    "status": "inserted",
                    "decision": "already_ok",
                    "current_output_text": context["current_output_text"],
                }
            )
        conn.commit()

    summary = {
        "rule_version": RULE_VERSION,
        "input_sample": str(sample_path),
        "run_id": run_id,
        "approved_segment_ids": APPROVED_SEGMENT_IDS,
        "inserted_count": len(plan),
        "inserted_already_ok": len(plan),
        "skipped_existing_count": skipped_existing_count,
        "blocked_count": blocked_count,
        "source_changed": False,
        "output_changed": False,
        "next_action": "apply_local_learning_feedback_for_culture_religion_parameter_modifier_batch1",
    }
    txt_path, jsonl_path, summary_path, snapshot_path = write_outputs(records, summary, before_snapshot)
    print(f"run_id={run_id}")
    print(f"inserted_count={summary['inserted_count']}")
    print(f"inserted_already_ok={summary['inserted_already_ok']}")
    print(f"blocked_count={blocked_count}")
    print(f"skipped_existing_count={skipped_existing_count}")
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"snapshot={snapshot_path}")


if __name__ == "__main__":
    main()
