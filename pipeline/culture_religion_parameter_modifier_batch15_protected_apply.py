from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator
from apply_safe_output_updates import protected_tokens, replace_quoted_text


RULE_VERSION = "culture_religion_parameter_modifier_batch15_protected_apply_v1"
SEGMENT_IDS = [20449, 20451, 20452, 20453, 20718, 21234, 21362]
EXPECTED_STATE_RUN_ID = 442
CONFIRMATION_SOURCE = "local_learning"
CONFIRMATION_LABEL = "semantic_error"
REVIEWER = "user_human_review_culture_religion_parameter_modifier_batch15"


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
    return db.project_path("memory/backups") / f"{stamp()}_culture_religion_parameter_modifier_batch15"


def token_counts(value: str) -> dict[str, int]:
    return dict(sorted(protected_tokens(value).items()))


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


def fetch_rows(conn, learning_run_id: int) -> list[dict[str, Any]]:
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
            s.old_text,
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
            l.id AS learning_id,
            l.run_id AS learning_run_id,
            l.human_label,
            l.corrected_text,
            l.learned_at,
            l.confirmation_synced_at,
            state.run_id AS state_run_id,
            state.final_state,
            state.state_group,
            state.review_state,
            state.apply_state,
            state.needs_output_apply,
            state.confirmed_matches_output,
            state.needs_human
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        LEFT JOIN segment_confirmations c ON c.segment_id = s.id
        LEFT JOIN local_learning_candidates l ON l.id = c.candidate_id
        LEFT JOIN segment_state_items state
          ON state.segment_id = s.id
         AND state.run_id = ?
        WHERE s.id IN ({placeholders})
        ORDER BY s.id
        """,
        (EXPECTED_STATE_RUN_ID, *SEGMENT_IDS),
    ).fetchall()
    found = {int(row["segment_id"]) for row in rows}
    missing = sorted(set(SEGMENT_IDS) - found)
    if missing:
        raise SystemExit(f"missing segment ids: {missing}")
    output = [dict(row) for row in rows]
    wrong_run = sorted({int(row.get("learning_run_id") or 0) for row in output} - {learning_run_id})
    if wrong_run:
        raise SystemExit(f"learning_run_id mismatch in fetched rows: expected={learning_run_id} found={wrong_run}")
    return output


def evaluate(row: dict[str, Any], learning_run_id: int) -> dict[str, Any]:
    reasons: list[str] = []
    output_text = str(row.get("output_text") or "")
    corrected_text = str(row.get("corrected_text") or row.get("confirmed_text") or "")
    relative_path = str(row.get("relative_path") or "")
    path = output_root() / Path(relative_path)
    output_line_number = int(row.get("output_line_number") or 0)
    raw_line = str(row.get("output_raw_line") or "")
    new_line = ""

    if int(row.get("learning_run_id") or 0) != learning_run_id:
        reasons.append("learning_run_id_mismatch")
    if row.get("confirmed_text") != corrected_text:
        reasons.append("confirmed_text_not_equal_corrected_text")
    if row.get("confirmation_level") != "human_confirmed":
        reasons.append("confirmation_not_human_confirmed")
    if row.get("confirmation_source") != CONFIRMATION_SOURCE:
        reasons.append("confirmation_source_mismatch")
    if row.get("confirmation_label") != CONFIRMATION_LABEL:
        reasons.append("confirmation_label_mismatch")
    if int(row.get("locked") or 0) != 1:
        reasons.append("confirmation_not_locked")
    if row.get("human_label") != CONFIRMATION_LABEL:
        reasons.append("learning_human_label_mismatch")
    if row.get("state_group") != "pending" or row.get("final_state") != "reopen_auto_confirmed_autofix":
        reasons.append("unexpected_segment_state")
    if not path.exists():
        reasons.append("missing_output_file")
    if output_line_number <= 0:
        reasons.append("missing_output_line_number")

    output_tokens = protected_tokens(output_text)
    corrected_tokens = protected_tokens(corrected_text)
    if output_tokens != corrected_tokens:
        reasons.append("protected_token_signature_mismatch")

    quality = quality_blockers(corrected_text)
    if quality:
        reasons.append("quality_issue:" + ",".join(quality))

    if path.exists() and output_line_number > 0:
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
                new_line = replace_quoted_text(disk_line, corrected_text)
            except ValueError:
                reasons.append("line_without_quoted_value")

    status = "ready" if not reasons and output_text != corrected_text else "blocked"
    if output_text == corrected_text and not reasons:
        status = "already_applied"

    return {
        "segment_id": int(row["segment_id"]),
        "status": status,
        "block_reasons": reasons,
        "state_run_id": EXPECTED_STATE_RUN_ID,
        "learning_run_id": learning_run_id,
        "relative_path": relative_path,
        "source_key": row.get("source_key"),
        "source_line_number": row.get("source_line_number"),
        "output_line_number": output_line_number,
        "output_path": str(path),
        "english_text": row.get("english_text"),
        "spanish_text": row.get("spanish_text"),
        "current_output_text": output_text,
        "confirmed_text": row.get("confirmed_text"),
        "corrected_text": corrected_text,
        "current_output_hash": sha256_text(output_text),
        "corrected_text_hash": sha256_text(corrected_text),
        "output_tokens": token_counts(output_text),
        "corrected_tokens": token_counts(corrected_text),
        "token_integrity_ok": output_tokens == corrected_tokens,
        "quality_blockers": quality,
        "old_line": raw_line,
        "new_line": new_line,
        "unified_diff": unified_diff(raw_line, new_line, relative_path, output_line_number) if new_line else "",
        "confirmation": {
            "id": row.get("confirmation_id"),
            "level": row.get("confirmation_level"),
            "source": row.get("confirmation_source"),
            "label": row.get("confirmation_label"),
            "locked": int(row.get("locked") or 0),
            "candidate_id": row.get("candidate_id"),
        },
        "learning": {
            "id": row.get("learning_id"),
            "run_id": row.get("learning_run_id"),
            "human_label": row.get("human_label"),
            "learned_at": row.get("learned_at"),
            "confirmation_synced_at": row.get("confirmation_synced_at"),
        },
        "state": {
            "final_state": row.get("final_state"),
            "state_group": row.get("state_group"),
            "review_state": row.get("review_state"),
            "apply_state": row.get("apply_state"),
            "needs_output_apply": int(row.get("needs_output_apply") or 0),
            "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
            "needs_human": int(row.get("needs_human") or 0),
        },
    }


def apply_records(conn, records: list[dict[str, Any]]) -> dict[str, str]:
    backup_dir = backup_root()
    backup_by_relative_path: dict[str, str] = {}
    records_by_path: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        records_by_path.setdefault(record["output_path"], []).append(record)

    for output_path, path_records in records_by_path.items():
        path = Path(output_path)
        relative_path = Path(path_records[0]["relative_path"])
        backup_file = backup_dir / relative_path
        backup_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_file)
        backup_by_relative_path[str(relative_path)] = str(backup_file)

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
                    record["corrected_text"],
                    record["new_line"],
                    record["corrected_text_hash"],
                    now(),
                    record["segment_id"],
                ),
            )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")

    return backup_by_relative_path


def write_reports(records: list[dict[str, Any]], *, mode: str, backup_paths: dict[str, str] | None) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_culture_religion_parameter_modifier_batch15_protected_apply_{mode}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    ready_count = sum(1 for record in records if record["status"] == "ready")
    blocked_count = sum(1 for record in records if record["status"] == "blocked")
    already_applied_count = sum(1 for record in records if record["status"] == "already_applied")
    applied_count = ready_count if mode == "apply" and blocked_count == 0 else 0
    output_segments_match_count = sum(
        1 for record in records if record["status"] == "ready" and record["corrected_text_hash"] == sha256_text(record["corrected_text"])
    )
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "generated_at": now(),
        "mode": mode,
        "segment_ids": SEGMENT_IDS,
        "expected_state_run_id": EXPECTED_STATE_RUN_ID,
        "ready_count": ready_count,
        "applied_count": applied_count,
        "blocked_count": blocked_count,
        "already_applied_count": already_applied_count,
        "output_segments_match_count": output_segments_match_count if mode == "apply" else None,
        "source_changed": False,
        "output_changed": applied_count > 0,
        "database_output_segments_changed": applied_count > 0,
        "backup_paths": backup_paths or {},
        "rollback_paths": backup_paths or {},
        "post_validation_required": applied_count > 0,
        "run_segment_state_after_apply": applied_count > 0,
        "run_production_full_now": False,
        "run_reindex_now": False,
        "records": records,
    }
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Culture/religion parameter modifier batch15 protected apply",
        f"rule_version={RULE_VERSION}",
        f"mode={mode}",
        f"ready_count={ready_count}",
        f"blocked_count={blocked_count}",
        f"already_applied_count={already_applied_count}",
        f"applied_count={applied_count}",
        "",
        "Diff previews:",
    ]
    for record in records:
        lines.extend(
            [
                "",
                f"- segment_id={record['segment_id']} status={record['status']} key={record['source_key']}",
                f"  block_reasons={record['block_reasons']}",
                record["unified_diff"],
            ]
        )
    lines.extend(
        [
            "",
            "source_changed=false",
            f"output_changed={str(applied_count > 0).lower()}",
            f"database_output_segments_changed={str(applied_count > 0).lower()}",
            f"backup_paths={json.dumps(backup_paths or {}, ensure_ascii=False, sort_keys=True)}",
            "production_full_recommended_now=false",
            "reindex_recommended_now=false",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--learning-run-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    mode = "apply" if args.apply else "dry_run"

    with db.connect(db.load_settings()) as conn:
        db.ensure_database(conn)
        records = [evaluate(row, args.learning_run_id) for row in fetch_rows(conn, args.learning_run_id)]
        blocked = [record for record in records if record["status"] == "blocked"]
        ready = [record for record in records if record["status"] == "ready"]
        backup_paths: dict[str, str] | None = None
        if args.apply:
            if blocked or len(ready) != len(SEGMENT_IDS):
                raise SystemExit(f"apply blocked: ready={len(ready)} blocked={len(blocked)}")
            backup_paths = apply_records(conn, ready)
            conn.commit()

    txt_path, jsonl_path, summary_path = write_reports(records, mode=mode, backup_paths=backup_paths)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"mode={mode}")
    print(f"ready_count={len(ready)}")
    print(f"applied_count={len(ready) if args.apply and not blocked else 0}")
    print(f"blocked_count={len(blocked)}")
    print(f"already_applied_count={sum(1 for record in records if record['status'] == 'already_applied')}")
    print(f"output_changed={str(bool(args.apply and ready and not blocked)).lower()}")
    print(f"database_output_segments_changed={str(bool(args.apply and ready and not blocked)).lower()}")
    print(f"backup_paths={json.dumps(backup_paths or {}, ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
