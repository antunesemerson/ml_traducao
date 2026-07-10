from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "short_label_plain_batch2_learning_ingest_v1"
QUEUE_SOURCE = "short-label-plain-human-review-batch2"
ORIGIN = "human_confirmed_short_label_plain_batch2"
MATCH_TYPE_CORRECTION = "short_label_plain_human_correction"
MATCH_TYPE_ALREADY_OK = "short_label_plain_human_confirmed_already_ok"
REVIEWER = "user_human_review_short_label_plain_batch2"
FOCUS_GROUP = "short_label_plain_batch2"

APPROVED_DECISIONS = {
    50741: ("human_correction_candidate", "Eu vou ensinar você."),
    50743: ("already_ok", None),
    51754: ("human_correction_candidate", "Este personagem não poderia se importar menos com as implicações da mudança governamental."),
    55794: ("human_correction_candidate", '"Vou mostrar a você!"'),
    55979: ("already_ok", None),
    55990: ("already_ok", None),
    56160: ("human_correction_candidate", "Suas forças te abandonam"),
    56161: ("already_ok", None),
    56362: ("already_ok", None),
    56388: ("already_ok", None),
    56482: ("already_ok", None),
    56498: ("human_correction_candidate", "Este condado está recebendo um lento fluxo de povos nômades vindos do outro lado da fronteira."),
}


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


def latest_packet() -> Path:
    matches = sorted(
        reports_dir().glob("*_short_label_plain_human_review_packet.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise SystemExit("missing short_label_plain_human_review_packet jsonl")
    return matches[0]


def read_jsonl(path: Path) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            try:
                rows[int(row["segment_id"])] = row
            except (KeyError, TypeError, ValueError) as exc:
                raise SystemExit(f"invalid segment_id at {path}:{line_number}") from exc
    return rows


def token_guard(current: str, suggested: str) -> bool:
    token_markers = ("[", "]", "$", "#", "Select_CString", ".Custom('ES_")
    return not any(marker in current or marker in suggested for marker in token_markers)


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


def create_run(conn, inserted_count: int, skipped_count: int, timestamp: str) -> int:
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
            f"{RULE_VERSION}; skipped_or_blocked={skipped_count}",
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    return int(cursor.lastrowid)


def insert_candidate(conn, run_id: int, context, packet_row: dict[str, Any], suggested: str, match_type: str, timestamp: str) -> int:
    reasons = [
        "human_confirmed_short_label_plain_batch2",
        f"packet_decision:{packet_row.get('packet_decision')}",
        "short_label_style_microagent",
        "no_source_write",
        "no_output_write",
        "approved_in_chat",
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
            "human_correction" if match_type == MATCH_TYPE_CORRECTION else "human_already_ok",
            ORIGIN,
            match_type,
            1.0,
            "ok",
            "safe",
            1.0,
            "high_confidence",
            "correct",
            "human-approved short label plain batch2 learning signal",
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
    base = reports_dir() / f"{stamp()}_short_label_plain_batch2_learning_ingest"
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
        "short label plain batch2 learning ingest",
        f"rule_version={RULE_VERSION}",
        f"input_packet={summary['input_packet']}",
        f"run_id={summary['run_id']}",
        f"inserted_count={summary['inserted_count']}",
        f"inserted_corrections={summary['inserted_corrections']}",
        f"inserted_already_ok={summary['inserted_already_ok']}",
        f"skipped_existing_count={summary['skipped_existing_count']}",
        f"blocked_count={summary['blocked_count']}",
        f"snapshot_path={snapshot_path}",
        "",
        "records:",
    ]
    for record in records:
        lines.append(f"- {record['segment_id']} | {record['status']} | {record.get('decision', '')}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path, snapshot_path


def main() -> None:
    packet_path = latest_packet()
    packet_rows = read_jsonl(packet_path)
    expected_ids = sorted(APPROVED_DECISIONS)
    missing = [segment_id for segment_id in expected_ids if segment_id not in packet_rows]
    if missing:
        raise SystemExit(f"segments missing from packet: {missing}")

    timestamp = now()
    records: list[dict[str, Any]] = []
    plan: list[tuple[int, dict[str, Any], Any, str, str, str]] = []
    skipped_existing_count = 0
    blocked_count = 0

    with db.connect(db.load_settings()) as conn:
        before_snapshot = fetch_snapshot(conn, expected_ids)
        for segment_id, (decision, corrected_text) in sorted(APPROVED_DECISIONS.items()):
            packet_row = packet_rows[segment_id]
            context = fetch_context(conn, segment_id)
            if not context:
                blocked_count += 1
                records.append({"segment_id": segment_id, "status": "blocked_missing_context", "decision": decision})
                continue
            current = str(context["current_output_text"] or "")
            suggested = corrected_text if corrected_text is not None else current
            match_type = MATCH_TYPE_CORRECTION if decision == "human_correction_candidate" else MATCH_TYPE_ALREADY_OK
            suggested_hash = sha256_text(suggested)
            existing = existing_candidate(conn, segment_id, suggested_hash)
            if existing:
                skipped_existing_count += 1
                records.append(
                    {
                        "segment_id": segment_id,
                        "status": "skipped_existing",
                        "decision": decision,
                        "existing_candidate_id": int(existing["id"]),
                    }
                )
                continue
            if not token_guard(current, suggested):
                blocked_count += 1
                records.append({"segment_id": segment_id, "status": "blocked_token_guard", "decision": decision})
                continue
            plan.append((segment_id, packet_row, context, suggested, match_type, decision))

        run_id = create_run(conn, len(plan), skipped_existing_count + blocked_count, timestamp)
        inserted_corrections = 0
        inserted_already_ok = 0
        for segment_id, packet_row, context, suggested, match_type, decision in plan:
            candidate_id = insert_candidate(conn, run_id, context, packet_row, suggested, match_type, timestamp)
            if match_type == MATCH_TYPE_CORRECTION:
                inserted_corrections += 1
            else:
                inserted_already_ok += 1
            records.append(
                {
                    "segment_id": segment_id,
                    "status": "inserted",
                    "decision": decision,
                    "candidate_id": candidate_id,
                    "run_id": run_id,
                    "suggested_hash": sha256_text(suggested),
                }
            )
        conn.commit()

    summary = {
        "schema_version": 1,
        "rule_version": RULE_VERSION,
        "generated_at": timestamp,
        "input_packet": str(packet_path),
        "run_id": run_id,
        "approved_count": len(APPROVED_DECISIONS),
        "inserted_count": len(plan),
        "inserted_corrections": inserted_corrections,
        "inserted_already_ok": inserted_already_ok,
        "skipped_existing_count": skipped_existing_count,
        "blocked_count": blocked_count,
        "hold_count": 0,
        "source_output_write": False,
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "next_action": "apply_local_learning_feedback_for_short_label_plain_batch2",
    }
    txt_path, jsonl_path, summary_path, snapshot_path = write_outputs(records, summary, before_snapshot)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"snapshot={snapshot_path}")
    print(f"run_id={run_id}")
    print(f"inserted_count={len(plan)}")
    print(f"inserted_corrections={inserted_corrections}")
    print(f"inserted_already_ok={inserted_already_ok}")
    print(f"skipped_existing_count={skipped_existing_count}")
    print(f"blocked_count={blocked_count}")
    print(f"next_action={summary['next_action']}")


if __name__ == "__main__":
    main()
