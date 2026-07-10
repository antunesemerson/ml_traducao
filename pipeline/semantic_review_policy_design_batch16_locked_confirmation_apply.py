from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from semantic_review_policy_design_batch16_locked_confirmation_diff_preview import (
    RULE_VERSION as PREVIEW_RULE_VERSION,
    SEGMENT_IDS,
    SEGMENT_STATE_RUN_ID,
    evaluate,
    fetch_rows,
    reports_dir,
    sha256_text,
)


RULE_VERSION = "semantic_review_policy_design_batch16_locked_confirmation_apply_v1"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def backup_root() -> Path:
    return db.project_path("memory/backups") / f"{stamp()}_semantic_review_policy_design_batch16_locked_confirmation"


def apply_records(conn, records: list[dict[str, Any]]) -> dict[str, str]:
    backup_dir = backup_root()
    backups: dict[str, str] = {}
    by_path: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_path.setdefault(record["output_path"], []).append(record)
    for output_path, path_records in by_path.items():
        path = Path(output_path)
        relative_path = Path(path_records[0]["relative_path"])
        backup_file = backup_dir / relative_path
        backup_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_file)
        backups[str(relative_path)] = str(backup_file)
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        for record in path_records:
            index = int(record["output_line_number"]) - 1
            if lines[index] != record["old_line"]:
                raise RuntimeError(f"stale disk line before apply: {record['segment_id']}")
            lines[index] = record["new_line"]
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
                    record["new_line"],
                    sha256_text(record["confirmed_text"]),
                    now(),
                    record["segment_id"],
                ),
            )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return backups


def write_reports(records: list[dict[str, Any]], mode: str, backups: dict[str, str] | None) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_semantic_review_policy_design_batch16_locked_confirmation_apply_{mode}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    ready_count = sum(1 for record in records if record["status"] == "ready_for_human_approval")
    blocked_count = sum(1 for record in records if record["status"] == "blocked")
    applied_count = ready_count if mode == "apply" and blocked_count == 0 else 0
    summary = {
        "schema_version": 1,
        "rule_version": RULE_VERSION,
        "preview_rule_version": PREVIEW_RULE_VERSION,
        "generated_at": now(),
        "mode": mode,
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "segment_ids": list(SEGMENT_IDS),
        "ready_count": ready_count,
        "blocked_count": blocked_count,
        "applied_count": applied_count,
        "source_changed": False,
        "output_changed": applied_count > 0,
        "database_output_segments_changed": applied_count > 0,
        "backup_paths": backups or {},
        "rollback_paths": backups or {},
        "runs_segment_state": False,
        "runs_lifecycle": False,
        "production_full_recommended_now": False,
        "records": records,
    }
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "semantic review policy design batch16 locked confirmation protected apply",
        f"rule_version={RULE_VERSION}",
        f"mode={mode}",
        f"ready_count={ready_count}",
        f"blocked_count={blocked_count}",
        f"applied_count={applied_count}",
        f"source_changed=false",
        f"output_changed={str(applied_count > 0).lower()}",
        f"database_output_segments_changed={str(applied_count > 0).lower()}",
        "",
        "Diff previews:",
    ]
    for record in records:
        lines.extend(["", f"- {record['segment_id']} | {record['status']} | {record['source_key']}", record["unified_diff"]])
    lines.extend(["", "production_full_recommended_now=false"])
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    mode = "apply" if args.apply else "dry_run"
    with db.connect(db.load_settings()) as conn:
        db.ensure_database(conn)
        records = [evaluate(row) for row in fetch_rows(conn)]
        blocked = [record for record in records if record["status"] == "blocked"]
        ready = [record for record in records if record["status"] == "ready_for_human_approval"]
        backups = None
        if args.apply:
            if blocked or len(ready) != len(SEGMENT_IDS):
                raise SystemExit(f"apply blocked: ready={len(ready)} blocked={len(blocked)}")
            backups = apply_records(conn, ready)
            conn.commit()
    txt_path, jsonl_path, summary_path = write_reports(records, mode, backups)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"mode={mode}")
    print(f"ready_count={len(ready)}")
    print(f"blocked_count={len(blocked)}")
    print(f"applied_count={len(ready) if args.apply and not blocked else 0}")
    print(f"output_changed={str(bool(args.apply and ready and not blocked)).lower()}")
    print(f"database_output_segments_changed={str(bool(args.apply and ready and not blocked)).lower()}")
    print(f"backup_paths={json.dumps(backups or {}, ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
