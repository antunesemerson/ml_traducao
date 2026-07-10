from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens, replace_quoted_text


SOURCE = "release_decision_post592_top15_protected_apply_v1"
INPUT_JSONL = Path("reports/20260704_005755_892574_release_decision_post592_top15_diff_preview.jsonl")
EXPECTED_READY = 13
CONFIRMATION_SOURCE = "codex_release_readiness_human_review"
CONFIRMATION_LABEL = "narrative_plain_light_batch_post592_top15_corrected"
EXCLUDED_IDS = {79601, 104908, 120831}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def output_root() -> Path:
    return db.project_path(db.load_settings()["output_spanish"])


def backup_root() -> Path:
    path = db.project_path("memory/backups") / f"release_decision_post592_top15_{stamp()}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Protected apply for post-592 top15 ready corrected_text rows.")
    parser.add_argument("--input-jsonl", type=Path, default=INPUT_JSONL)
    parser.add_argument("--expected-ready", type=int, default=EXPECTED_READY)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db.get_database_path(db.load_settings()), timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def fetch_live(conn: sqlite3.Connection, ids: list[int]) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT
          s.id AS segment_id,
          s.relative_path,
          s.source_key,
          s.source_line_number,
          s.english_text,
          s.spanish_text,
          o.output_line_number,
          o.portuguese_text AS output_text,
          o.output_raw_line,
          c.confirmed_text,
          c.confirmation_source,
          c.confirmation_label,
          c.locked
        FROM source_segments s
        JOIN output_segments o ON o.segment_id = s.id
        LEFT JOIN segment_confirmations c ON c.segment_id = s.id
        WHERE s.id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def ready_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        [
            row
            for row in rows
            if row.get("validation_status") == "ready_for_protected_apply"
            and int(row.get("segment_id") or 0) not in EXCLUDED_IDS
        ],
        key=lambda row: int(row["segment_id"]),
    )


def build_records(rows: list[dict[str, Any]], expected_ready: int) -> list[dict[str, Any]]:
    ready = ready_rows(rows)
    if len(ready) != expected_ready:
        raise SystemExit(f"ready count guard failed: expected {expected_ready}, got {len(ready)}")
    with connect() as conn:
        live = fetch_live(conn, [int(row["segment_id"]) for row in ready])
    root = output_root()
    records: list[dict[str, Any]] = []
    for row in ready:
        segment_id = int(row["segment_id"])
        live_row = live.get(segment_id)
        reasons: list[str] = []
        if live_row is None:
            reasons.append("missing_live_row")
            live_row = {}
        relative_path = str(live_row.get("relative_path") or row.get("relative_path"))
        output_path = root / relative_path
        output_line_number = int(live_row.get("output_line_number") or 0)
        old_output = str(live_row.get("output_text") or "")
        target = str(row.get("corrected_text") or "")
        current_raw_line = str(live_row.get("output_raw_line") or "")
        new_raw_line = replace_quoted_text(current_raw_line, target) if current_raw_line else ""
        disk_line = ""
        if segment_id in EXCLUDED_IDS:
            reasons.append("explicitly_excluded")
        if old_output != str(row.get("current_output_text") or ""):
            reasons.append("preview_base_mismatch_db_output")
        if protected_tokens(old_output) != protected_tokens(target):
            reasons.append("token_integrity_mismatch")
        if "\n" in target or "\r" in target:
            reasons.append("structure_integrity_mismatch")
        if not output_path.exists():
            reasons.append("missing_output_file")
        elif output_line_number <= 0:
            reasons.append("missing_output_line_number")
        else:
            lines = output_path.read_text(encoding="utf-8-sig").splitlines()
            idx = output_line_number - 1
            if idx < 0 or idx >= len(lines):
                reasons.append("line_out_of_range")
            else:
                disk_line = lines[idx]
                if disk_line != current_raw_line:
                    reasons.append("disk_line_mismatch_output_segments")
        records.append(
            {
                "source": SOURCE,
                "record_type": "protected_apply_item",
                "segment_id": segment_id,
                "relative_path": relative_path,
                "source_key": live_row.get("source_key") or row.get("source_key"),
                "source_line_number": live_row.get("source_line_number"),
                "english_text": live_row.get("english_text"),
                "spanish_text": live_row.get("spanish_text"),
                "output_line_number": output_line_number,
                "old_output_text": old_output,
                "target_text": target,
                "old_confirmed_text": live_row.get("confirmed_text"),
                "current_raw_line": current_raw_line,
                "new_raw_line": new_raw_line,
                "disk_line": disk_line,
                "token_integrity_ok": protected_tokens(old_output) == protected_tokens(target),
                "structure_integrity_ok": "\n" not in target and "\r" not in target,
                "status": "ready" if not reasons else "blocked",
                "block_reasons": reasons,
                "confirmation_source_after": CONFIRMATION_SOURCE,
                "confirmation_label_after": CONFIRMATION_LABEL,
                "target_hash": stable_hash(target),
            }
        )
    return records


def apply_records(records: list[dict[str, Any]], backup: Path) -> tuple[int, int, int]:
    root = output_root()
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_path[str(record["relative_path"])].append(record)
    timestamp = now_iso()
    files_touched = 0
    applied = 0
    confirmations = 0
    with connect() as conn:
        for relative_path, path_records in sorted(by_path.items()):
            output_path = root / relative_path
            backup_path = backup / relative_path
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output_path, backup_path)
            lines = output_path.read_text(encoding="utf-8-sig").splitlines()
            for record in path_records:
                idx = int(record["output_line_number"]) - 1
                if lines[idx] != record["current_raw_line"]:
                    raise SystemExit(f"disk line changed during apply for segment {record['segment_id']}")
                lines[idx] = str(record["new_raw_line"])
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
                        str(record["target_text"]),
                        str(record["new_raw_line"]),
                        str(record["target_hash"]),
                        timestamp,
                        int(record["segment_id"]),
                    ),
                )
                applied += 1
            output_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
            files_touched += 1
        for record in records:
            cur = conn.execute(
                """
                UPDATE segment_confirmations
                SET confirmation_level = 'human_confirmed',
                    confirmed_text = ?,
                    confirmation_source = ?,
                    confirmation_label = ?,
                    locked = 1,
                    confidence_score = 1.0,
                    reviewer = 'codex',
                    updated_at = ?,
                    confirmed_at = COALESCE(confirmed_at, ?)
                WHERE segment_id = ?
                """,
                (
                    str(record["target_text"]),
                    str(record["confirmation_source_after"]),
                    str(record["confirmation_label_after"]),
                    timestamp,
                    timestamp,
                    int(record["segment_id"]),
                ),
            )
            if cur.rowcount == 0:
                conn.execute(
                    """
                    INSERT INTO segment_confirmations (
                      segment_id, confirmation_level, confirmed_text,
                      confirmation_source, confirmation_label, locked,
                      confidence_score, candidate_id, feedback_id, reviewer,
                      confirmed_at, updated_at
                    )
                    VALUES (?, 'human_confirmed', ?, ?, ?, 1, 1.0, NULL, NULL, 'codex', ?, ?)
                    """,
                    (
                        int(record["segment_id"]),
                        str(record["target_text"]),
                        str(record["confirmation_source_after"]),
                        str(record["confirmation_label_after"]),
                        timestamp,
                        timestamp,
                    ),
                )
            confirmations += 1
        conn.commit()
    return applied, files_touched, confirmations


def post_validate(records: list[dict[str, Any]]) -> dict[str, Any]:
    root = output_root()
    file_ok = output_db_ok = confirmation_ok = token_ok = structure_ok = 0
    details: list[dict[str, Any]] = []
    with connect() as conn:
        for record in records:
            segment_id = int(record["segment_id"])
            line = (root / str(record["relative_path"])).read_text(encoding="utf-8-sig").splitlines()[
                int(record["output_line_number"]) - 1
            ]
            output = conn.execute(
                "SELECT portuguese_text, output_raw_line, portuguese_hash FROM output_segments WHERE segment_id=?",
                (segment_id,),
            ).fetchone()
            conf = conn.execute(
                "SELECT confirmed_text, confirmation_source, confirmation_label, locked FROM segment_confirmations WHERE segment_id=?",
                (segment_id,),
            ).fetchone()
            f_ok = line == str(record["new_raw_line"])
            o_ok = bool(
                output
                and output["portuguese_text"] == str(record["target_text"])
                and output["output_raw_line"] == str(record["new_raw_line"])
                and output["portuguese_hash"] == str(record["target_hash"])
            )
            c_ok = bool(
                conf
                and conf["confirmed_text"] == str(record["target_text"])
                and conf["confirmation_source"] == CONFIRMATION_SOURCE
                and conf["confirmation_label"] == CONFIRMATION_LABEL
                and int(conf["locked"] or 0) == 1
            )
            t_ok = protected_tokens(str(record["target_text"])) == protected_tokens(str(record["old_output_text"]))
            s_ok = "\n" not in str(record["target_text"]) and "\r" not in str(record["target_text"])
            file_ok += int(f_ok)
            output_db_ok += int(o_ok)
            confirmation_ok += int(c_ok)
            token_ok += int(t_ok)
            structure_ok += int(s_ok)
            details.append(
                {
                    "segment_id": segment_id,
                    "file_ok": f_ok,
                    "output_db_ok": o_ok,
                    "confirmation_ok": c_ok,
                    "token_integrity_ok": t_ok,
                    "structure_integrity_ok": s_ok,
                }
            )
    return {
        "file_ok": file_ok,
        "output_db_ok": output_db_ok,
        "confirmation_ok": confirmation_ok,
        "token_integrity_ok": token_ok,
        "structure_integrity_ok": structure_ok,
        "details": details,
    }


def write_reports(summary: dict[str, Any], records: list[dict[str, Any]]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_decision_post592_top15_protected_apply_{summary['mode']}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                "Release decision post592 top15 protected apply",
                f"mode={summary['mode']}",
                f"ready_count={summary['ready_count']}",
                f"blocked_count={summary['blocked_count']}",
                f"applied_count={summary['applied_count']}",
                f"files_touched_count={summary['files_touched_count']}",
                f"confirmation_updated_count={summary['confirmation_updated_count']}",
                f"backup_root={summary['backup_root']}",
                f"post_validation={json.dumps(summary['post_validation'], ensure_ascii=False, sort_keys=True)}",
                f"rollback_path={summary['rollback_path']}",
                "candidate_generation_count=0",
                f"apply_count={summary['applied_count']}",
                "lifecycle_count=0",
                "segment_state_count=0",
                "reindex_count=0",
                "production_full_count=0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records = build_records(read_jsonl(args.input_jsonl), args.expected_ready)
    blocked = [record for record in records if record["status"] != "ready"]
    mode = "blocked" if blocked else ("apply" if args.apply else "dry_run")
    backup = None
    applied = files_touched = confirmation_updated = 0
    if args.apply and not blocked:
        backup = backup_root()
        applied, files_touched, confirmation_updated = apply_records(records, backup)
    post = (
        post_validate(records)
        if args.apply and not blocked
        else {"file_ok": 0, "output_db_ok": 0, "confirmation_ok": 0, "token_integrity_ok": 0, "structure_integrity_ok": 0, "details": []}
    )
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": mode,
        "input_jsonl": str(args.input_jsonl),
        "ready_count": len([r for r in records if r["status"] == "ready"]),
        "blocked_count": len(blocked),
        "block_reason_counts": dict(Counter(reason for record in blocked for reason in record["block_reasons"]).most_common()),
        "applied_count": applied,
        "files_touched_count": files_touched,
        "confirmation_updated_count": confirmation_updated,
        "backup_root": str(backup) if backup else None,
        "rollback_path": str(backup) if backup else "not_created_dry_run_or_blocked",
        "post_validation": post,
        "hold_concept_ids": sorted(EXCLUDED_IDS & {79601, 104908}),
        "candidate_generation_count": 0,
        "apply_count": applied,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "materializer_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": bool(applied),
    }
    txt, jsonl, summary_path = write_reports(summary, records)
    print(f"txt={txt}")
    print(f"jsonl={jsonl}")
    print(f"summary={summary_path}")
    print(f"mode={summary['mode']}")
    print(f"ready_count={summary['ready_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"applied_count={summary['applied_count']}")
    print(f"files_touched_count={summary['files_touched_count']}")
    print(f"confirmation_updated_count={summary['confirmation_updated_count']}")
    print(f"post_validation={json.dumps(summary['post_validation'], ensure_ascii=False, sort_keys=True)}")
    print("candidate_generation_count=0")
    print(f"apply_count={summary['apply_count']}")
    print("learning_ingest_count=0")
    print("issue_closure_count=0")
    print("lifecycle_count=0")
    print("materializer_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
