from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import shutil
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens, replace_quoted_text


RULE_VERSION = "holy_site_effect_name_singular_token_protected_apply_v1"
POLICY_KEY = "holy_site_effect_name_singular_token_allowlist_policy"
SEGMENT_STATE_RUN_ID = 526
SEGMENT_IDS = [237388, 239477, 239479, 239507, 239509, 239511]
OUTPUT_TOKEN = "[holy_sites|lE]"
CONFIRMED_TOKEN = "[holy_site|lE]"
SOURCE_KEY_RE = re.compile(r"^holy_site_[a-z0-9_]+_effect_name$")
VAR_RE = re.compile(r"\$holy_site_[a-z0-9_]+_name\$")


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
    return db.project_path("memory/backups") / f"{stamp()}_holy_site_effect_name_singular_token"


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def token_counts(value: str) -> dict[str, int]:
    return dict(sorted(protected_tokens(value).items()))


def file_encoding(path: Path) -> str:
    with path.open("rb") as handle:
        prefix = handle.read(3)
    return "utf-8-sig" if prefix == b"\xef\xbb\xbf" else "utf-8"


def read_lines(path: Path) -> list[str]:
    with path.open("r", encoding=file_encoding(path), newline="") as handle:
        return handle.readlines()


def write_lines(path: Path, lines: list[str]) -> None:
    with path.open("w", encoding=file_encoding(path), newline="") as handle:
        handle.writelines(lines)


def line_without_newline(value: str) -> str:
    return value.rstrip("\r\n")


def line_ending(value: str) -> str:
    if value.endswith("\r\n"):
        return "\r\n"
    if value.endswith("\n"):
        return "\n"
    if value.endswith("\r"):
        return "\r"
    return ""


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
            c.confirmed_text,
            c.confirmation_level,
            c.confirmation_source,
            c.confirmation_label,
            c.locked,
            state.final_state,
            state.state_group,
            state.needs_output_apply,
            state.confirmed_matches_output,
            state.is_closed
        FROM source_segments s
        JOIN output_segments o ON o.segment_id = s.id
        JOIN segment_confirmations c ON c.segment_id = s.id
        JOIN segment_state_items state ON state.segment_id = s.id AND state.run_id = ?
        WHERE s.id IN ({placeholders})
        ORDER BY s.id
        """,
        (SEGMENT_STATE_RUN_ID, *SEGMENT_IDS),
    ).fetchall()
    found = {int(row["segment_id"]) for row in rows}
    if found != set(SEGMENT_IDS):
        raise SystemExit(f"segment id guard failed: {sorted(found)}")
    return [dict(row) for row in rows]


def policy_materialized(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT notes_json FROM ml_agent_registry WHERE agent_key = ? AND status = 'active'",
        (POLICY_KEY,),
    ).fetchone()
    if row is None:
        return False
    notes = json.loads(row["notes_json"] or "{}")
    return (
        notes.get("allowed_exact_change") == {"from": OUTPUT_TOKEN, "to": CONFIRMED_TOKEN}
        and notes.get("candidate_generation_allowed") is False
        and notes.get("auto_apply_allowed") is False
        and notes.get("lifecycle_allowed") is False
        and notes.get("production_release_allowed") is False
    )


def evaluate(row: dict[str, Any], *, policy_ok: bool) -> dict[str, Any]:
    reasons: list[str] = []
    segment_id = int(row["segment_id"])
    relative_path = str(row["relative_path"] or "")
    source_key = str(row["source_key"] or "")
    output_text = str(row["output_text"] or "")
    confirmed_text = str(row["confirmed_text"] or "")
    output_vars = VAR_RE.findall(output_text)
    confirmed_vars = VAR_RE.findall(confirmed_text)
    path = output_root() / Path(relative_path)
    output_line_number = int(row["output_line_number"] or 0)
    old_line = str(row["output_raw_line"] or "")
    new_line = ""

    if not policy_ok:
        reasons.append("allowlist_policy_not_materialized")
    if segment_id not in SEGMENT_IDS:
        reasons.append("segment_not_allowlisted")
    if not SOURCE_KEY_RE.match(source_key):
        reasons.append("source_key_not_effect_name")
    if row["final_state"] != "pending_apply_confirmed":
        reasons.append("state_not_pending_apply_confirmed")
    if row["state_group"] != "pending" or int(row["is_closed"] or 0) != 0:
        reasons.append("state_not_open_pending")
    if int(row["needs_output_apply"] or 0) != 1:
        reasons.append("needs_output_apply_not_1")
    if int(row["confirmed_matches_output"] or 0) != 0:
        reasons.append("confirmed_matches_output_not_0")
    if OUTPUT_TOKEN not in output_text or CONFIRMED_TOKEN not in confirmed_text:
        reasons.append("expected_token_missing")
    if confirmed_text != output_text.replace(OUTPUT_TOKEN, CONFIRMED_TOKEN):
        reasons.append("text_change_not_exact_allowlist")
    if output_vars != confirmed_vars or len(output_vars) != 1:
        reasons.append("holy_site_variable_not_preserved")
    if "#weak (" not in output_text or ")#!" not in output_text or "#weak (" not in confirmed_text or ")#!" not in confirmed_text:
        reasons.append("weak_wrapper_not_preserved")
    output_tokens = protected_tokens(output_text)
    confirmed_tokens = protected_tokens(confirmed_text)
    expected_output_tokens = Counter(output_tokens)
    expected_output_tokens[OUTPUT_TOKEN] -= 1
    if expected_output_tokens[OUTPUT_TOKEN] == 0:
        del expected_output_tokens[OUTPUT_TOKEN]
    expected_output_tokens[CONFIRMED_TOKEN] += 1
    if expected_output_tokens != confirmed_tokens:
        reasons.append("token_delta_not_exact")
    if not path.exists():
        reasons.append("output_file_missing")
    if output_line_number <= 0:
        reasons.append("output_line_missing")
    if path.exists() and output_line_number > 0:
        lines = read_lines(path)
        index = output_line_number - 1
        if index < 0 or index >= len(lines):
            reasons.append("output_line_out_of_range")
        else:
            disk_line = line_without_newline(lines[index])
            if disk_line != old_line:
                reasons.append("disk_line_mismatch_output_raw_line")
            if output_text not in disk_line:
                reasons.append("disk_line_does_not_contain_current_output")
            try:
                new_line = replace_quoted_text(disk_line, confirmed_text)
            except ValueError:
                reasons.append("line_without_quoted_value")

    status = "ready" if not reasons and output_text != confirmed_text else "blocked"
    if not reasons and output_text == confirmed_text:
        status = "already_applied"
    return {
        "segment_id": segment_id,
        "status": status,
        "block_reasons": reasons,
        "policy_key": POLICY_KEY,
        "policy_materialized": policy_ok,
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "relative_path": relative_path,
        "source_key": source_key,
        "source_line_number": row["source_line_number"],
        "output_path": str(path),
        "output_line_number": output_line_number,
        "english_text": row["english_text"],
        "spanish_text": row["spanish_text"],
        "current_output_text": output_text,
        "confirmed_text": confirmed_text,
        "old_line": old_line,
        "new_line": new_line,
        "current_output_hash": sha256_text(output_text),
        "confirmed_text_hash": sha256_text(confirmed_text),
        "output_tokens": token_counts(output_text),
        "confirmed_tokens": token_counts(confirmed_text),
        "token_integrity_ok": "token_delta_not_exact" not in reasons,
        "structure_integrity_ok": not any(reason in reasons for reason in ["holy_site_variable_not_preserved", "weak_wrapper_not_preserved"]),
        "diff_preview": unified_diff(old_line, new_line, relative_path, output_line_number) if new_line else "",
        "apply_decision": "not_run",
        "applied": False,
        "rollback_executed": False,
    }


def apply_records(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> dict[str, str]:
    backup_dir = backup_root()
    backup_paths: dict[str, str] = {}
    by_path: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_path.setdefault(record["output_path"], []).append(record)
    for output_path, path_records in by_path.items():
        path = Path(output_path)
        relative_path = Path(path_records[0]["relative_path"])
        backup_path = backup_dir / relative_path
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_path)
        backup_paths[str(relative_path)] = str(backup_path)
        lines = read_lines(path)
        for record in sorted(path_records, key=lambda item: int(item["output_line_number"])):
            index = int(record["output_line_number"]) - 1
            if line_without_newline(lines[index]) != record["old_line"]:
                raise RuntimeError(f"stale output line before apply: {record['segment_id']}")
            lines[index] = record["new_line"] + line_ending(lines[index])
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
                    record["confirmed_text_hash"],
                    now(),
                    record["segment_id"],
                ),
            )
            record["apply_decision"] = "applied"
            record["applied"] = True
        write_lines(path, lines)
    return backup_paths


def post_validate(records: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    failures: list[str] = []
    with connect_readonly() as conn:
        for record in records:
            row = conn.execute(
                "SELECT portuguese_text, output_raw_line, portuguese_hash FROM output_segments WHERE segment_id = ?",
                (record["segment_id"],),
            ).fetchone()
            if row is None:
                failures.append(f"{record['segment_id']}:missing_output_segment")
                continue
            path = Path(record["output_path"])
            lines = read_lines(path)
            actual_line = line_without_newline(lines[int(record["output_line_number"]) - 1])
            if actual_line != record["new_line"]:
                failures.append(f"{record['segment_id']}:disk_line_mismatch")
            if row["portuguese_text"] != record["confirmed_text"]:
                failures.append(f"{record['segment_id']}:db_text_mismatch")
            if row["output_raw_line"] != record["new_line"]:
                failures.append(f"{record['segment_id']}:db_raw_line_mismatch")
            if row["portuguese_hash"] != record["confirmed_text_hash"]:
                failures.append(f"{record['segment_id']}:db_hash_mismatch")
    return not failures, failures


def rollback(records: list[dict[str, Any]], backup_paths: dict[str, str]) -> None:
    for relative_path, backup_path in backup_paths.items():
        shutil.copy2(backup_path, output_root() / Path(relative_path))
    for record in records:
        record["rollback_executed"] = True
        if not record["applied"]:
            record["apply_decision"] = "rollback_after_error"


def write_reports(records: list[dict[str, Any]], *, mode: str, backup_paths: dict[str, str], post_ok: bool, post_failures: list[str]) -> tuple[Path, Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_holy_site_effect_name_singular_token_protected_apply_{mode}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    diff_path = Path(str(base) + ("_applied_diff.md" if mode == "apply" else "_diff_preview.md"))
    ready_count = sum(1 for record in records if record["status"] == "ready")
    blocked_count = sum(1 for record in records if record["status"] == "blocked")
    applied_count = sum(1 for record in records if record["applied"])
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "mode": mode,
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "segment_ids": SEGMENT_IDS,
        "ready_count": ready_count,
        "blocked_count": blocked_count,
        "applied_count": applied_count,
        "guard_fail_count": blocked_count,
        "token_integrity_ok_count": sum(1 for record in records if record["token_integrity_ok"]),
        "structure_integrity_ok_count": sum(1 for record in records if record["structure_integrity_ok"]),
        "snapshot_count": len(backup_paths),
        "backup_paths": backup_paths,
        "rollback_paths": backup_paths,
        "rollback_executed": any(record["rollback_executed"] for record in records),
        "post_validation_ok": post_ok,
        "post_validation_failures": post_failures,
        "candidate_generation_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": applied_count > 0,
        "production_full_recommended_now": False,
    }
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    diff_lines = [f"# Holy-site effect name singular token protected apply {mode}", ""]
    for record in records:
        diff_lines.extend(
            [
                f"## Segment {record['segment_id']} / {record['source_key']}",
                "",
                f"Status: `{record['status']}`",
                f"Apply: `{record['apply_decision']}`",
                "",
                "```diff",
                record["diff_preview"],
                "```",
                "",
            ]
        )
    diff_path.write_text("\n".join(diff_lines), encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                f"holy-site protected apply {mode}",
                f"ready_count={ready_count}",
                f"blocked_count={blocked_count}",
                f"applied_count={applied_count}",
                f"snapshot_count={len(backup_paths)}",
                f"rollback_executed={summary['rollback_executed']}",
                f"post_validation_ok={post_ok}",
                "candidate_generation_count=0",
                "lifecycle_count=0",
                "segment_state_count=0",
                "reindex_count=0",
                "production_full_count=0",
                "source_changed=false",
                f"output_changed={str(applied_count > 0).lower()}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return txt_path, jsonl_path, summary_path, diff_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    mode = "apply" if args.apply else "dry_run"
    with connect_readonly() as conn:
        policy_ok = policy_materialized(conn)
        rows = fetch_rows(conn)
    records = [evaluate(row, policy_ok=policy_ok) for row in rows]
    ready = [record for record in records if record["status"] == "ready"]
    blocked = [record for record in records if record["status"] == "blocked"]
    backup_paths: dict[str, str] = {}
    post_ok = False
    post_failures: list[str] = []
    if args.apply:
        if blocked or len(ready) != len(SEGMENT_IDS):
            raise SystemExit(f"apply blocked: ready={len(ready)} blocked={len(blocked)}")
        with db.connect(db.load_settings()) as conn:
            try:
                backup_paths = apply_records(conn, ready)
                conn.commit()
            except Exception:
                conn.rollback()
                if backup_paths:
                    rollback(records, backup_paths)
                raise
        post_ok, post_failures = post_validate(ready)
        if not post_ok:
            rollback(records, backup_paths)
            raise SystemExit(f"post validation failed: {post_failures}")
    txt_path, jsonl_path, summary_path, diff_path = write_reports(
        records,
        mode=mode,
        backup_paths=backup_paths,
        post_ok=post_ok,
        post_failures=post_failures,
    )
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"diff={diff_path}")
    print(f"mode={mode}")
    print(f"ready_count={len(ready)}")
    print(f"blocked_count={len(blocked)}")
    print(f"applied_count={sum(1 for record in records if record['applied'])}")
    print(f"post_validation_ok={post_ok}")


if __name__ == "__main__":
    main()
