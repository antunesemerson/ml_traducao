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
from apply_safe_output_updates import replace_quoted_text
from apply_segment_state_updates import canonical_localization_text, structural_tokens


SOURCE = "release_promotion_partial_repair_protected_apply_v1"
CONFIRMATION_SOURCE = "codex_release_promotion_review"
CONFIRMATION_LABEL = "release_promotion_final8_corrected"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Protected apply for the final eight promotion repairs.")
    parser.add_argument("--preview-jsonl", type=Path, required=True)
    parser.add_argument("--expected-count", type=int, default=8)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db.get_database_path(db.load_settings()), timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def fetch_live(conn: sqlite3.Connection, ids: list[int]) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT s.id AS segment_id, s.relative_path, s.source_key,
               o.output_line_number, o.output_raw_line,
               o.portuguese_text AS output_text,
               c.confirmed_text, state.final_state, state.needs_output_apply
        FROM source_segments s
        JOIN output_segments o ON o.segment_id = s.id
        LEFT JOIN segment_confirmations c ON c.segment_id = s.id
        JOIN segment_state_items state ON state.segment_id = s.id
        WHERE state.run_id = (
            SELECT id FROM segment_state_runs
            WHERE finished_at IS NOT NULL AND total_segments > 1000
            ORDER BY finished_at DESC, id DESC LIMIT 1
        ) AND s.id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    return {int(row["segment_id"]): dict(row) for row in rows}


def build_records(preview: list[dict[str, Any]], expected: int) -> list[dict[str, Any]]:
    ready = [row for row in preview if row.get("readiness") == "ready_for_protected_apply_preview"]
    if len(ready) != expected:
        raise SystemExit(f"ready count guard failed: expected {expected}, got {len(ready)}")
    ids = sorted(int(row["segment_id"]) for row in ready)
    with connect() as conn:
        live_rows = fetch_live(conn, ids)
    root = db.project_path(db.load_settings()["output_spanish"])
    records: list[dict[str, Any]] = []
    for row in sorted(ready, key=lambda item: int(item["segment_id"])):
        segment_id = int(row["segment_id"])
        live = live_rows.get(segment_id) or {}
        current = str(live.get("output_text") or "")
        target = str(row.get("corrected_text") or "")
        raw = str(live.get("output_raw_line") or "")
        reasons: list[str] = []
        if not live:
            reasons.append("missing_live_row")
        if canonical_localization_text(current) != canonical_localization_text(str(row.get("current_text") or "")):
            reasons.append("preview_base_mismatch")
        if canonical_localization_text(current) != canonical_localization_text(str(live.get("confirmed_text") or "")):
            reasons.append("output_confirmation_mismatch")
        if structural_tokens(current) != structural_tokens(target):
            reasons.append("structural_tokens_changed")
        if int(live.get("needs_output_apply") or 0) != 0:
            reasons.append("unexpected_needs_output_apply")
        if not str(live.get("final_state") or "").startswith("reopen_"):
            reasons.append("state_not_reopen")
        relative_path = str(live.get("relative_path") or row.get("relative_path") or "")
        path = root / relative_path
        line_number = int(live.get("output_line_number") or 0)
        disk_line = ""
        if not path.exists() or line_number <= 0:
            reasons.append("missing_output_file_or_line")
        else:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
            if line_number > len(lines):
                reasons.append("output_line_out_of_range")
            else:
                disk_line = lines[line_number - 1]
                if disk_line != raw:
                    reasons.append("disk_line_differs_from_db")
        try:
            new_raw = replace_quoted_text(raw, target) if raw else ""
        except ValueError:
            new_raw = ""
            reasons.append("raw_line_not_replaceable")
        records.append({
            "source": SOURCE,
            "record_type": "release_promotion_partial_repair_apply_item",
            "segment_id": segment_id,
            "relative_path": relative_path,
            "source_key": live.get("source_key") or row.get("source_key"),
            "output_line_number": line_number,
            "old_output_text": current,
            "target_text": target,
            "old_raw_line": raw,
            "new_raw_line": new_raw,
            "target_hash": sha256(target),
            "status": "ready" if not reasons else "blocked",
            "block_reasons": reasons,
            "token_integrity_ok": structural_tokens(current) == structural_tokens(target),
        })
    return records


def restore_files(backup: Path, relative_paths: set[str]) -> None:
    root = db.project_path(db.load_settings()["output_spanish"])
    for relative_path in relative_paths:
        saved = backup / relative_path
        if saved.exists():
            shutil.copy2(saved, root / relative_path)


def apply_records(records: list[dict[str, Any]]) -> tuple[Path, int]:
    root = db.project_path(db.load_settings()["output_spanish"])
    backup = db.project_path("memory/backups") / f"release_promotion_final8_{stamp()}"
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_path[str(record["relative_path"])].append(record)
    for relative_path in by_path:
        source_path = root / relative_path
        backup_path = backup / relative_path
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, backup_path)
    timestamp = now_iso()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for relative_path, items in sorted(by_path.items()):
            path = root / relative_path
            lines = path.read_text(encoding="utf-8-sig").splitlines()
            for record in items:
                index = int(record["output_line_number"]) - 1
                if lines[index] != record["old_raw_line"]:
                    raise RuntimeError(f"disk line changed for segment {record['segment_id']}")
                lines[index] = str(record["new_raw_line"])
                conn.execute(
                    """
                    UPDATE output_segments SET portuguese_text=?, output_raw_line=?, portuguese_hash=?, last_indexed_at=?
                    WHERE segment_id=?
                    """,
                    (record["target_text"], record["new_raw_line"], record["target_hash"], timestamp, record["segment_id"]),
                )
                updated = conn.execute(
                    """
                    UPDATE segment_confirmations
                    SET confirmation_level='human_confirmed', confirmed_text=?, confirmation_source=?,
                        confirmation_label=?, locked=1, confidence_score=1.0, reviewer='codex',
                        updated_at=?, confirmed_at=COALESCE(confirmed_at, ?)
                    WHERE segment_id=?
                    """,
                    (record["target_text"], CONFIRMATION_SOURCE, CONFIRMATION_LABEL, timestamp, timestamp, record["segment_id"]),
                )
                if updated.rowcount == 0:
                    conn.execute(
                        """
                        INSERT INTO segment_confirmations (
                            segment_id, confirmation_level, confirmed_text, confirmation_source,
                            confirmation_label, locked, confidence_score, reviewer, confirmed_at, updated_at
                        ) VALUES (?, 'human_confirmed', ?, ?, ?, 1, 1.0, 'codex', ?, ?)
                        """,
                        (record["segment_id"], record["target_text"], CONFIRMATION_SOURCE, CONFIRMATION_LABEL, timestamp, timestamp),
                    )
            path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        conn.commit()
    except Exception:
        conn.rollback()
        restore_files(backup, set(by_path))
        raise
    finally:
        conn.close()
    return backup, len(by_path)


def post_validate(records: list[dict[str, Any]]) -> dict[str, int]:
    root = db.project_path(db.load_settings()["output_spanish"])
    counts = Counter()
    with connect() as conn:
        for record in records:
            row = conn.execute(
                """
                SELECT o.portuguese_text, o.output_raw_line, c.confirmed_text,
                       c.confirmation_source, c.confirmation_label, c.locked
                FROM output_segments o JOIN segment_confirmations c ON c.segment_id=o.segment_id
                WHERE o.segment_id=?
                """,
                (record["segment_id"],),
            ).fetchone()
            disk = (root / str(record["relative_path"])).read_text(encoding="utf-8-sig").splitlines()[int(record["output_line_number"]) - 1]
            counts["file_ok"] += int(disk == record["new_raw_line"])
            counts["output_db_ok"] += int(bool(row and row["portuguese_text"] == record["target_text"] and row["output_raw_line"] == record["new_raw_line"]))
            counts["confirmation_ok"] += int(bool(row and row["confirmed_text"] == record["target_text"] and row["confirmation_source"] == CONFIRMATION_SOURCE and row["confirmation_label"] == CONFIRMATION_LABEL and int(row["locked"] or 0) == 1))
            counts["token_integrity_ok"] += int(structural_tokens(record["old_output_text"]) == structural_tokens(record["target_text"]))
    return dict(counts)


def write_reports(summary: dict[str, Any], records: list[dict[str, Any]]) -> tuple[Path, Path]:
    reports = db.project_path(db.load_settings()["reports_dir"])
    reports.mkdir(parents=True, exist_ok=True)
    base = reports / f"{stamp()}_release_promotion_partial_repair_protected_apply_{summary['mode']}"
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return jsonl_path, summary_path


def main() -> int:
    args = parse_args()
    records = build_records(read_jsonl(args.preview_jsonl), args.expected_count)
    blocked = [record for record in records if record["status"] != "ready"]
    mode = "blocked" if blocked else ("apply" if args.apply else "dry_run")
    backup: Path | None = None
    files_touched = 0
    post: dict[str, int] = {}
    if args.apply and not blocked:
        backup, files_touched = apply_records(records)
        post = post_validate(records)
        if any(post.get(key, 0) != len(records) for key in ("file_ok", "output_db_ok", "confirmation_ok", "token_integrity_ok")):
            raise SystemExit(f"post-validation failed; inspect backup {backup}: {post}")
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": mode,
        "record_count": len(records),
        "ready_count": len(records) - len(blocked),
        "blocked_count": len(blocked),
        "block_reason_counts": dict(Counter(reason for record in blocked for reason in record["block_reasons"])),
        "applied_count": len(records) if args.apply and not blocked else 0,
        "files_touched_count": files_touched,
        "backup_root": str(backup) if backup else None,
        "post_validation": post,
        "confirmation_source": CONFIRMATION_SOURCE,
        "confirmation_label": CONFIRMATION_LABEL,
        "source_changed": False,
        "output_changed": bool(args.apply and not blocked),
        "segment_state_executed": False,
        "reindex_count": 0,
        "production_full_count": 0,
    }
    artifacts = write_reports(summary, records)
    print(json.dumps({"summary": summary, "artifacts": [str(path) for path in artifacts]}, ensure_ascii=False, indent=2))
    return 0 if not blocked else 2


if __name__ == "__main__":
    raise SystemExit(main())
