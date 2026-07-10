from __future__ import annotations

import difflib
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator
from apply_safe_output_updates import protected_tokens, replace_quoted_text


RULE_VERSION = "domain_policy_vote_candidate_medium_dynamic_light_protected_apply_dry_run_v1"
DECISIONS_PATH = Path("reports/20260629_123549_030368_domain_policy_vote_candidate_medium_dynamic_light_decisions_consolidated.jsonl")
EXPECTED_STATE_RUN_ID = 495


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


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


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


def fetch_state(conn: sqlite3.Connection, segment_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            s.id AS segment_id,
            s.relative_path AS source_relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            o.relative_path AS output_relative_path,
            o.output_line_number,
            o.portuguese_text AS output_text,
            o.output_raw_line,
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
        LEFT JOIN segment_state_items state
          ON state.segment_id = s.id
         AND state.run_id = ?
        WHERE s.id IN ({placeholders})
        ORDER BY s.id
        """,
        (EXPECTED_STATE_RUN_ID, *segment_ids),
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def evaluate(decision: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
    reasons: list[str] = []
    segment_id = int(decision["segment_id"])
    current_from_decision = str(decision.get("current_output_text") or "")
    corrected_text = str(decision.get("corrected_text") or "")

    if decision.get("human_decision") != "approve_correction":
        reasons.append("not_approve_correction")
    if not corrected_text:
        reasons.append("missing_corrected_text")
    if state is None:
        reasons.append("missing_database_state")
        state = {}

    db_output = str(state.get("output_text") or "")
    relative_path = str(state.get("output_relative_path") or state.get("source_relative_path") or decision.get("relative_path") or "")
    output_line_number = int(state.get("output_line_number") or 0)
    raw_line = str(state.get("output_raw_line") or "")
    path = output_root() / Path(relative_path)
    new_line = ""

    if state.get("state_run_id") != EXPECTED_STATE_RUN_ID:
        reasons.append("missing_expected_segment_state")
    if state.get("state_group") != "pending" or state.get("final_state") != "reopen_auto_confirmed_autofix":
        reasons.append("unexpected_segment_state")
    if int(state.get("needs_output_apply") or 0) != 0:
        reasons.append("needs_output_apply_not_zero_pre_apply")
    if int(state.get("confirmed_matches_output") or 0) != 1:
        reasons.append("confirmed_matches_output_not_one_pre_apply")
    if db_output != current_from_decision:
        reasons.append("decision_current_text_mismatch_database_output")
    if db_output == corrected_text and corrected_text:
        reasons.append("already_applied_in_database")
    if not path.exists():
        reasons.append("missing_output_file")
    if output_line_number <= 0:
        reasons.append("missing_output_line_number")

    output_tokens = protected_tokens(db_output)
    corrected_tokens = protected_tokens(corrected_text)
    token_integrity_ok = output_tokens == corrected_tokens
    if not token_integrity_ok:
        reasons.append("protected_token_signature_mismatch")
    structure_integrity_ok = db_output.count("\n") == corrected_text.count("\n")
    if not structure_integrity_ok:
        reasons.append("line_structure_mismatch")

    quality = quality_blockers(corrected_text) if corrected_text else []
    if quality:
        reasons.append("quality_issue:" + ",".join(quality))

    disk_line = ""
    if path.exists() and output_line_number > 0:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        index = output_line_number - 1
        if index < 0 or index >= len(lines):
            reasons.append("line_out_of_range")
        else:
            disk_line = lines[index]
            if raw_line and disk_line != raw_line:
                reasons.append("disk_line_mismatch_output_raw_line")
            if db_output and db_output not in disk_line:
                reasons.append("disk_line_does_not_contain_current_output")
            try:
                new_line = replace_quoted_text(disk_line, corrected_text)
            except ValueError:
                reasons.append("line_without_quoted_value")

    false_safe_risk = not token_integrity_ok or not structure_integrity_ok or bool(quality)
    status = "ready" if not reasons else "blocked"
    return {
        "segment_id": segment_id,
        "status": status,
        "block_reasons": reasons,
        "false_safe_risk": false_safe_risk,
        "token_integrity_ok": token_integrity_ok,
        "structure_integrity_ok": structure_integrity_ok,
        "quality_blockers": quality,
        "relative_path": relative_path,
        "source_key": state.get("source_key") or decision.get("source_key"),
        "source_line_number": state.get("source_line_number") or decision.get("source_line_number"),
        "output_line_number": output_line_number,
        "output_path": str(path),
        "english_text": state.get("english_text") or decision.get("english_text"),
        "spanish_text": state.get("spanish_text") or decision.get("spanish_text"),
        "current_output_text": db_output,
        "corrected_text": corrected_text,
        "current_output_hash": sha256_text(db_output),
        "corrected_text_hash": sha256_text(corrected_text),
        "output_tokens": token_counts(db_output),
        "corrected_tokens": token_counts(corrected_text),
        "old_line": raw_line,
        "disk_line": disk_line,
        "new_line": new_line,
        "unified_diff": unified_diff(raw_line, new_line, relative_path, output_line_number) if new_line else "",
        "human_decision": decision.get("human_decision"),
        "decision_source": decision.get("decision_source"),
        "review_notes": decision.get("review_notes"),
        "state": {
            "run_id": state.get("state_run_id"),
            "final_state": state.get("final_state"),
            "state_group": state.get("state_group"),
            "review_state": state.get("review_state"),
            "apply_state": state.get("apply_state"),
            "needs_output_apply": int(state.get("needs_output_apply") or 0),
            "confirmed_matches_output": int(state.get("confirmed_matches_output") or 0),
            "needs_human": int(state.get("needs_human") or 0),
        },
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def top_counter(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common()]


def write_txt(path: Path, summary: dict[str, Any], records: list[dict[str, Any]]) -> None:
    lines = [
        "domain_policy_vote_candidate medium_dynamic_light protected apply dry-run",
        "",
        f"rule_version: {RULE_VERSION}",
        f"decision_source: {summary['decision_source']}",
        f"expected_state_run_id: {summary['expected_state_run_id']}",
        f"correction_decision_count: {summary['correction_decision_count']}",
        f"ready_count: {summary['ready_count']}",
        f"blocked_count: {summary['blocked_count']}",
        f"false_safe_risk_count: {summary['false_safe_risk_count']}",
        f"token_integrity_ok_count: {summary['token_integrity_ok_count']}",
        f"structure_integrity_ok_count: {summary['structure_integrity_ok_count']}",
        "",
        "gates:",
        "- apply: not_run",
        "- lifecycle: not_run",
        "- segment_state: not_run",
        "- reindex: not_run",
        "- full_production: not_run",
        "",
        "block_reason_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["block_reason_counts"])
    lines.extend(["", "diff_previews:"])
    for record in records:
        lines.extend(
            [
                "",
                f"## segment_id {record['segment_id']} | status={record['status']} | key={record['source_key']}",
                f"- block_reasons: {record['block_reasons']}",
                f"- output_path: {record['output_path']}",
                f"- output_line_number: {record['output_line_number']}",
                "```diff",
                record["unified_diff"],
                "```",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    decisions = [row for row in read_jsonl(DECISIONS_PATH) if row.get("human_decision") == "approve_correction"]
    segment_ids = [int(row["segment_id"]) for row in decisions]
    with connect_readonly() as conn:
        state_by_id = fetch_state(conn, segment_ids)
    records = [evaluate(decision, state_by_id.get(int(decision["segment_id"]))) for decision in decisions]
    ready_count = sum(1 for record in records if record["status"] == "ready")
    blocked_count = sum(1 for record in records if record["status"] == "blocked")
    false_safe_risk_count = sum(1 for record in records if record["false_safe_risk"])
    token_integrity_ok_count = sum(1 for record in records if record["token_integrity_ok"])
    structure_integrity_ok_count = sum(1 for record in records if record["structure_integrity_ok"])
    block_counter = Counter(reason for record in records for reason in record["block_reasons"])
    summary = {
        "created_at": now(),
        "mode": "dry_run_diff_preview_only",
        "rule_version": RULE_VERSION,
        "decision_source": str(DECISIONS_PATH),
        "expected_state_run_id": EXPECTED_STATE_RUN_ID,
        "correction_decision_count": len(decisions),
        "ready_count": ready_count,
        "blocked_count": blocked_count,
        "candidate_count": ready_count,
        "false_safe_risk_count": false_safe_risk_count,
        "token_integrity_ok_count": token_integrity_ok_count,
        "structure_integrity_ok_count": structure_integrity_ok_count,
        "requires_apply_later_count": ready_count,
        "requires_lifecycle_later_count": 0,
        "source_changed": False,
        "output_changed": False,
        "database_output_segments_changed": False,
        "snapshot_created": False,
        "rollback_path": "not_created_dry_run_only",
        "post_validation_required": False,
        "run_production_full_now": False,
        "run_reindex_now": False,
        "run_segment_state_now": False,
        "block_reason_counts": top_counter(block_counter),
        "gates": {
            "apply": "not_run",
            "lifecycle": "not_run",
            "segment_state": "not_run",
            "reindex": "not_run",
            "full_production": "not_run",
        },
        "next_recommendation": "if ready_count equals correction_decision_count and false_safe_risk_count is 0, request approval for protected apply with snapshot and post-validation",
        "output_files": {},
    }

    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_medium_dynamic_light_protected_apply_dry_run"
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    txt_path = base.with_suffix(".txt")
    write_jsonl(jsonl_path, records)
    summary["output_files"] = {
        "jsonl": str(jsonl_path),
        "summary_json": str(summary_path),
        "txt": str(txt_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary, records)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
