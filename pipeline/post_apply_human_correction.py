from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "post_apply_human_correction_v1"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def file_encoding(path: Path) -> str:
    with path.open("rb") as handle:
        return "utf-8-sig" if handle.read(3) == b"\xef\xbb\xbf" else "utf-8"


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


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db.get_database_path(db.load_settings()), timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_context(conn: sqlite3.Connection, segment_id: int, state_run_id: int):
    return conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            st.state_group,
            st.needs_human,
            st.active_action,
            st.candidate_action,
            st.policy_action,
            st.reasons_json,
            o.relative_path AS output_relative_path,
            o.output_line_number,
            o.portuguese_text,
            o.output_raw_line
        FROM source_segments s
        JOIN segment_state_items st ON st.segment_id = s.id AND st.run_id = ?
        JOIN output_segments o ON o.segment_id = s.id
        WHERE s.id = ?
        """,
        (state_run_id, segment_id),
    ).fetchone()


def quoted_value(raw_line: str) -> str:
    first = raw_line.find('"')
    last = raw_line.rfind('"')
    if first < 0 or last <= first:
        raise SystemExit("raw line does not contain quoted value")
    return raw_line[first + 1 : last]


def replace_raw_value(raw_line: str, corrected_text: str) -> str:
    first = raw_line.find('"')
    last = raw_line.rfind('"')
    if first < 0 or last <= first:
        raise SystemExit("raw line does not contain quoted value")
    return raw_line[: first + 1] + corrected_text + raw_line[last:]


def write_outputs(record: dict[str, Any], summary: dict[str, Any], base_name: str) -> tuple[Path, Path, Path]:
    txt_path = reports_dir() / f"{base_name}.txt"
    jsonl_path = reports_dir() / f"{base_name}.jsonl"
    summary_path = reports_dir() / f"{base_name}_summary.json"
    jsonl_path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "post apply human correction",
        f"segment_id={record['segment_id']}",
        f"mode={summary['mode']}",
        f"applied={str(summary['applied']).lower()}",
        f"post_validation_ok={str(summary['post_validation_ok']).lower()}",
        f"snapshot_path={record['snapshot_path']}",
        f"wrong_learning_candidate_id={record['wrong_learning_candidate_id']}",
        f"new_learning_candidate_id={record['new_learning_candidate_id']}",
        f"needs_human_marker_seen={str(record['needs_human_marker_seen']).lower()}",
        f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment-id", type=int, required=True)
    parser.add_argument("--state-run-id", type=int, required=True)
    parser.add_argument("--expected-current", required=True)
    parser.add_argument("--corrected-text", required=True)
    parser.add_argument("--auto-apply", action="store_true")
    args = parser.parse_args()

    base_name = f"{stamp()}_post_apply_human_correction_{'apply' if args.auto_apply else 'dry_run'}"
    with connect() as conn:
        context = fetch_context(conn, args.segment_id, args.state_run_id)
        if context is None:
            raise SystemExit("missing segment context")
        target = db.PROJECT_ROOT / "output" / "spanish" / str(context["output_relative_path"])
        lines = read_lines(target)
        index = int(context["output_line_number"]) - 1
        actual_raw = line_without_newline(lines[index])
        actual_text = quoted_value(actual_raw)
        if actual_text != args.expected_current:
            raise SystemExit(f"current text guard failed: {actual_text!r}")
        corrected_raw = replace_raw_value(actual_raw, args.corrected_text)
        snapshot_path = ""
        applied = False
        if args.auto_apply:
            snapshot_root = reports_dir() / f"{base_name}_snapshots"
            snapshot_path = str(snapshot_root / target.relative_to(db.PROJECT_ROOT))
            Path(snapshot_path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, snapshot_path)
            lines[index] = corrected_raw + line_ending(lines[index])
            write_lines(target, lines)
            applied = True

        post_validation_ok = True
        if args.auto_apply:
            updated = line_without_newline(read_lines(target)[index])
            post_validation_ok = updated == corrected_raw
            if not post_validation_ok:
                raise SystemExit("post validation failed")

        timestamp = now()
        wrong = conn.execute(
            """
            SELECT id
            FROM local_learning_candidates
            WHERE segment_id = ?
              AND suggested_text = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (args.segment_id, args.expected_current),
        ).fetchone()
        wrong_id = int(wrong["id"]) if wrong else None
        new_candidate_id = None
        if args.auto_apply:
            if wrong_id is not None:
                conn.execute(
                    """
                    UPDATE local_learning_candidates
                    SET
                        human_label = 'semantic_error',
                        corrected_text = ?,
                        reason = 'post-apply human correction: comparative quantity should be singular muito mais',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (args.corrected_text, timestamp, wrong_id),
                )
            cursor = conn.execute(
                """
                INSERT INTO local_learning_runs (
                    mode,
                    limit_count,
                    auto_confidence_threshold,
                    candidate_count,
                    high_confidence_count,
                    pending_human_count,
                    status,
                    notes,
                    started_at,
                    finished_at,
                    updated_at
                )
                VALUES (?, 1, 1.0, 1, 1, 0, 'completed', ?, ?, ?, ?)
                """,
                (
                    "post-apply-human-correction",
                    RULE_VERSION,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            run_id = int(cursor.lastrowid)
            cursor = conn.execute(
                """
                INSERT INTO local_learning_candidates (
                    run_id,
                    feedback_id,
                    suggestion_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    english_text,
                    spanish_text,
                    old_text,
                    current_output_text,
                    suggested_text,
                    suggested_hash,
                    source_language,
                    origin,
                    match_type,
                    match_score,
                    token_status,
                    suggestion_status,
                    local_confidence_score,
                    local_status,
                    human_label,
                    corrected_text,
                    reason,
                    reviewer,
                    reviewed_at,
                    reasons_json,
                    created_at,
                    updated_at,
                    queue_source,
                    focus_group,
                    learned_at,
                    confirmation_synced_at
                )
                VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'human_correction', 'human_correction_after_apply',
                        'post_apply_correction', 1.0, 'ok', 'safe', 1.0, 'high_confidence', 'correct',
                        NULL, ?, 'user', ?, ?, ?, ?, 'post-apply-human-correction', 'all', ?, ?)
                """,
                (
                    run_id,
                    args.segment_id,
                    context["relative_path"],
                    context["source_key"],
                    context["source_line_number"],
                    context["english_text"],
                    context["spanish_text"],
                    context["old_text"],
                    args.expected_current,
                    args.corrected_text,
                    sha256_text(args.corrected_text),
                    "post-apply human correction: muito mais",
                    timestamp,
                    json.dumps(
                        [
                            "human_correction",
                            "previous_post_apply_candidate_marked_semantic_error",
                            "token_integrity_ok",
                            "structure_integrity_ok",
                        ],
                        ensure_ascii=True,
                    ),
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            new_candidate_id = int(cursor.lastrowid)
            existing_conf = conn.execute(
                "SELECT id FROM segment_confirmations WHERE segment_id = ?",
                (args.segment_id,),
            ).fetchone()
            if existing_conf:
                conn.execute(
                    """
                    UPDATE segment_confirmations
                    SET
                        confirmation_level = 'human_confirmed',
                        confirmed_text = ?,
                        confirmation_source = 'post_apply_human_correction',
                        confirmation_label = 'correct',
                        locked = 1,
                        confidence_score = 1.0,
                        candidate_id = ?,
                        reviewer = 'user',
                        confirmed_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (args.corrected_text, new_candidate_id, timestamp, timestamp, existing_conf["id"]),
                )
            conn.commit()

        record = {
            "segment_id": args.segment_id,
            "target_file": str(target),
            "target_line_number": int(context["output_line_number"]),
            "previous_text": args.expected_current,
            "corrected_text": args.corrected_text,
            "previous_raw_line": actual_raw,
            "corrected_raw_line": corrected_raw,
            "snapshot_path": snapshot_path,
            "wrong_learning_candidate_id": wrong_id,
            "new_learning_candidate_id": new_candidate_id,
            "needs_human_marker_seen": bool(context["needs_human"]),
            "state_action_markers": {
                "state_group": context["state_group"],
                "active_action": context["active_action"],
                "candidate_action": context["candidate_action"],
                "policy_action": context["policy_action"],
                "reasons_json": context["reasons_json"],
            },
        }
        summary = {
            "schema_version": 1,
            "source": RULE_VERSION,
            "mode": "apply" if args.auto_apply else "dry_run",
            "applied": applied,
            "post_validation_ok": post_validation_ok,
            "production_full_recommended_now": False,
            "lifecycle_reindex_recommended_now": False,
            "recommended_next_action": "rerun_closeout_status",
        }
        txt_path, jsonl_path, summary_path = write_outputs(record, summary, base_name)
        print(f"txt={txt_path}")
        print(f"jsonl={jsonl_path}")
        print(f"summary={summary_path}")
        print(f"applied={summary['applied']}")
        print(f"post_validation_ok={summary['post_validation_ok']}")
        print(f"needs_human_marker_seen={record['needs_human_marker_seen']}")
        print(f"wrong_learning_candidate_id={wrong_id}")
        print(f"new_learning_candidate_id={new_candidate_id}")


if __name__ == "__main__":
    main()
