from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_pending_apply_run540_protected_apply_v1"
INPUT_JSONL = Path("reports/20260702_113133_782732_domain_policy_vote_candidate_pending_apply_run540_diff_preview.jsonl")
EXPECTED_READY = 11
EXCLUDED_SEGMENT_IDS = {120831}


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


def backup_root() -> Path:
    path = db.project_path("memory/backups") / f"pending_apply_run540_{stamp()}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Protected apply for run 540 pending_apply_confirmed ready rows.")
    parser.add_argument("--input-jsonl", type=Path, default=INPUT_JSONL)
    parser.add_argument("--expected-ready", type=int, default=EXPECTED_READY)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db.get_database_path(db.load_settings()), timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def ready_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ready = [
        row
        for row in rows
        if row.get("record_type") == "diff_preview"
        and bool(row.get("ready_for_protected_apply"))
        and int(row.get("segment_id") or 0) not in EXCLUDED_SEGMENT_IDS
    ]
    return sorted(ready, key=lambda row: int(row["segment_id"]))


def validate_records(records: list[dict[str, Any]], expected_ready: int) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    root = output_root()
    if len(records) != expected_ready:
        raise SystemExit(f"ready count guard failed: expected {expected_ready}, got {len(records)}")
    for row in records:
        reasons: list[str] = []
        segment_id = int(row["segment_id"])
        relative_path = str(row["relative_path"])
        output_path = root / relative_path
        output_line_number = int(row.get("output_line_number") or 0)
        current_file_line = str(row.get("current_file_line") or "")
        preview_new_file_line = str(row.get("preview_new_file_line") or "")
        confirmed_text = str(row.get("confirmed_text") or "")
        if segment_id in EXCLUDED_SEGMENT_IDS:
            reasons.append("explicitly_excluded_segment")
        if not bool(row.get("token_integrity_ok")):
            reasons.append("token_integrity_not_ok")
        if not bool(row.get("structure_integrity_ok")):
            reasons.append("structure_integrity_not_ok")
        if not bool(row.get("current_output_guard_ok")):
            reasons.append("current_output_guard_not_ok")
        if not preview_new_file_line:
            reasons.append("missing_preview_new_file_line")
        if output_line_number <= 0:
            reasons.append("missing_output_line_number")
        if not output_path.exists():
            reasons.append("missing_output_file")
        disk_line = ""
        if output_path.exists() and output_line_number > 0:
            lines = output_path.read_text(encoding="utf-8-sig").splitlines()
            index = output_line_number - 1
            if index < 0 or index >= len(lines):
                reasons.append("line_out_of_range")
            else:
                disk_line = lines[index]
                if disk_line != current_file_line:
                    reasons.append("disk_line_mismatch_preview_current_line")
        validated.append(
            {
                **row,
                "status": "ready" if not reasons else "blocked",
                "block_reasons": reasons,
                "output_path": str(output_path),
                "disk_line": disk_line,
                "confirmed_text_hash": stable_hash(confirmed_text),
            }
        )
    return validated


def apply_records(records: list[dict[str, Any]], backup: Path) -> tuple[int, int]:
    root = output_root()
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_path[str(record["relative_path"])].append(record)
    files_touched = 0
    applied = 0
    with connect() as conn:
        for relative_path, path_records in sorted(by_path.items()):
            output_path = root / relative_path
            backup_path = backup / relative_path
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output_path, backup_path)
            lines = output_path.read_text(encoding="utf-8-sig").splitlines()
            for record in path_records:
                index = int(record["output_line_number"]) - 1
                if lines[index] != record["current_file_line"]:
                    raise SystemExit(f"disk line changed during apply for segment {record['segment_id']}")
                lines[index] = str(record["preview_new_file_line"])
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
                        str(record["confirmed_text"]),
                        str(record["preview_new_file_line"]),
                        str(record["confirmed_text_hash"]),
                        now(),
                        int(record["segment_id"]),
                    ),
                )
                applied += 1
            output_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
            files_touched += 1
        conn.commit()
    return applied, files_touched


def post_validate(records: list[dict[str, Any]]) -> dict[str, Any]:
    root = output_root()
    file_ok = 0
    db_ok = 0
    details: list[dict[str, Any]] = []
    with connect() as conn:
        for record in records:
            segment_id = int(record["segment_id"])
            output_path = root / str(record["relative_path"])
            lines = output_path.read_text(encoding="utf-8-sig").splitlines()
            disk_line = lines[int(record["output_line_number"]) - 1]
            row = conn.execute(
                "SELECT portuguese_text, output_raw_line, portuguese_hash FROM output_segments WHERE segment_id = ?",
                (segment_id,),
            ).fetchone()
            file_matches = disk_line == str(record["preview_new_file_line"])
            db_matches = bool(
                row
                and row["portuguese_text"] == str(record["confirmed_text"])
                and row["output_raw_line"] == str(record["preview_new_file_line"])
                and row["portuguese_hash"] == str(record["confirmed_text_hash"])
            )
            file_ok += int(file_matches)
            db_ok += int(db_matches)
            details.append(
                {
                    "segment_id": segment_id,
                    "file_matches": file_matches,
                    "db_matches": db_matches,
                }
            )
    return {"file_ok": file_ok, "db_ok": db_ok, "details": details}


def write_reports(summary: dict[str, Any], records: list[dict[str, Any]]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_pending_apply_run540_protected_apply_{summary['mode']}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Pending apply run540 protected apply",
        f"mode={summary['mode']}",
        f"ready_count={summary['ready_count']}",
        f"blocked_count={summary['blocked_count']}",
        f"applied_count={summary['applied_count']}",
        f"files_touched_count={summary['files_touched_count']}",
        f"backup_root={summary['backup_root']}",
        f"post_validation_file_ok={summary['post_validation'].get('file_ok')}",
        f"post_validation_db_ok={summary['post_validation'].get('db_ok')}",
        f"rollback_path={summary['rollback_path']}",
        "",
        "Operation counters:",
        "candidate_generation_count=0",
        f"apply_count={summary['applied_count']}",
        "lifecycle_count=0",
        "segment_state_count=0",
        "reindex_count=0",
        "production_full_count=0",
    ]
    for record in records:
        lines.append("")
        lines.append(f"- segment_id={record['segment_id']} status={record['status']} key={record['source_key']}")
        lines.append(f"  block_reasons={record['block_reasons']}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    mode = "apply" if args.apply else "dry_run"
    rows = read_jsonl(args.input_jsonl)
    records = validate_records(ready_records(rows), args.expected_ready)
    blocked = [record for record in records if record["status"] != "ready"]
    if blocked:
        raise SystemExit(f"protected apply blocked: {len(blocked)}")

    backup = backup_root() if args.apply else None
    applied_count = 0
    files_touched_count = len({str(record["relative_path"]) for record in records})
    post_validation: dict[str, Any] = {"file_ok": 0, "db_ok": 0, "details": []}
    if args.apply:
        assert backup is not None
        applied_count, files_touched_count = apply_records(records, backup)
        post_validation = post_validate(records)

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": mode,
        "input_jsonl": str(args.input_jsonl),
        "excluded_segment_ids": sorted(EXCLUDED_SEGMENT_IDS),
        "ready_count": len(records),
        "blocked_count": len(blocked),
        "applied_count": applied_count,
        "files_touched_count": files_touched_count,
        "backup_root": str(backup) if backup else None,
        "rollback_path": f"restore files from {backup}" if backup else "not_created_dry_run",
        "post_validation": post_validation,
        "candidate_generation_count": 0,
        "apply_count": applied_count,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": bool(args.apply and applied_count > 0),
        "output_segments_changed": bool(args.apply and applied_count > 0),
        "production_full_recommended_now": False,
        "single_operational_recommendation": (
            "Apply completed and post-validation passed. Next: run only segment-state and delta."
            if args.apply and post_validation.get("file_ok") == len(records) and post_validation.get("db_ok") == len(records)
            else "Dry-run passed. Re-run with --apply to apply the 11 ready rows with backup."
            if not args.apply
            else "Apply completed but post-validation is not clean; use rollback path before further state work."
        ),
    }
    txt_path, jsonl_path, summary_path = write_reports(summary, records)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"ready_count={summary['ready_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"applied_count={summary['applied_count']}")
    print(f"files_touched_count={summary['files_touched_count']}")
    print(f"post_validation_file_ok={summary['post_validation'].get('file_ok')}")
    print(f"post_validation_db_ok={summary['post_validation'].get('db_ok')}")
    print("candidate_generation_count=0")
    print(f"apply_count={summary['apply_count']}")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
