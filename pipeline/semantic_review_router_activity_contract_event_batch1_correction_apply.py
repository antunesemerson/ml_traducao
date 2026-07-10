from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator
from apply_safe_output_updates import protected_tokens, replace_quoted_text


RULE_VERSION = "semantic_review_router_activity_contract_event_batch1_correction_apply_v1"
QUEUE_SOURCE = "semantic-review-router-activity-contract-event-human-review-batch1"
ORIGIN = "human_confirmed_semantic_review_router_activity_contract_event_batch1"
SEGMENT_IDS = (46883,)
SEGMENT_STATE_RUN_ID = 460


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def output_root() -> Path:
    return db.project_path(db.load_settings()["output_spanish"])


def backup_root() -> Path:
    return db.project_path("memory/backups") / f"{stamp()}_semantic_review_router_activity_contract_event_batch1_correction_apply"


def quality_blockers(text: str) -> list[str]:
    validation = local_quality_validator.validate_text(text)
    issues = validation.get("issues") or []
    return sorted(
        {
            str(issue.get("code") or "quality_issue")
            for issue in issues
            if isinstance(issue, dict)
            and str(issue.get("severity") or "").lower() in {"medium", "high", "error", "critical"}
        }
    )


def fetch_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in SEGMENT_IDS)
    rows = conn.execute(
        f"""
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            o.portuguese_text AS output_text,
            o.output_raw_line,
            o.output_line_number,
            c.id AS confirmation_id,
            c.confirmed_text,
            c.confirmation_level,
            c.confirmation_source,
            c.confirmation_label,
            c.locked,
            c.candidate_id,
            lc.run_id AS learning_run_id,
            state.final_state,
            state.state_group,
            state.needs_output_apply
        FROM source_segments s
        JOIN output_segments o ON o.segment_id = s.id
        JOIN local_learning_candidates lc
          ON lc.segment_id = s.id
         AND lc.queue_source = ?
         AND lc.origin = ?
         AND lc.match_type = 'semantic_review_router_activity_contract_event_human_correction'
        JOIN segment_confirmations c
          ON c.segment_id = s.id
         AND c.candidate_id = lc.id
        JOIN segment_state_items state
          ON state.segment_id = s.id
         AND state.run_id = ?
        WHERE s.id IN ({placeholders})
        ORDER BY s.id
        """,
        (QUEUE_SOURCE, ORIGIN, SEGMENT_STATE_RUN_ID, *SEGMENT_IDS),
    ).fetchall()
    found = {int(row["segment_id"]) for row in rows}
    missing = sorted(set(SEGMENT_IDS) - found)
    if missing:
        raise SystemExit(f"missing segment ids or confirmations: {missing}")
    return [dict(row) for row in rows]


def unified_diff(old_line: str, new_line: str, relative_path: str, line_number: int) -> str:
    return "\n".join(
        difflib.unified_diff(
            [old_line],
            [new_line],
            fromfile=f"a/{relative_path}:{line_number}",
            tofile=f"b/{relative_path}:{line_number}",
            lineterm="",
        )
    )


def evaluate(row: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    output_text = str(row.get("output_text") or "")
    confirmed_text = str(row.get("confirmed_text") or "")
    relative_path = str(row.get("relative_path") or "")
    path = output_root() / Path(relative_path)
    output_line_number = int(row.get("output_line_number") or 0)
    raw_line = str(row.get("output_raw_line") or "")
    new_line = ""

    if row.get("confirmation_level") != "human_confirmed":
        reasons.append("confirmation_not_human_confirmed")
    if row.get("confirmation_source") != "local_learning":
        reasons.append("confirmation_source_not_local_learning")
    if int(row.get("locked") or 0) != 1:
        reasons.append("confirmation_not_locked")
    if row.get("state_group") != "pending" or row.get("final_state") != "reopen_auto_confirmed_autofix":
        reasons.append("unexpected_segment_state")
    if int(row.get("needs_output_apply") or 0) != 0:
        reasons.append("needs_output_apply_not_zero")
    if output_text == confirmed_text:
        reasons.append("output_already_matches_confirmation")
    if protected_tokens(output_text) != protected_tokens(confirmed_text):
        reasons.append("protected_token_signature_mismatch")
    quality = quality_blockers(confirmed_text)
    if quality:
        reasons.append("quality_issue:" + ",".join(quality))

    if not path.exists():
        reasons.append("missing_output_file")
    elif output_line_number <= 0:
        reasons.append("missing_output_line_number")
    else:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        index = output_line_number - 1
        if index < 0 or index >= len(lines):
            reasons.append("line_out_of_range")
        else:
            disk_line = lines[index]
            if raw_line and disk_line != raw_line:
                reasons.append("disk_line_mismatch_output_raw_line")
            if output_text not in disk_line:
                reasons.append("disk_line_does_not_contain_current_output")
            try:
                new_line = replace_quoted_text(disk_line, confirmed_text)
            except ValueError:
                reasons.append("line_without_quoted_value")

    status = "ready_for_apply" if not reasons else "blocked"
    return {
        "segment_id": int(row["segment_id"]),
        "status": status,
        "block_reasons": reasons,
        "relative_path": relative_path,
        "source_key": row.get("source_key"),
        "source_line_number": row.get("source_line_number"),
        "output_line_number": output_line_number,
        "output_path": str(path),
        "english_text": row.get("english_text"),
        "spanish_text": row.get("spanish_text"),
        "current_output_text": output_text,
        "confirmed_text": confirmed_text,
        "learning_run_id": row.get("learning_run_id"),
        "candidate_id": row.get("candidate_id"),
        "confirmation_id": row.get("confirmation_id"),
        "current_output_hash": sha256_text(output_text),
        "confirmed_text_hash": sha256_text(confirmed_text),
        "token_integrity_ok": protected_tokens(output_text) == protected_tokens(confirmed_text),
        "quality_blockers": quality,
        "old_line": raw_line,
        "new_line": new_line,
        "unified_diff": unified_diff(raw_line, new_line, relative_path, output_line_number) if new_line else "",
    }


def apply_records(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> dict[str, str]:
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
    base = reports_dir() / f"{stamp()}_semantic_review_router_activity_contract_event_batch1_correction_apply_{mode}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    ready_count = sum(1 for record in records if record["status"] == "ready_for_apply")
    blocked_count = sum(1 for record in records if record["status"] == "blocked")
    applied_count = ready_count if mode == "apply" and blocked_count == 0 else 0
    summary = {
        "schema_version": 1,
        "rule_version": RULE_VERSION,
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
        "next_action": "post_validate_then_materialize_lifecycle" if applied_count else "apply_only_if_human_approved_and_all_guards_ready",
    }
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "semantic review router activity contract event batch1 correction protected apply",
        f"rule_version={RULE_VERSION}",
        f"mode={mode}",
        f"ready_count={ready_count}",
        f"blocked_count={blocked_count}",
        f"applied_count={applied_count}",
        "source_changed=false",
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
        ready = [record for record in records if record["status"] == "ready_for_apply"]
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
