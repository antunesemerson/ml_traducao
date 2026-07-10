from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from release_readiness_ui_tooltips_packet2_context_sublote_decisions import (
    DECISIONS,
    EXPECTED_APPROVE_ALREADY_OK,
    EXPECTED_CORRECTED_TEXT,
)


RULE_VERSION = "release_readiness_ui_tooltips_packet2_context_sublote_learning_ingest_v1"
REVIEW_PACKET_JSONL = Path("reports/20260702_211655_987586_release_readiness_ui_tooltips_packet2_context_sublote_review_packet.jsonl")
QUEUE_SOURCE = "release-readiness-ui-tooltips-packet2-context-sublote"
ORIGIN = "human_confirmed_release_readiness_ui_tooltips_packet2_context_sublote"
REVIEWER = "user_human_review_release_readiness_ui_tooltips_packet2_context_sublote"
FOCUS_GROUP = "release_readiness_ui_tooltips_packet2_context_sublote"


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


def read_review_rows() -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    with db.project_path(REVIEW_PACKET_JSONL).open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                rows[int(row["segment_id"])] = row
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


def insert_candidate(conn, run_id: int, context, segment_id: int, decision: str, corrected_text: str, timestamp: str) -> int:
    suggested = corrected_text if decision == "corrected_text" else context["current_output_text"]
    reasons = [
        "human_confirmed_release_readiness_ui_tooltips_packet2_context_sublote",
        f"review_decision:{decision}",
        "no_source_write",
        "protected_output_apply_if_corrected_text",
        "approved_in_chat",
    ]
    match_type = "ui_tooltips_context_sublote_human_correction" if decision == "corrected_text" else "ui_tooltips_context_sublote_human_confirmed_already_ok"
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
        VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            "human_correction" if decision == "corrected_text" else "human_already_ok",
            ORIGIN,
            match_type,
            1.0,
            "ok",
            "safe",
            1.0,
            "high_confidence",
            "correct",
            corrected_text if decision == "corrected_text" else None,
            "human-approved UI/tooltips context sublote learning signal",
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


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_packet2_context_sublote_learning_ingest"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                "UI/tooltips packet2 context sublote learning ingest",
                f"run_id={summary['run_id']}",
                f"record_count={summary['record_count']}",
                f"inserted_count={summary['inserted_count']}",
                f"skipped_count={summary['skipped_count']}",
                f"decision_counts={json.dumps(summary['decision_counts'], ensure_ascii=False, sort_keys=True)}",
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
    return txt_path, jsonl_path, summary_path


def main() -> None:
    review_rows = read_review_rows()
    decision_counts = Counter(decision for decision, _ in DECISIONS.values())
    if decision_counts["approve_already_ok"] != EXPECTED_APPROVE_ALREADY_OK:
        raise SystemExit("approve count guard failed")
    if decision_counts["corrected_text"] != EXPECTED_CORRECTED_TEXT:
        raise SystemExit("corrected count guard failed")
    timestamp = now()
    records: list[dict[str, Any]] = []
    with db.connect(db.load_settings()) as conn:
        ready: list[tuple[int, str, str, Any]] = []
        for segment_id, (decision, corrected_text) in sorted(DECISIONS.items()):
            context = fetch_context(conn, segment_id)
            reasons: list[str] = []
            if context is None:
                reasons.append("missing_segment")
                suggested = corrected_text
            else:
                suggested = corrected_text if decision == "corrected_text" else context["current_output_text"]
            if segment_id not in review_rows:
                reasons.append("missing_review_row")
            if not suggested:
                reasons.append("missing_suggested_text")
            if suggested and existing_candidate(conn, segment_id, sha256_text(suggested)):
                reasons.append("already_ingested")
            status = "ready" if not reasons else "skipped"
            records.append(
                {
                    "source": RULE_VERSION,
                    "record_type": "learning_ingest_item",
                    "segment_id": segment_id,
                    "decision": decision,
                    "suggested_text": suggested,
                    "status": status,
                    "block_reasons": reasons,
                    "candidate_generation_count": 0,
                    "apply_count": 0,
                    "lifecycle_count": 0,
                    "segment_state_count": 0,
                    "reindex_count": 0,
                    "production_full_count": 0,
                }
            )
            if status == "ready":
                ready.append((segment_id, decision, corrected_text, context))
        run_id = create_run(conn, len(ready), len(records) - len(ready), timestamp)
        for segment_id, decision, corrected_text, context in ready:
            candidate_id = insert_candidate(conn, run_id, context, segment_id, decision, corrected_text, timestamp)
            for record in records:
                if record["segment_id"] == segment_id:
                    record["local_learning_candidate_id"] = candidate_id
                    record["run_id"] = run_id
                    record["status"] = "inserted"
        conn.commit()
    status_counts = Counter(record["status"] for record in records)
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "run_id": run_id,
        "record_count": len(records),
        "inserted_count": status_counts["inserted"],
        "skipped_count": status_counts["skipped"],
        "decision_counts": dict(decision_counts),
        "status_counts": dict(status_counts),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "single_operational_recommendation": "Run learn-feedback, then segment-state before issue closure/lifecycle.",
    }
    txt, jsonl, summary_path = write_reports(records, summary)
    print(f"txt={txt}")
    print(f"jsonl={jsonl}")
    print(f"summary={summary_path}")
    print(f"run_id={run_id}")
    print(f"record_count={summary['record_count']}")
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
