from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens, replace_quoted_text


RULE_VERSION = "v3_deterministic_correction_apply_queue_v1"
CONFIRMATION_SOURCE = "codex_v3_deterministic_review"
CONFIRMATION_LABEL = "v3_deterministic_correction_batch1"
REVIEWER = "codex"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def latest_decisions() -> Path:
    matches = sorted(reports_dir().glob("*_v3_deterministic_correction_decision_preview_readonly_decisions.jsonl"))
    if not matches:
        raise RuntimeError("No approved V3 deterministic decision artifact was found.")
    return matches[-1]


def report_paths(mode: str) -> dict[str, Path]:
    base = reports_dir() / f"{stamp()}_v3_deterministic_correction_apply_queue_{mode}"
    return {
        "markdown": base.with_suffix(".md"),
        "jsonl": base.with_suffix(".jsonl"),
        "summary": base.with_name(base.name + "_summary.json"),
    }


def load_decisions(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != 11 or len({int(row["segment_id"]) for row in rows}) != 11:
        raise RuntimeError("Expected 11 unique approved decisions.")
    for row in rows:
        if row.get("human_decision") != "corrected_text" or not row.get("corrected_text"):
            raise RuntimeError(f"Decision guard failed for segment {row.get('segment_id')}.")
        if row.get("decision_source") != "explicit_user_approval_20260711":
            raise RuntimeError(f"Decision source guard failed for segment {row.get('segment_id')}.")
    return rows


def latest_state_run_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT id FROM segment_state_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise RuntimeError("No completed segment-state run was found.")
    return int(row[0])


def fetch_live(conn: sqlite3.Connection, segment_ids: list[int], state_run_id: int) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            source.id AS segment_id,
            source.relative_path,
            source.source_key,
            source.spanish_text,
            output.portuguese_text AS current_output_text,
            output.output_line_number,
            confirmation.id AS confirmation_id,
            confirmation.confirmation_level,
            confirmation.confirmed_text,
            confirmation.confirmation_source,
            confirmation.confirmation_label,
            confirmation.locked,
            confirmation.confidence_score,
            confirmation.candidate_id,
            confirmation.feedback_id,
            confirmation.reviewer,
            confirmation.confirmed_at,
            confirmation.updated_at,
            state.final_state,
            state.is_closed,
            state.needs_output_apply,
            state.confirmed_matches_output
        FROM source_segments source
        JOIN output_segments output ON output.segment_id = source.id
        JOIN segment_state_items state
          ON state.segment_id = source.id
         AND state.run_id = ?
        LEFT JOIN segment_confirmations confirmation ON confirmation.segment_id = source.id
        WHERE source.id IN ({placeholders})
        """,
        (state_run_id, *segment_ids),
    ).fetchall()
    if len(rows) != len(segment_ids):
        raise RuntimeError(f"Expected {len(segment_ids)} live rows, got {len(rows)}.")
    return {int(row["segment_id"]): dict(row) for row in rows}


def evaluate(decision: dict[str, Any], live: dict[str, Any], output_root: Path) -> dict[str, Any]:
    corrected = str(decision["corrected_text"])
    current = str(live.get("current_output_text") or "")
    spanish = str(live.get("spanish_text") or "")
    line_number = int(live.get("output_line_number") or 0)
    output_path = output_root / Path(str(live["relative_path"]))
    disk_ok = False
    if output_path.exists() and line_number > 0:
        lines = output_path.read_text(encoding="utf-8-sig").splitlines()
        if line_number <= len(lines):
            try:
                disk_ok = replace_quoted_text(lines[line_number - 1], current) == lines[line_number - 1]
            except ValueError:
                disk_ok = False
    existing_human_lock = (
        str(live.get("confirmation_level") or "") == "human_confirmed"
        and int(live.get("locked") or 0) == 1
    )
    guards = {
        "decision_current_matches_live_output": str(decision.get("output_text") or "") == current,
        "canonical_change_present": corrected != current,
        "token_integrity_current": protected_tokens(current) == protected_tokens(corrected),
        "token_integrity_spanish": protected_tokens(spanish) == protected_tokens(corrected),
        "output_file_matches_db": disk_ok,
        "segment_is_closed": int(live.get("is_closed") or 0) == 1,
        "needs_output_apply_zero_before_queue": int(live.get("needs_output_apply") or 0) == 0,
        "existing_human_lock_absent": not existing_human_lock,
    }
    return {
        "segment_id": int(decision["segment_id"]),
        "relative_path": live["relative_path"],
        "source_key": live["source_key"],
        "output_line_number": line_number,
        "current_output_text": current,
        "approved_corrected_text": corrected,
        "before_confirmation": {
            key: live.get(key)
            for key in (
                "confirmation_id",
                "confirmation_level",
                "confirmed_text",
                "confirmation_source",
                "confirmation_label",
                "locked",
                "confidence_score",
                "candidate_id",
                "feedback_id",
                "reviewer",
                "confirmed_at",
                "updated_at",
            )
        },
        "before_state": {
            "final_state": live.get("final_state"),
            "needs_output_apply": int(live.get("needs_output_apply") or 0),
            "confirmed_matches_output": int(live.get("confirmed_matches_output") or 0),
        },
        "guards": guards,
        "ready": all(guards.values()),
        "block_reasons": [name for name, passed in guards.items() if not passed],
        "confirmation_source_after": CONFIRMATION_SOURCE,
        "confirmation_label_after": CONFIRMATION_LABEL,
        "confirmation_level_after": "human_confirmed",
        "locked_after": 1,
    }


def queue_confirmation(conn: sqlite3.Connection, record: dict[str, Any], now: str) -> None:
    segment_id = int(record["segment_id"])
    corrected = str(record["approved_corrected_text"])
    existing_id = record["before_confirmation"].get("confirmation_id")
    if existing_id is None:
        conn.execute(
            """
            INSERT INTO segment_confirmations (
                segment_id, confirmation_level, confirmed_text, confirmation_source,
                confirmation_label, locked, confidence_score, reviewer, confirmed_at, updated_at
            ) VALUES (?, 'human_confirmed', ?, ?, ?, 1, 1.0, ?, ?, ?)
            """,
            (segment_id, corrected, CONFIRMATION_SOURCE, CONFIRMATION_LABEL, REVIEWER, now, now),
        )
    else:
        conn.execute(
            """
            UPDATE segment_confirmations
            SET confirmation_level = 'human_confirmed',
                confirmed_text = ?,
                confirmation_source = ?,
                confirmation_label = ?,
                locked = 1,
                confidence_score = 1.0,
                reviewer = ?,
                confirmed_at = ?,
                updated_at = ?
            WHERE segment_id = ?
            """,
            (corrected, CONFIRMATION_SOURCE, CONFIRMATION_LABEL, REVIEWER, now, now, segment_id),
        )


def write_reports(paths: dict[str, Path], decisions_path: Path, state_run_id: int, records: list[dict[str, Any]], apply: bool) -> dict[str, Any]:
    ready = [row for row in records if row["ready"]]
    blocked = [row for row in records if not row["ready"]]
    block_counts = Counter(reason for row in blocked for reason in row["block_reasons"])
    summary = {
        "schema_version": 1,
        "rule_version": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "apply" if apply else "dry_run",
        "source_decisions": str(decisions_path),
        "source_segment_state_run_id": state_run_id,
        "record_count": len(records),
        "ready_count": len(ready),
        "blocked_count": len(blocked),
        "block_reason_counts": dict(block_counts),
        "queued_confirmation_count": len(ready) if apply else 0,
        "queued_segment_ids": [int(row["segment_id"]) for row in ready] if apply else [],
        "output_written": 0,
        "source_changed": False,
        "output_changed": False,
        "next_step": "run_segment_state_to_materialize_pending_apply_confirmed" if apply else "rerun_with_apply_if_all_ready",
    }
    paths["summary"].write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with paths["jsonl"].open("w", encoding="utf-8", newline="\n") as handle:
        for row in records:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    lines = [
        "# V3 deterministic correction apply queue",
        "",
        f"- Mode: `{'apply' if apply else 'dry_run'}`",
        f"- Ready: `{len(ready)}`",
        f"- Blocked: `{len(blocked)}`",
        f"- Confirmations queued: `{len(ready) if apply else 0}`",
        "- Output written: `0`",
        "",
    ]
    for row in records:
        lines.extend(
            [
                f"## Segment {row['segment_id']} - {'ready' if row['ready'] else 'blocked'}",
                "",
                f"- Path/key: `{row['relative_path']} :: {row['source_key']}`",
                f"- Guards: `{json.dumps(row['guards'], ensure_ascii=False, sort_keys=True)}`",
                "",
            ]
        )
    paths["markdown"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Queue approved V3 deterministic corrections without writing output.")
    parser.add_argument("--decisions", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    decisions_path = args.decisions.resolve() if args.decisions else latest_decisions()
    decisions = load_decisions(decisions_path)
    settings = db.load_settings()
    database_path = db.get_database_path(settings)
    output_root = db.project_path(settings["output_spanish"])
    with sqlite3.connect(database_path, timeout=120) as conn:
        conn.row_factory = sqlite3.Row
        state_run_id = latest_state_run_id(conn)
        live = fetch_live(conn, [int(row["segment_id"]) for row in decisions], state_run_id)
        records = [evaluate(row, live[int(row["segment_id"])], output_root) for row in decisions]
        ready = [row for row in records if row["ready"]]
        if len(ready) != len(records):
            conn.rollback()
        elif args.apply:
            now = utc_now()
            for record in ready:
                queue_confirmation(conn, record, now)
            conn.commit()
        else:
            conn.rollback()
    paths = report_paths("apply" if args.apply else "dry_run")
    summary = write_reports(paths, decisions_path, state_run_id, records, args.apply)
    print("[v3-apply-queue] Completed")
    print(f"[v3-apply-queue] Mode: {summary['mode']}")
    print(f"[v3-apply-queue] Ready: {summary['ready_count']}")
    print(f"[v3-apply-queue] Blocked: {summary['blocked_count']}")
    print(f"[v3-apply-queue] Queued: {summary['queued_confirmation_count']}")
    print(f"[v3-apply-queue] Summary: {paths['summary']}")
    return summary


if __name__ == "__main__":
    main()
