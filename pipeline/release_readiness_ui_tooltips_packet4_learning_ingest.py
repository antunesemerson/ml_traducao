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


RULE_VERSION = "release_readiness_ui_tooltips_packet4_learning_ingest_v1"
PACKET_JSONL = Path("reports/20260703_134652_257558_release_readiness_ui_tooltips_plain_light_human_packet.jsonl")
APPLY_JSONL = Path("reports/20260703_135214_374570_release_readiness_ui_tooltips_packet4_corrected_apply_apply.jsonl")
QUEUE_SOURCE = "release-readiness-ui-tooltips-packet4"
ORIGIN = "human_confirmed_release_readiness_ui_tooltips_packet4"
REVIEWER = "user_human_review_release_readiness_ui_tooltips_packet4"
FOCUS_GROUP = "release_readiness_ui_tooltips_packet4"
CORRECTED_SEGMENT_IDS = {33279, 46950, 49535, 49943}
APPROVE_SEGMENT_IDS = {62868, 65128, 30310, 46844, 47209, 49937}


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest UI/tooltips packet4 human decisions as local learning.")
    parser.add_argument("--packet-jsonl", type=Path, default=PACKET_JSONL)
    parser.add_argument("--apply-jsonl", type=Path, default=APPLY_JSONL)
    parser.add_argument("--expected-count", type=int, default=40)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def fetch_context(conn: sqlite3.Connection, segment_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT s.id AS segment_id, s.relative_path, s.source_key, s.source_line_number,
               s.english_text, s.spanish_text, s.old_text, o.portuguese_text AS current_output_text
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id=s.id
        WHERE s.id=?
        """,
        (segment_id,),
    ).fetchone()
    return dict(row) if row else None


def existing_candidate(conn: sqlite3.Connection, segment_id: int, suggested_hash: str) -> int | None:
    row = conn.execute(
        """
        SELECT id FROM local_learning_candidates
        WHERE segment_id=? AND suggested_hash=? AND queue_source=? AND origin=?
        LIMIT 1
        """,
        (segment_id, suggested_hash, QUEUE_SOURCE, ORIGIN),
    ).fetchone()
    return int(row["id"]) if row else None


def create_run(conn: sqlite3.Connection, inserted_count: int, skipped_count: int, timestamp: str) -> int:
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


def insert_candidate(conn: sqlite3.Connection, run_id: int, context: dict[str, Any], record: dict[str, Any], timestamp: str) -> int:
    is_correction = record["decision_kind"] == "corrected_text"
    suggested = record["suggested_text"]
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
        VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, 1.0, 'ok', 'safe',
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
            "human_correction" if is_correction else "human_already_ok",
            ORIGIN,
            "ui_tooltips_packet4_human_correction" if is_correction else "ui_tooltips_packet4_human_confirmed_already_ok",
            suggested if is_correction else None,
            "human-approved release readiness UI/tooltips packet4 signal",
            REVIEWER,
            timestamp,
            json.dumps(
                [
                    "approved_in_chat",
                    "protected_apply_done" if is_correction else "output_already_matches_human_approved_text",
                    "no_source_write",
                ],
                ensure_ascii=True,
            ),
            timestamp,
            timestamp,
            QUEUE_SOURCE,
            FOCUS_GROUP,
        ),
    )
    return int(cur.lastrowid)


def build_records(args: argparse.Namespace) -> list[dict[str, Any]]:
    packet_rows = {int(row["segment_id"]): row for row in read_jsonl(args.packet_jsonl)}
    apply_rows = {int(row["segment_id"]): row for row in read_jsonl(args.apply_jsonl)}
    target_ids = set(packet_rows)
    if len(target_ids) != args.expected_count:
        raise SystemExit(f"expected packet count {args.expected_count}, got {len(target_ids)}")
    records: list[dict[str, Any]] = []
    for segment_id in sorted(target_ids):
        packet = packet_rows[segment_id]
        apply_row = apply_rows.get(segment_id)
        decision_kind = "corrected_text" if segment_id in CORRECTED_SEGMENT_IDS else "approve_already_ok"
        if packet.get("suggested_human_decision") == "corrected_text" and segment_id not in CORRECTED_SEGMENT_IDS | APPROVE_SEGMENT_IDS:
            raise SystemExit(f"unresolved corrected_text decision for segment {segment_id}")
        suggested = (
            str(apply_row.get("target_text") or "")
            if apply_row
            else str(packet.get("output_text") or packet.get("confirmed_text") or "")
        )
        records.append(
            {
                "source": RULE_VERSION,
                "record_type": "learning_ingest_item",
                "segment_id": segment_id,
                "relative_path": packet.get("relative_path"),
                "source_key": packet.get("source_key"),
                "decision_kind": decision_kind,
                "suggested_text": suggested,
                "suggested_hash": sha256_text(suggested),
                "status": "pending",
                "block_reasons": [],
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )
    return records


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any], before_snapshot: dict[str, Any]) -> None:
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_packet4_learning_ingest_{summary['mode']}"
    txt = base.with_suffix(".txt")
    jsonl = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    snapshot = Path(str(base) + "_before_snapshot.json")
    with jsonl.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt), "jsonl": str(jsonl), "summary": str(summary_path), "before_snapshot": str(snapshot)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    snapshot.write_text(json.dumps(before_snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt.write_text(
        "\n".join(
            [
                "Release readiness UI/tooltips packet4 learning ingest",
                f"mode={summary['mode']}",
                f"run_id={summary['run_id'] or ''}",
                f"record_count={summary['record_count']}",
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
    print(f"txt={txt}")
    print(f"jsonl={jsonl}")
    print(f"summary={summary_path}")
    print(f"before_snapshot={snapshot}")


def main() -> None:
    args = parse_args()
    mode = "apply" if args.apply else "dry_run"
    records = build_records(args)
    timestamp = now()
    run_id = None
    contexts: dict[int, dict[str, Any]] = {}
    with db.connect(db.load_settings()) as conn:
        segment_ids = [int(record["segment_id"]) for record in records]
        ph = ",".join("?" for _ in segment_ids)
        before_snapshot = {
            "local_learning_candidates": [
                dict(row)
                for row in conn.execute(
                    f"SELECT * FROM local_learning_candidates WHERE segment_id IN ({ph}) ORDER BY segment_id, id",
                    segment_ids,
                )
            ],
            "segment_confirmations": [
                dict(row)
                for row in conn.execute(
                    f"SELECT * FROM segment_confirmations WHERE segment_id IN ({ph}) ORDER BY segment_id, id",
                    segment_ids,
                )
            ],
        }
        for record in records:
            segment_id = int(record["segment_id"])
            reasons: list[str] = []
            context = fetch_context(conn, segment_id)
            if context is None:
                reasons.append("missing_segment")
            else:
                contexts[segment_id] = context
                if canonical_localization_text(context.get("current_output_text") or "") != canonical_localization_text(record["suggested_text"]):
                    reasons.append("current_output_not_suggested")
            if existing_candidate(conn, segment_id, record["suggested_hash"]):
                reasons.append("already_ingested")
            record["status"] = "ready" if not reasons else "skipped"
            record["block_reasons"] = reasons
        ready = [record for record in records if record["status"] == "ready"]
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
        "packet_jsonl": str(args.packet_jsonl),
        "apply_jsonl": str(args.apply_jsonl),
        "run_id": run_id,
        "record_count": len(records),
        "ready_count": status_counts.get("ready", 0) if not args.apply else status_counts.get("inserted", 0),
        "inserted_count": status_counts.get("inserted", 0),
        "skipped_count": status_counts.get("skipped", 0),
        "decision_kind_counts": dict(Counter(record["decision_kind"] for record in records).most_common()),
        "status_counts": dict(status_counts.most_common()),
        "block_reason_counts": dict(block_counts.most_common()),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
    }
    write_reports(records, summary, before_snapshot)
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
