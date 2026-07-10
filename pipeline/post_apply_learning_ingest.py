from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "post_apply_learning_ingest_v1"
QUEUE_SOURCE = "post-apply-protected"
ORIGIN = "candidate_apply_protected"
REVIEWER = "candidate_apply_post_validation"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def project_path(value: str) -> Path:
    return db.project_path(value)


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_apply_rows(rows: list[dict[str, Any]], post_summary: dict[str, Any] | None, expected_count: int) -> list[str]:
    notes: list[str] = []
    if len(rows) != expected_count:
        notes.append(f"expected {expected_count} apply rows, got {len(rows)}")
    for row in rows:
        index = row.get("candidate_index", row.get("segment_id"))
        if not row.get("applied"):
            notes.append(f"candidate {index}: not applied")
        if "apply_decision" in row and row.get("apply_decision") != "applied":
            notes.append(f"candidate {index}: apply_decision is {row.get('apply_decision')!r}")
        if not row.get("token_integrity_ok"):
            notes.append(f"candidate {index}: token_integrity_ok is false")
        if not row.get("structure_integrity_ok"):
            notes.append(f"candidate {index}: structure_integrity_ok is false")
        if row.get("rollback_executed"):
            notes.append(f"candidate {index}: rollback_executed is true")
        if not str(row.get("candidate_text") or "").strip():
            notes.append(f"candidate {index}: candidate_text is empty")
    if post_summary:
        expected = {
            "applied_count": expected_count,
            "post_validation_fail_count": 0,
        }
        for key, expected_value in expected.items():
            if key in post_summary and post_summary.get(key) != expected_value:
                notes.append(f"post summary {key} expected {expected_value!r}, got {post_summary.get(key)!r}")
    return notes


def create_run(conn, inserted_count: int, skipped_count: int, timestamp: str, dry_run: bool) -> int | None:
    if dry_run:
        return None
    cursor = conn.execute(
        """
        INSERT INTO local_learning_runs (
            mode,
            limit_count,
            auto_confidence_threshold,
            candidate_count,
            high_confidence_count,
            pending_human_count,
            status,
            notes,
            started_at,
            finished_at,
            updated_at
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
            f"{RULE_VERSION}; skipped_duplicates={skipped_count}",
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    return int(cursor.lastrowid)


def existing_candidate(conn, segment_id: int, suggested_hash: str):
    return conn.execute(
        """
        SELECT id, learned_at, confirmation_synced_at
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


def fetch_segment_context(conn, segment_id: int):
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
            o.portuguese_text AS current_indexed_output_text
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        WHERE s.id = ?
        """,
        (segment_id,),
    ).fetchone()


def insert_candidate(conn, run_id: int, row: dict[str, Any], context, suggested_hash: str, timestamp: str) -> int:
    reasons = [
        "post_apply_validated",
        "human_review_approved_for_future_apply",
        "protected_apply_applied",
        "token_integrity_ok",
        "structure_integrity_ok",
        f"candidate_index:{row.get('candidate_index', row['segment_id'])}",
        f"target_file:{row['target_file']}",
        f"target_line_number:{row['target_line_number']}",
    ]
    cursor = conn.execute(
        """
        INSERT INTO local_learning_candidates (
            run_id,
            feedback_id,
            suggestion_id,
            segment_id,
            relative_path,
            source_key,
            source_line_number,
            english_text,
            spanish_text,
            old_text,
            current_output_text,
            suggested_text,
            suggested_hash,
            source_language,
            origin,
            match_type,
            match_score,
            token_status,
            suggestion_status,
            local_confidence_score,
            local_status,
            human_label,
            corrected_text,
            reason,
            reviewer,
            reviewed_at,
            reasons_json,
            created_at,
            updated_at,
            queue_source,
            focus_group
        )
        VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            int(row["segment_id"]),
            context["relative_path"],
            context["source_key"],
            context["source_line_number"],
            context["english_text"],
            context["spanish_text"],
            context["old_text"],
            row.get("current_output_text") or context["current_indexed_output_text"],
            row["candidate_text"],
            suggested_hash,
            "post_apply",
            ORIGIN,
            "protected_human_approved",
            1.0,
            "ok",
            "safe",
            1.0,
            "high_confidence",
            "correct",
            "protected post-apply candidate validated and ingested as positive learning signal",
            REVIEWER,
            timestamp,
            json.dumps(reasons, ensure_ascii=True),
            timestamp,
            timestamp,
            QUEUE_SOURCE,
            "all",
        ),
    )
    return int(cursor.lastrowid)


def write_outputs(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_post_apply_learning_ingest"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "post apply learning ingest",
        f"rule_version={RULE_VERSION}",
        "",
        f"inserted={summary['inserted_count']}",
        f"skipped_existing={summary['skipped_existing_count']}",
        f"missing_context={summary['missing_context_count']}",
        f"validation_note_count={summary['validation_note_count']}",
        f"dry_run={summary['dry_run']}",
        "",
        "status counts:",
        *[f"- {key}: {value}" for key, value in sorted(summary["status_counts"].items())],
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest validated protected apply rows as positive local learning signal.")
    parser.add_argument("--apply-jsonl", required=True)
    parser.add_argument("--post-validation-summary-json", default="")
    parser.add_argument("--expected-count", type=int, default=12)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    apply_rows = read_jsonl(project_path(args.apply_jsonl))
    post_summary = read_json(project_path(args.post_validation_summary_json)) if args.post_validation_summary_json else None
    validation_notes = validate_apply_rows(apply_rows, post_summary, args.expected_count)
    if validation_notes:
        raise SystemExit("; ".join(validation_notes))

    settings = db.load_settings()
    timestamp = now()
    records: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    pending_inserts: list[tuple[dict[str, Any], Any, str]] = []

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        for row in sorted(apply_rows, key=lambda item: int(item.get("candidate_index", item["segment_id"]))):
            segment_id = int(row["segment_id"])
            candidate_index = int(row.get("candidate_index", segment_id))
            candidate_text = str(row["candidate_text"])
            suggested_hash = sha256_text(candidate_text)
            existing = existing_candidate(conn, segment_id, suggested_hash)
            context = fetch_segment_context(conn, segment_id)
            if existing:
                status = "skipped_existing"
                record = {
                    "candidate_index": candidate_index,
                    "segment_id": segment_id,
                    "status": status,
                    "candidate_id": int(existing["id"]),
                    "learned_at": existing["learned_at"] or "",
                    "confirmation_synced_at": existing["confirmation_synced_at"] or "",
                }
            elif context is None:
                status = "missing_context"
                record = {
                    "candidate_index": candidate_index,
                    "segment_id": segment_id,
                    "status": status,
                    "candidate_id": None,
                    "learned_at": "",
                    "confirmation_synced_at": "",
                }
            else:
                status = "pending_insert" if args.dry_run else "inserted"
                record = {
                    "candidate_index": candidate_index,
                    "segment_id": segment_id,
                    "status": status,
                    "candidate_id": None,
                    "learned_at": "",
                    "confirmation_synced_at": "",
                }
                pending_inserts.append((row, context, suggested_hash))
            records.append(record)
            status_counts[status] += 1

        inserted = 0 if args.dry_run else len(pending_inserts)
        skipped = status_counts["skipped_existing"] + status_counts["missing_context"]
        run_id = create_run(conn, inserted, skipped, timestamp, args.dry_run)
        if not args.dry_run and run_id is not None:
            inserted_by_key: dict[tuple[int, int], int] = {}
            for row, context, suggested_hash in pending_inserts:
                candidate_id = insert_candidate(conn, run_id, row, context, suggested_hash, timestamp)
                inserted_by_key[(int(row.get("candidate_index", row["segment_id"])), int(row["segment_id"]))] = candidate_id
            for record in records:
                key = (int(record["candidate_index"]), int(record["segment_id"]))
                if key in inserted_by_key:
                    record["candidate_id"] = inserted_by_key[key]
            conn.commit()

    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "dry_run": bool(args.dry_run),
        "input_apply_count": len(apply_rows),
        "inserted_count": int(inserted),
        "skipped_existing_count": int(status_counts["skipped_existing"]),
        "missing_context_count": int(status_counts["missing_context"]),
        "validation_note_count": len(validation_notes),
        "ready_for_learning_feedback_apply": bool(inserted > 0 and not args.dry_run),
        "production_full_recommended_now": False,
        "status_counts": dict(status_counts),
    }
    txt_path, jsonl_path, summary_path = write_outputs(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"inserted_count={summary['inserted_count']}")
    print(f"skipped_existing_count={summary['skipped_existing_count']}")
    print(f"ready_for_learning_feedback_apply={summary['ready_for_learning_feedback_apply']}")


if __name__ == "__main__":
    main()
