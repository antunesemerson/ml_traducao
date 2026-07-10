from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens


RULE_VERSION = "culture_religion_parameter_modifier_batch14_output_segments_sync_v1"
SEGMENT_IDS = (20487, 20736, 21399, 21417)
EXPECTED_LEARNING_RUN_ID = 717


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def output_root() -> Path:
    return db.project_path(db.load_settings()["output_spanish"])


def token_counts(value: str | None) -> dict[str, int]:
    return dict(sorted(protected_tokens(value or "").items()))


def fetch_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in SEGMENT_IDS)
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT
                s.id AS segment_id,
                s.relative_path,
                s.source_key,
                s.source_line_number,
                o.output_line_number,
                o.portuguese_text AS db_output_text,
                o.output_raw_line AS db_output_raw_line,
                c.confirmed_text,
                c.confirmation_level,
                c.confirmation_source,
                c.confirmation_label,
                c.locked,
                l.run_id AS learning_run_id,
                l.human_label
            FROM source_segments s
            JOIN output_segments o ON o.segment_id = s.id
            JOIN segment_confirmations c ON c.segment_id = s.id
            JOIN local_learning_candidates l ON l.id = c.candidate_id
            WHERE s.id IN ({placeholders})
            ORDER BY s.id
            """,
            SEGMENT_IDS,
        ).fetchall()
    ]


def evaluate(row: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    relative_path = str(row["relative_path"])
    output_path = output_root() / relative_path
    line_number = int(row.get("output_line_number") or row.get("source_line_number") or 0)
    confirmed_text = str(row.get("confirmed_text") or "")
    db_output_text = str(row.get("db_output_text") or "")
    file_line = ""

    if int(row.get("learning_run_id") or 0) != EXPECTED_LEARNING_RUN_ID:
        reasons.append("learning_run_id_mismatch")
    if row.get("confirmation_level") != "human_confirmed":
        reasons.append("confirmation_not_human_confirmed")
    if row.get("confirmation_source") != "local_learning":
        reasons.append("confirmation_source_mismatch")
    if row.get("confirmation_label") != "semantic_error":
        reasons.append("confirmation_label_mismatch")
    if int(row.get("locked") or 0) != 1:
        reasons.append("confirmation_not_locked")
    if row.get("human_label") != "semantic_error":
        reasons.append("learning_human_label_mismatch")
    if not output_path.exists():
        reasons.append("missing_output_file")
    if line_number <= 0:
        reasons.append("missing_output_line_number")

    if output_path.exists() and line_number > 0:
        lines = output_path.read_text(encoding="utf-8-sig").splitlines()
        index = line_number - 1
        if index < 0 or index >= len(lines):
            reasons.append("line_out_of_range")
        else:
            file_line = lines[index]
            if confirmed_text not in file_line:
                reasons.append("file_line_does_not_contain_confirmed_text")

    if token_counts(db_output_text) != token_counts(confirmed_text):
        reasons.append("db_output_token_signature_mismatch")
    if token_counts(file_line) != token_counts(confirmed_text):
        reasons.append("file_line_token_signature_mismatch")

    already_synced = db_output_text == confirmed_text and str(row.get("db_output_raw_line") or "") == file_line
    status = "ready" if not reasons and not already_synced else "blocked"
    if not reasons and already_synced:
        status = "already_synced"

    return {
        "segment_id": int(row["segment_id"]),
        "relative_path": relative_path,
        "source_key": row.get("source_key"),
        "line_number": line_number,
        "status": status,
        "block_reasons": reasons,
        "db_output_text": db_output_text,
        "confirmed_text": confirmed_text,
        "file_line": file_line,
        "db_output_equals_confirmed": db_output_text == confirmed_text,
        "file_line_contains_confirmed": confirmed_text in file_line,
        "db_output_tokens": token_counts(db_output_text),
        "confirmed_tokens": token_counts(confirmed_text),
        "file_line_tokens": token_counts(file_line),
    }


def write_reports(records: list[dict[str, Any]], *, mode: str, applied_count: int) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_culture_religion_parameter_modifier_batch14_output_segments_sync_{mode}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    ready_count = sum(1 for record in records if record["status"] == "ready")
    blocked_count = sum(1 for record in records if record["status"] == "blocked")
    already_synced_count = sum(1 for record in records if record["status"] == "already_synced")
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "generated_at": now(),
        "mode": mode,
        "segment_ids": list(SEGMENT_IDS),
        "ready_count": ready_count,
        "blocked_count": blocked_count,
        "already_synced_count": already_synced_count,
        "applied_count": applied_count,
        "source_changed": False,
        "output_files_changed": False,
        "output_segments_changed": applied_count > 0,
        "run_segment_state_now": False,
        "run_reindex_now": False,
        "run_production_full_now": False,
        "records": records,
    }
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Culture/religion parameter modifier batch14 output_segments sync",
        f"rule_version={RULE_VERSION}",
        f"mode={mode}",
        f"ready_count={ready_count}",
        f"blocked_count={blocked_count}",
        f"already_synced_count={already_synced_count}",
        f"applied_count={applied_count}",
        "source_changed=false",
        "output_files_changed=false",
        f"output_segments_changed={str(applied_count > 0).lower()}",
    ]
    for record in records:
        lines.append("")
        lines.append(f"- segment_id={record['segment_id']} status={record['status']} key={record['source_key']}")
        lines.append(f"  block_reasons={record['block_reasons']}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    mode = "apply" if args.apply else "dry_run"

    with db.connect(db.load_settings()) as conn:
        db.ensure_database(conn)
        conn.row_factory = sqlite3.Row
        records = [evaluate(row) for row in fetch_rows(conn)]
        ready = [record for record in records if record["status"] == "ready"]
        blocked = [record for record in records if record["status"] == "blocked"]
        applied_count = 0
        if args.apply:
            if blocked or not ready:
                raise SystemExit(f"sync blocked: ready={len(ready)} blocked={len(blocked)}")
            for record in ready:
                conn.execute(
                    """
                    UPDATE output_segments
                    SET portuguese_text = ?,
                        output_raw_line = ?,
                        portuguese_hash = ?,
                        last_indexed_at = ?
                    WHERE segment_id = ?
                    """,
                    (
                        record["confirmed_text"],
                        record["file_line"],
                        stable_hash(record["confirmed_text"]),
                        now(),
                        record["segment_id"],
                    ),
                )
            applied_count = len(ready)
            conn.commit()

    txt_path, jsonl_path, summary_path = write_reports(records, mode=mode, applied_count=applied_count)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"mode={mode}")
    print(f"ready_count={sum(1 for record in records if record['status'] == 'ready')}")
    print(f"blocked_count={sum(1 for record in records if record['status'] == 'blocked')}")
    print(f"already_synced_count={sum(1 for record in records if record['status'] == 'already_synced')}")
    print(f"applied_count={applied_count}")
    print("source_changed=false")
    print("output_files_changed=false")
    print(f"output_segments_changed={str(applied_count > 0).lower()}")


if __name__ == "__main__":
    main()
