from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "semantic_plain_lowrisk_human_corrections_ingest_v1"
QUEUE_SOURCE = "semantic-plain-lowrisk-human-review"
ORIGIN = "human_confirmed_semantic_plain_lowrisk"
MATCH_TYPE = "ptbr_fluency_human_correction"
REVIEWER = "user_human_review_semantic_plain_lowrisk"
FOCUS_GROUP = "semantic_plain_lowrisk"

APPROVED_CORRECTIONS = {
    1992: "Sua família se ressentirá de você por apoiar este estrangeiro para conquistar a lealdade dele.",
    6406: "Dizem que esta lâmina é uma das espadas de El Cid, uma inspiração para qualquer pessoa que aspire às virtudes cavalheirescas dele.",
    3806: "Agora sinto até falta de estar em um lugar coberto.",
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


def latest_review_packet() -> Path:
    matches = sorted(
        reports_dir().glob("*_semantic_plain_lowrisk_human_review_packet.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise SystemExit("missing semantic_plain_lowrisk_human_review_packet jsonl")
    return matches[0]


def read_packet_rows(path: Path) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            try:
                segment_id = int(row["segment_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise SystemExit(f"invalid segment_id at {path}:{line_number}") from exc
            rows[segment_id] = row
    return rows


def token_guard(current: str, corrected: str) -> bool:
    # This batch is intentionally no-token prose. If tokens appear, force a hold.
    token_markers = ("[", "]", "$", "#", "Select_CString", ".Custom('ES_")
    return not any(marker in current or marker in corrected for marker in token_markers)


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


def fetch_snapshot(conn, segment_ids: list[int]) -> dict[str, Any]:
    placeholders = ",".join("?" for _ in segment_ids)
    candidates = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT *
            FROM local_learning_candidates
            WHERE segment_id IN ({placeholders})
            ORDER BY segment_id, id
            """,
            tuple(segment_ids),
        )
    ]
    confirmations = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT *
            FROM segment_confirmations
            WHERE segment_id IN ({placeholders})
            ORDER BY segment_id, id
            """,
            tuple(segment_ids),
        )
    ]
    return {
        "local_learning_candidates": candidates,
        "segment_confirmations": confirmations,
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
            f"{RULE_VERSION}; skipped_duplicates={skipped_count}",
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    return int(cursor.lastrowid)


def insert_candidate(conn, run_id: int, row: dict[str, Any], corrected: str, timestamp: str) -> int:
    suggested_hash = sha256_text(corrected)
    reasons = [
        "human_confirmed_ptbr_fluency_correction",
        "semantic_plain_lowrisk_review_sample",
        "no_token_surface",
        "no_source_write",
        "no_output_write",
        "approved_in_chat",
        f"previous_classification:{row.get('initial_classification')}",
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
            row.get("relative_path"),
            row.get("source_key"),
            row.get("source_line_number"),
            row.get("english_text"),
            row.get("spanish_text"),
            row.get("old_text"),
            row.get("current_output_text"),
            corrected,
            suggested_hash,
            "human_correction",
            ORIGIN,
            MATCH_TYPE,
            1.0,
            "ok",
            "safe",
            1.0,
            "high_confidence",
            "correct",
            "human-approved PT-BR fluency correction from semantic plain lowrisk review",
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
    base = reports_dir() / f"{stamp()}_semantic_plain_lowrisk_human_corrections_ingest"
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
        "semantic plain lowrisk human corrections ingest",
        f"rule_version={RULE_VERSION}",
        f"input_packet={summary['input_packet']}",
        f"run_id={summary['run_id']}",
        f"inserted_count={summary['inserted_count']}",
        f"skipped_existing_count={summary['skipped_existing_count']}",
        f"blocked_count={summary['blocked_count']}",
        f"snapshot_path={snapshot_path}",
        "",
        "records:",
    ]
    for record in records:
        lines.append(
            "- "
            f"{record['segment_id']} | {record['status']} | "
            f"{record.get('current_output_text', '')} -> {record.get('suggested_text', '')}"
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path, snapshot_path


def main() -> None:
    packet_path = latest_review_packet()
    packet_rows = read_packet_rows(packet_path)
    missing = sorted(set(APPROVED_CORRECTIONS) - set(packet_rows))
    if missing:
        raise SystemExit(f"approved corrections missing from packet: {missing}")

    timestamp = now()
    segment_ids = sorted(APPROVED_CORRECTIONS)
    records: list[dict[str, Any]] = []
    inserted_count = 0
    skipped_existing_count = 0
    blocked_count = 0

    with db.connect(db.load_settings()) as conn:
        before_snapshot = fetch_snapshot(conn, segment_ids)
        for segment_id, corrected in APPROVED_CORRECTIONS.items():
            row = packet_rows[segment_id]
            current = str(row.get("current_output_text") or "")
            suggested_hash = sha256_text(corrected)
            existing = existing_candidate(conn, segment_id, suggested_hash)
            if existing:
                skipped_existing_count += 1
                records.append(
                    {
                        "segment_id": segment_id,
                        "status": "skipped_existing",
                        "existing_candidate_id": int(existing["id"]),
                        "current_output_text": current,
                        "suggested_text": corrected,
                        "suggested_hash": suggested_hash,
                    }
                )
                continue
            if not token_guard(current, corrected):
                blocked_count += 1
                records.append(
                    {
                        "segment_id": segment_id,
                        "status": "blocked_token_guard",
                        "current_output_text": current,
                        "suggested_text": corrected,
                        "suggested_hash": suggested_hash,
                    }
                )
                continue
            inserted_count += 1

        run_id = create_run(conn, inserted_count, skipped_existing_count + blocked_count, timestamp)

        for segment_id, corrected in APPROVED_CORRECTIONS.items():
            if any(record["segment_id"] == segment_id and record["status"] != "skipped_existing" for record in records):
                continue
            suggested_hash = sha256_text(corrected)
            if existing_candidate(conn, segment_id, suggested_hash):
                continue
            row = packet_rows[segment_id]
            candidate_id = insert_candidate(conn, run_id, row, corrected, timestamp)
            records.append(
                {
                    "segment_id": segment_id,
                    "status": "inserted",
                    "candidate_id": candidate_id,
                    "run_id": run_id,
                    "current_output_text": row.get("current_output_text"),
                    "suggested_text": corrected,
                    "suggested_hash": suggested_hash,
                }
            )

        conn.commit()

    summary = {
        "schema_version": 1,
        "rule_version": RULE_VERSION,
        "generated_at": timestamp,
        "input_packet": str(packet_path),
        "run_id": run_id,
        "approved_count": len(APPROVED_CORRECTIONS),
        "inserted_count": inserted_count,
        "skipped_existing_count": skipped_existing_count,
        "blocked_count": blocked_count,
        "apply_ready_now": 0,
        "source_output_write": False,
        "production_full_recommended_now": False,
        "next_action": "apply_local_learning_feedback_for_confirmed_human_corrections",
    }
    txt_path, jsonl_path, summary_path, snapshot_path = write_outputs(records, summary, before_snapshot)
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
