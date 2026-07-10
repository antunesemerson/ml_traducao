from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import domain_policy_vote_candidate_accolade_formula_run515_protected_apply_dry_run as dry


RULE_VERSION = "domain_policy_vote_candidate_accolade_formula_run515_protected_apply_v1"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def backup_root() -> Path:
    return db.project_path("memory/backups") / f"{stamp()}_domain_policy_vote_candidate_accolade_formula_run515"


def group_by_output_path(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(str(record["output_path"]), []).append(record)
    return grouped


def fetch_records(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    decisions_path = dry.latest_decisions_path()
    decisions = dry.read_jsonl(decisions_path)
    if len(decisions) != dry.EXPECTED_APPROVED_COUNT:
        raise SystemExit(f"approved decision count guard failed: {len(decisions)}")
    segment_ids = [int(row["segment_id"]) for row in decisions]
    state_by_id = dry.fetch_state(conn, segment_ids)
    return [dry.evaluate(decision, state_by_id.get(int(decision["segment_id"]))) for decision in decisions]


def apply_records(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> dict[str, str]:
    backup_dir = backup_root()
    backup_paths: dict[str, str] = {}
    for output_path, path_records in group_by_output_path(records).items():
        path = Path(output_path)
        relative_path = Path(path_records[0]["relative_path"])
        backup_file = backup_dir / relative_path
        backup_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_file)
        backup_paths[str(relative_path)] = str(backup_file)

        lines = path.read_text(encoding="utf-8-sig").splitlines()
        for record in sorted(path_records, key=lambda item: int(item["output_line_number"])):
            index = int(record["output_line_number"]) - 1
            if index < 0 or index >= len(lines):
                raise RuntimeError(f"line_out_of_range during apply: {record['segment_id']}")
            if lines[index] != record["disk_line"]:
                raise RuntimeError(f"stale disk line during apply: {record['segment_id']}")
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
                    record["corrected_text"],
                    record["new_line"],
                    record["corrected_text_hash"],
                    now(),
                    record["segment_id"],
                ),
            )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return backup_paths


def post_validate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    with dry.connect_readonly() as conn:
        state_by_id = dry.fetch_state(conn, [int(record["segment_id"]) for record in records])
    validated: list[dict[str, Any]] = []
    for record in records:
        segment_id = int(record["segment_id"])
        state = state_by_id.get(segment_id) or {}
        reasons: list[str] = []
        db_output = str(state.get("output_text") or "")
        confirmed_text = str(state.get("confirmed_text") or "")
        path = Path(record["output_path"])
        line_number = int(record["output_line_number"])
        disk_line = ""
        if db_output != record["corrected_text"]:
            reasons.append("database_output_not_corrected_text")
        if confirmed_text != record["corrected_text"]:
            reasons.append("confirmed_text_not_corrected_text")
        if not path.exists():
            reasons.append("missing_output_file")
        elif line_number <= 0:
            reasons.append("missing_output_line_number")
        else:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
            index = line_number - 1
            if index < 0 or index >= len(lines):
                reasons.append("line_out_of_range")
            else:
                disk_line = lines[index]
                if disk_line != record["new_line"]:
                    reasons.append("disk_line_not_new_line")
                if record["corrected_text"] not in disk_line:
                    reasons.append("disk_line_does_not_contain_corrected_text")
        validated.append(
            {
                "segment_id": segment_id,
                "status": "ok" if not reasons else "failed",
                "reasons": reasons,
                "relative_path": record["relative_path"],
                "source_key": record["source_key"],
                "output_line_number": line_number,
                "corrected_text": record["corrected_text"],
                "database_output_text": db_output,
                "disk_line": disk_line,
            }
        )
    return validated


def write_reports(
    records: list[dict[str, Any]],
    post_validation: list[dict[str, Any]],
    *,
    mode: str,
    backup_paths: dict[str, str],
) -> tuple[Path, Path, Path]:
    ready_count = sum(1 for record in records if record["status"] == "ready")
    blocked_count = sum(1 for record in records if record["status"] == "blocked")
    applied_count = ready_count if mode == "apply" and blocked_count == 0 else 0
    post_failed_count = sum(1 for record in post_validation if record["status"] != "ok")
    block_counter = Counter(reason for record in records for reason in record["block_reasons"])
    summary = {
        "created_at": now(),
        "mode": mode,
        "rule_version": RULE_VERSION,
        "dry_run_rule_version": dry.RULE_VERSION,
        "decision_source": str(dry.latest_decisions_path()),
        "expected_state_run_id": dry.EXPECTED_STATE_RUN_ID,
        "approved_decision_count": len(records),
        "ready_count": ready_count,
        "blocked_count": blocked_count,
        "applied_count": applied_count,
        "post_validation_ok_count": sum(1 for record in post_validation if record["status"] == "ok"),
        "post_validation_failed_count": post_failed_count,
        "false_safe_risk_count": sum(1 for record in records if record["false_safe_risk"]),
        "token_integrity_ok_count": sum(1 for record in records if record["token_integrity_ok"]),
        "structure_integrity_ok_count": sum(1 for record in records if record["structure_integrity_ok"]),
        "requires_apply_later_count": 0 if applied_count else ready_count,
        "requires_lifecycle_later_count": applied_count,
        "source_changed": False,
        "output_changed": applied_count > 0,
        "database_output_segments_changed": applied_count > 0,
        "snapshot_created": bool(backup_paths),
        "backup_paths": backup_paths,
        "rollback_paths": backup_paths,
        "post_validation_required": applied_count > 0,
        "run_segment_state_now": False,
        "run_reindex_now": False,
        "run_production_full_now": False,
        "candidate_generation_count": 0,
        "apply_count": applied_count,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "production_full_recommended_now": False,
        "block_reason_counts": [{"key": key, "count": count} for key, count in block_counter.most_common()],
        "records": records,
        "post_validation": post_validation,
        "output_files": {},
    }
    base = dry.reports_dir() / f"{stamp()}_domain_policy_vote_candidate_accolade_formula_run515_protected_apply_{mode}"
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    txt_path = base.with_suffix(".txt")
    dry.write_jsonl(jsonl_path, records)
    summary["output_files"] = {
        "jsonl": str(jsonl_path),
        "summary_json": str(summary_path),
        "txt": str(txt_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "domain_policy_vote_candidate accolade formula run515 protected apply",
        "",
        f"mode: {mode}",
        f"ready_count: {ready_count}",
        f"blocked_count: {blocked_count}",
        f"applied_count: {applied_count}",
        f"post_validation_failed_count: {post_failed_count}",
        f"output_changed: {str(applied_count > 0).lower()}",
        f"database_output_segments_changed: {str(applied_count > 0).lower()}",
        f"backup_paths: {json.dumps(backup_paths, ensure_ascii=False, sort_keys=True)}",
        "",
        "records:",
    ]
    for record in records:
        validation = next((item for item in post_validation if item["segment_id"] == record["segment_id"]), None)
        lines.extend(
            [
                "",
                f"## segment_id {record['segment_id']} | status={record['status']} | post={validation['status'] if validation else 'not_run'}",
                f"- key: {record['source_key']}",
                f"- output_path: {record['output_path']}",
                f"- output_line_number: {record['output_line_number']}",
                f"- block_reasons: {record['block_reasons']}",
                "```diff",
                record["unified_diff"],
                "```",
            ]
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    mode = "apply" if args.apply else "dry_run"

    settings = db.load_settings()
    backup_paths: dict[str, str] = {}
    with db.connect(settings) as conn:
        conn.row_factory = sqlite3.Row
        db.ensure_database(conn)
        records = fetch_records(conn)
        blocked = [record for record in records if record["status"] == "blocked"]
        ready = [record for record in records if record["status"] == "ready"]
        if args.apply:
            if blocked or len(ready) != len(records):
                raise SystemExit(f"apply blocked: ready={len(ready)} blocked={len(blocked)} total={len(records)}")
            backup_paths = apply_records(conn, ready)
            conn.commit()

    post = post_validate(records) if args.apply else []
    txt_path, jsonl_path, summary_path = write_reports(records, post, mode=mode, backup_paths=backup_paths)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"mode={mode}")
    print(f"ready_count={sum(1 for record in records if record['status'] == 'ready')}")
    print(f"blocked_count={sum(1 for record in records if record['status'] == 'blocked')}")
    print(f"applied_count={len(records) if args.apply else 0}")
    print(f"post_validation_failed_count={sum(1 for record in post if record['status'] != 'ok')}")
    print(f"output_changed={str(args.apply).lower()}")
    print(f"database_output_segments_changed={str(args.apply).lower()}")
    print(f"backup_paths={json.dumps(backup_paths, ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
