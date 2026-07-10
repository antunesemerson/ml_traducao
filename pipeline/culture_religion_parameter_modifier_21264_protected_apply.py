from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator
from apply_safe_output_updates import protected_tokens, replace_quoted_text


RULE_VERSION = "culture_religion_parameter_modifier_21264_protected_apply_v1"
SEGMENT_ID = 21264
EXPECTED_STATE_RUN_ID = 424
EXPECTED_CURRENT_OUTPUT = "É muito mais provável que os [characters|lE] se tornem [adventurers|lE]"
EXPECTED_CORRECTED_TEXT = "É mais provável que os [characters|lE] se tornem [adventurers|lE]"
CONFIRMATION_SOURCE = "local_learning"
CONFIRMATION_LABEL = "semantic_error"
REVIEWER = "user_human_review_culture_religion_parameter_modifier_batch6"


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
    return db.project_path("memory/backups") / f"{stamp()}_culture_religion_parameter_modifier_21264"


def short(value: Any, limit: int = 220) -> str:
    text = str(value or "").replace("\n", "\\n").replace("\t", "\\t")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def fetch_row(conn) -> dict[str, Any]:
    row = conn.execute(
        """
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
        WHERE s.id = ?
        """,
        (EXPECTED_STATE_RUN_ID, SEGMENT_ID),
    ).fetchone()
    if not row:
        raise SystemExit(f"missing segment_id={SEGMENT_ID}")
    return dict(row)


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


def token_counts(value: str) -> dict[str, int]:
    return dict(sorted(protected_tokens(value).items()))


def unified_diff(old_line: str, new_line: str, relative_path: str, line_number: int) -> str:
    before = [old_line]
    after = [new_line]
    return "\n".join(
        difflib.unified_diff(
            before,
            after,
            fromfile=f"a/{relative_path}:{line_number}",
            tofile=f"b/{relative_path}:{line_number}",
            lineterm="",
        )
    )


def evaluate(row: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    reasons: list[str] = []
    output_text = str(row.get("output_text") or "")
    corrected_text = str(row.get("corrected_text") or row.get("confirmed_text") or "")
    relative_path = str(row.get("relative_path") or "")
    path = output_root() / Path(relative_path)
    output_line_number = int(row.get("output_line_number") or 0)
    raw_line = str(row.get("output_raw_line") or "")
    new_line = ""

    if output_text != EXPECTED_CURRENT_OUTPUT:
        reasons.append("current_output_text_mismatch")
    if corrected_text != EXPECTED_CORRECTED_TEXT:
        reasons.append("corrected_text_mismatch")
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
    if int(row.get("needs_output_apply") or 0) != 0:
        reasons.append("state_needs_output_apply_not_zero")
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

    record = {
        "segment_id": SEGMENT_ID,
        "status": status,
        "block_reasons": reasons,
        "state_run_id": EXPECTED_STATE_RUN_ID,
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
    return record, reasons


def write_reports(record: dict[str, Any], *, mode: str, applied: bool, backup_path: str | None) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_culture_religion_parameter_modifier_21264_protected_apply_{mode}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "generated_at": now(),
        "mode": mode,
        "segment_id": SEGMENT_ID,
        "status": record["status"],
        "ready_count": int(record["status"] == "ready"),
        "applied_count": int(applied),
        "blocked_count": int(record["status"] == "blocked"),
        "already_applied_count": int(record["status"] == "already_applied"),
        "block_reasons": record["block_reasons"],
        "token_integrity_ok": record["token_integrity_ok"],
        "quality_blockers": record["quality_blockers"],
        "source_changed": False,
        "output_changed": bool(applied),
        "backup_path": backup_path,
        "rollback_path": backup_path,
        "post_validation_required": bool(applied),
        "run_segment_state_after_apply": bool(applied),
        "run_production_full_now": False,
        "run_reindex_now": False,
        "record": record,
    }
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Culture/religion parameter modifier 21264 protected apply",
        f"rule_version={RULE_VERSION}",
        f"mode={mode}",
        f"status={record['status']}",
        f"segment_id={SEGMENT_ID}",
        f"relative_path={record['relative_path']}",
        f"source_key={record['source_key']}",
        f"output_line_number={record['output_line_number']}",
        "",
        "Texts:",
        f"- current:   {record['current_output_text']}",
        f"- corrected: {record['corrected_text']}",
        "",
        "Validation:",
        f"- token_integrity_ok: {record['token_integrity_ok']}",
        f"- quality_blockers: {record['quality_blockers']}",
        f"- block_reasons: {record['block_reasons']}",
        "",
        "Diff preview:",
        record["unified_diff"],
        "",
        f"source_changed=false",
        f"output_changed={str(bool(applied)).lower()}",
        f"backup_path={backup_path or ''}",
        "production_full_recommended_now=false",
        "reindex_recommended_now=false",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def apply_record(record: dict[str, Any]) -> str:
    path = Path(record["output_path"])
    backup_dir = backup_root()
    backup_file = backup_dir / Path(record["relative_path"])
    backup_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup_file)

    lines = path.read_text(encoding="utf-8-sig").splitlines()
    index = int(record["output_line_number"]) - 1
    if lines[index] != record["old_line"]:
        raise RuntimeError("stale disk line before apply")
    lines[index] = record["new_line"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return str(backup_file)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    with db.connect(db.load_settings()) as conn:
        db.ensure_database(conn)
        row = fetch_row(conn)
    record, _reasons = evaluate(row)

    backup_path: str | None = None
    applied = False
    mode = "apply" if args.apply else "dry_run"
    if args.apply:
        if record["status"] != "ready":
            raise SystemExit(f"apply blocked: {record['block_reasons']}")
        backup_path = apply_record(record)
        applied = True

    txt_path, jsonl_path, summary_path = write_reports(record, mode=mode, applied=applied, backup_path=backup_path)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"mode={mode}")
    print(f"status={record['status']}")
    print(f"ready_count={int(record['status'] == 'ready')}")
    print(f"applied_count={int(applied)}")
    print(f"blocked_count={int(record['status'] == 'blocked')}")
    print(f"token_integrity_ok={record['token_integrity_ok']}")
    print(f"quality_blockers={json.dumps(record['quality_blockers'], ensure_ascii=False)}")
    print(f"output_changed={str(applied).lower()}")
    print(f"backup_path={backup_path or ''}")


if __name__ == "__main__":
    main()
