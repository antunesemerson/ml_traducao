from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import canonical_localization_text


RULE_VERSION = "release_readiness_ui_tooltips_packet2_approve_ok_learning_ingest_v1"
INPUT_JSONL = Path("reports/20260702_175422_889616_release_readiness_ui_tooltips_plain_light_human_packet.jsonl")
QUEUE_SOURCE = "release-readiness-ui-tooltips-packet2-approve-ok"
ORIGIN = "human_confirmed_release_readiness_ui_tooltips_packet2_approve_ok"
MATCH_TYPE = "ui_tooltips_plain_light_human_confirmed_already_ok"
REVIEWER = "user_human_review_release_readiness_ui_tooltips_packet2"
FOCUS_GROUP = "release_readiness_ui_tooltips_packet2_approve_ok"
EXPECTED_COUNT = 29
OUTPUT_SLUG = "release_readiness_ui_tooltips_packet2_approve_ok_learning_ingest"


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest approved already-ok UI/tooltips packet2 rows as local learning.")
    parser.add_argument("--input-jsonl", type=Path, default=INPUT_JSONL)
    parser.add_argument("--expected-count", type=int, default=EXPECTED_COUNT)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def read_focus_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("suggested_human_decision") == "approve_already_ok":
                row["_line_number"] = line_number
                rows.append(row)
    return rows


def fetch_context(conn: sqlite3.Connection, segment_id: int) -> dict[str, Any] | None:
    row = conn.execute(
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
    return dict(row) if row else None


def existing_candidate(conn: sqlite3.Connection, segment_id: int, suggested_hash: str) -> int | None:
    row = conn.execute(
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
    return int(row["id"]) if row else None


def fetch_snapshot(conn: sqlite3.Connection, segment_ids: list[int]) -> dict[str, Any]:
    placeholders = ",".join("?" for _ in segment_ids)
    return {
        "local_learning_candidates": [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM local_learning_candidates WHERE segment_id IN ({placeholders}) ORDER BY segment_id, id",
                segment_ids,
            )
        ],
        "segment_confirmations": [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM segment_confirmations WHERE segment_id IN ({placeholders}) ORDER BY segment_id, id",
                segment_ids,
            )
        ],
    }


def create_run(conn: sqlite3.Connection, inserted_count: int, skipped_count: int, timestamp: str) -> int:
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


def evaluate_row(conn: sqlite3.Connection, row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    segment_id = int(row["segment_id"])
    context = fetch_context(conn, segment_id)
    reasons: list[str] = []
    suggested = str(row.get("output_text") or row.get("confirmed_text") or "")
    if context is None:
        reasons.append("missing_segment")
    elif canonical_localization_text(context.get("current_output_text") or "") != canonical_localization_text(suggested):
        reasons.append("current_output_not_packet_output")
    if row.get("token_surface") not in {"plain_text", "light_token"}:
        reasons.append("unsupported_token_surface")
    if not suggested:
        reasons.append("missing_suggested_text")
    suggested_hash = sha256_text(suggested) if suggested else ""
    existing_id = existing_candidate(conn, segment_id, suggested_hash) if suggested_hash else None
    if existing_id is not None:
        reasons.append("already_ingested")
    record = {
        "source": RULE_VERSION,
        "record_type": "learning_ingest_item",
        "segment_id": segment_id,
        "review_index": row.get("review_index"),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "token_surface": row.get("token_surface"),
        "suggested_text": suggested,
        "suggested_hash": suggested_hash,
        "status": "ready" if not reasons else "skipped",
        "block_reasons": reasons,
        "existing_candidate_id": existing_id,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
    }
    return record, context


def insert_candidate(conn: sqlite3.Connection, run_id: int, context: dict[str, Any], record: dict[str, Any], timestamp: str) -> int:
    reasons = [
        "human_confirmed_release_readiness_ui_tooltips_packet2_approve_ok",
        "review_decision:approve_already_ok",
        "output_already_matches_human_approved_text",
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
            record["suggested_text"],
            record["suggested_hash"],
            "human_already_ok",
            ORIGIN,
            MATCH_TYPE,
            1.0,
            "ok",
            "safe",
            1.0,
            "high_confidence",
            "correct",
            "human-approved release readiness UI/tooltips already-ok learning signal",
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
    base = reports_dir() / f"{stamp()}_{OUTPUT_SLUG}_{summary['mode']}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    snapshot_path = Path(str(base) + "_before_snapshot.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {
        "txt": str(txt_path),
        "jsonl": str(jsonl_path),
        "summary": str(summary_path),
        "before_snapshot": str(snapshot_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    snapshot_path.write_text(json.dumps(before_snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                "Release readiness UI/tooltips packet2 approve-ok learning ingest",
                f"mode={summary['mode']}",
                f"run_id={summary['run_id'] or ''}",
                f"record_count={summary['record_count']}",
                f"ready_count={summary['ready_count']}",
                f"inserted_count={summary['inserted_count']}",
                f"skipped_count={summary['skipped_count']}",
                f"block_reason_counts={json.dumps(summary['block_reason_counts'], ensure_ascii=False, sort_keys=True)}",
                "candidate_generation_count=0",
                "apply_count=0",
                "lifecycle_count=0",
                "segment_state_count=0",
                "reindex_count=0",
                "production_full_count=0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return txt_path, jsonl_path, summary_path, snapshot_path


def main() -> None:
    args = parse_args()
    mode = "apply" if args.apply else "dry_run"
    rows = read_focus_rows(args.input_jsonl)
    if len(rows) != args.expected_count:
        raise SystemExit(f"expected {args.expected_count} approve_already_ok rows, got {len(rows)}")
    timestamp = now()
    records: list[dict[str, Any]] = []
    contexts: dict[int, dict[str, Any]] = {}
    with db.connect(db.load_settings()) as conn:
        segment_ids = [int(row["segment_id"]) for row in rows]
        before_snapshot = fetch_snapshot(conn, segment_ids)
        for row in rows:
            record, context = evaluate_row(conn, row)
            records.append(record)
            if context is not None:
                contexts[int(row["segment_id"])] = context
        ready = [record for record in records if record["status"] == "ready"]
        run_id = None
        if args.apply and ready:
            run_id = create_run(conn, len(ready), len(records) - len(ready), timestamp)
            for record in ready:
                candidate_id = insert_candidate(conn, run_id, contexts[int(record["segment_id"])], record, timestamp)
                record["local_learning_candidate_id"] = candidate_id
                record["run_id"] = run_id
                record["status"] = "inserted"
            conn.commit()
    status_counts = Counter(record["status"] for record in records)
    block_counts = Counter(reason for record in records for reason in record.get("block_reasons", []))
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "mode": mode,
        "input_jsonl": str(args.input_jsonl),
        "run_id": run_id,
        "record_count": len(records),
        "ready_count": status_counts.get("ready", 0) if not args.apply else status_counts.get("inserted", 0),
        "inserted_count": status_counts.get("inserted", 0),
        "skipped_count": status_counts.get("skipped", 0),
        "status_counts": dict(status_counts.most_common()),
        "block_reason_counts": dict(block_counts.most_common()),
        "token_surface_counts": dict(Counter(record.get("token_surface") for record in records).most_common()),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "single_operational_recommendation": (
            "Run apply only if dry-run has ready_count=29 and skipped_count=0; then run learn-feedback."
            if not args.apply
            else "Run learn-feedback, then segment-state before any lifecycle bridge."
        ),
    }
    txt, jsonl, summary_path, snapshot = write_outputs(records, summary, before_snapshot)
    print(f"txt={txt}")
    print(f"jsonl={jsonl}")
    print(f"summary={summary_path}")
    print(f"before_snapshot={snapshot}")
    print(f"mode={mode}")
    print(f"run_id={run_id or ''}")
    print(f"record_count={summary['record_count']}")
    print(f"ready_count={summary['ready_count']}")
    print(f"inserted_count={summary['inserted_count']}")
    print(f"skipped_count={summary['skipped_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
