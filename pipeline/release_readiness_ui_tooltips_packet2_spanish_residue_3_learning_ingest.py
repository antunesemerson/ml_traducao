from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from release_readiness_ui_tooltips_packet2_spanish_residue_3_decisions import (
    DECISIONS,
    EXPECTED_APPROVE_ALREADY_OK,
    EXPECTED_CORRECTED_TEXT,
)


RULE_VERSION = "release_readiness_ui_tooltips_packet2_spanish_residue_3_learning_ingest_v1"
QUEUE_SOURCE = "release-readiness-ui-tooltips-packet2-spanish-residue-3"
ORIGIN = "human_confirmed_release_readiness_ui_tooltips_packet2_spanish_residue_3"
REVIEWER = "user_human_review_release_readiness_ui_tooltips_packet2_spanish_residue_3"
FOCUS_GROUP = "release_readiness_ui_tooltips_packet2_spanish_residue_3"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def fetch_context(conn, segment_id: int):
    return conn.execute(
        """
        SELECT s.id AS segment_id, s.relative_path, s.source_key, s.source_line_number,
               s.english_text, s.spanish_text, s.old_text, o.portuguese_text AS current_output_text
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id=s.id
        WHERE s.id=?
        """,
        (segment_id,),
    ).fetchone()


def existing_candidate(conn, segment_id: int, suggested_hash: str):
    return conn.execute(
        """
        SELECT id FROM local_learning_candidates
        WHERE segment_id=? AND suggested_hash=? AND queue_source=? AND origin=?
        LIMIT 1
        """,
        (segment_id, suggested_hash, QUEUE_SOURCE, ORIGIN),
    ).fetchone()


def create_run(conn, inserted_count: int, skipped_count: int, timestamp: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO local_learning_runs (
            mode, limit_count, auto_confidence_threshold, candidate_count,
            high_confidence_count, pending_human_count, status, notes,
            started_at, finished_at, updated_at
        )
        VALUES (?, ?, 1.0, ?, ?, 0, 'completed', ?, ?, ?, ?)
        """,
        (
            QUEUE_SOURCE,
            inserted_count + skipped_count,
            inserted_count,
            inserted_count,
            f"{RULE_VERSION}; skipped_or_blocked={skipped_count}",
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    return int(cur.lastrowid)


def insert_candidate(conn, run_id: int, context, decision: str, corrected_text: str, timestamp: str) -> int:
    suggested = corrected_text if decision == "corrected_text" else context["current_output_text"]
    cur = conn.execute(
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
        VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0, 'ok', 'safe',
                1.0, 'high_confidence', 'correct', ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            "ui_tooltips_spanish_residue_3_human_correction" if decision == "corrected_text" else "ui_tooltips_spanish_residue_3_human_confirmed_already_ok",
            corrected_text if decision == "corrected_text" else None,
            "human-approved UI/tooltips spanish residue 3 signal",
            REVIEWER,
            timestamp,
            json.dumps(["approved_in_chat", f"review_decision:{decision}", "no_source_write"], ensure_ascii=True),
            timestamp,
            timestamp,
            QUEUE_SOURCE,
            FOCUS_GROUP,
        ),
    )
    return int(cur.lastrowid)


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_packet2_spanish_residue_3_learning_ingest"
    txt = base.with_suffix(".txt")
    jsonl = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt), "jsonl": str(jsonl), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt.write_text("\n".join([
        "UI/tooltips packet2 spanish residue 3 learning ingest",
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
    ]) + "\n", encoding="utf-8")
    print(f"txt={txt}")
    print(f"jsonl={jsonl}")
    print(f"summary={summary_path}")


def main() -> None:
    decision_counts = Counter(decision for decision, _ in DECISIONS.values())
    if decision_counts["approve_already_ok"] != EXPECTED_APPROVE_ALREADY_OK:
        raise SystemExit("approve count guard failed")
    if decision_counts["corrected_text"] != EXPECTED_CORRECTED_TEXT:
        raise SystemExit("corrected count guard failed")
    timestamp = now()
    records: list[dict[str, Any]] = []
    with db.connect(db.load_settings()) as conn:
        ready = []
        for segment_id, (decision, corrected_text) in sorted(DECISIONS.items()):
            context = fetch_context(conn, segment_id)
            reasons: list[str] = []
            suggested = corrected_text if decision == "corrected_text" else (context["current_output_text"] if context else "")
            if context is None:
                reasons.append("missing_segment")
            if not suggested:
                reasons.append("missing_suggested_text")
            if suggested and existing_candidate(conn, segment_id, sha256_text(suggested)):
                reasons.append("already_ingested")
            status = "ready" if not reasons else "skipped"
            record = {
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
            records.append(record)
            if status == "ready":
                ready.append((record, context, decision, corrected_text))
        run_id = create_run(conn, len(ready), len(records) - len(ready), timestamp)
        for record, context, decision, corrected_text in ready:
            record["local_learning_candidate_id"] = insert_candidate(conn, run_id, context, decision, corrected_text, timestamp)
            record["run_id"] = run_id
            record["status"] = "inserted"
        conn.commit()
    status_counts = Counter(r["status"] for r in records)
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
    }
    write_reports(records, summary)
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
