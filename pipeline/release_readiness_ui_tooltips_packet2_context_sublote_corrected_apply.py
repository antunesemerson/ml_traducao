from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import db
from apply_safe_output_updates import protected_tokens
from apply_segment_state_updates import canonical_localization_text
from release_readiness_ui_tooltips_packet2_context_sublote_decisions import (
    DECISIONS,
    EXPECTED_CORRECTED_TEXT,
)


SOURCE = "release_readiness_ui_tooltips_packet2_context_sublote_corrected_apply_v1"
REVIEW_PACKET_JSONL = Path("reports/20260702_211655_987586_release_readiness_ui_tooltips_packet2_context_sublote_review_packet.jsonl")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Protected apply for packet2 context sublote corrected_text rows.")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def read_review_rows() -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    with db.project_path(REVIEW_PACKET_JSONL).open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                rows[int(row["segment_id"])] = row
    return rows


def token_integrity_ok(old: str, new: str) -> bool:
    return Counter(protected_tokens(old)) == Counter(protected_tokens(new))


def structure_integrity_ok(old: str, new: str) -> bool:
    markers = ["#weak", "#WEAK", "#italic", "#!", "#D", "#P", "#N", "@warning_icon!", "@alert_icon!"]
    for marker in markers:
        if (marker in old) != (marker in new):
            return False
    return old.count("[") == new.count("[") and old.count("]") == new.count("]") and old.count("$") == new.count("$")


def yaml_quote_text(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fetch_context(conn: sqlite3.Connection, segment_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
          s.id AS segment_id,
          s.relative_path,
          s.source_key,
          s.source_line_number,
          o.portuguese_text AS output_text,
          o.output_raw_line AS output_raw_line,
          c.confirmed_text,
          c.confirmation_level,
          c.locked
        FROM source_segments s
        JOIN output_segments o ON o.segment_id = s.id
        LEFT JOIN segment_confirmations c ON c.segment_id = s.id
        WHERE s.id = ?
        """,
        (segment_id,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"missing segment {segment_id}")
    return dict(row)


def output_path(relative_path: str) -> Path:
    settings = db.load_settings()
    base = db.project_path(settings["output_spanish"])
    return base / relative_path


def build_new_raw_line(old_raw_line: str, corrected_text: str) -> str:
    if ":" not in old_raw_line:
        raise ValueError("raw_line_missing_colon")
    prefix = old_raw_line.split(":", 1)[0]
    indent_match = re.match(r"^(\s*)", old_raw_line)
    indent = indent_match.group(1) if indent_match else ""
    return f"{indent}{prefix.strip()}: {yaml_quote_text(corrected_text)}"


def build_records(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    review_rows = read_review_rows()
    records: list[dict[str, Any]] = []
    for segment_id, (decision, corrected_text) in sorted(DECISIONS.items()):
        if decision != "corrected_text":
            continue
        review = review_rows.get(segment_id)
        context = fetch_context(conn, segment_id)
        old_text = context["output_text"] or ""
        reasons: list[str] = []
        if review is None:
            reasons.append("missing_review_packet_row")
        if not corrected_text:
            reasons.append("missing_corrected_text")
        if not token_integrity_ok(old_text, corrected_text):
            reasons.append("token_integrity_failed")
        if not structure_integrity_ok(old_text, corrected_text):
            reasons.append("structure_integrity_failed")
        if canonical_localization_text(old_text) == canonical_localization_text(corrected_text):
            reasons.append("no_output_change")
        raw_line = context.get("output_raw_line") or ""
        try:
            new_raw_line = build_new_raw_line(raw_line, corrected_text)
        except ValueError as exc:
            reasons.append(str(exc))
            new_raw_line = ""
        records.append(
            {
                "source": SOURCE,
                "record_type": "corrected_apply_item",
                "segment_id": segment_id,
                "relative_path": context["relative_path"],
                "source_key": context["source_key"],
                "source_line_number": context["source_line_number"],
                "old_output_text": old_text,
                "corrected_text": corrected_text,
                "old_raw_line": raw_line,
                "new_raw_line": new_raw_line,
                "token_integrity_ok": token_integrity_ok(old_text, corrected_text),
                "structure_integrity_ok": structure_integrity_ok(old_text, corrected_text),
                "canonical_l10n_changes": canonical_localization_text(old_text) != canonical_localization_text(corrected_text),
                "status": "ready" if not reasons else "blocked",
                "block_reasons": reasons,
                "candidate_generation_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )
    return records


def backup_files(records: list[dict[str, Any]]) -> Path:
    root = db.project_path("memory/backups") / f"release_readiness_ui_tooltips_packet2_context_sublote_corrected_{stamp()}"
    root.mkdir(parents=True, exist_ok=True)
    for relative in sorted({record["relative_path"] for record in records}):
        src = output_path(relative)
        dst = root / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return root


def apply_records(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> Path:
    backup_root = backup_files(records)
    by_file: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_file.setdefault(record["relative_path"], []).append(record)
    for relative, file_records in by_file.items():
        path = output_path(relative)
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        line_by_segment = {int(record["source_line_number"]): record for record in file_records}
        for line_no, record in line_by_segment.items():
            idx = line_no - 1
            if idx < 0 or idx >= len(lines):
                raise RuntimeError(f"line out of range for {record['segment_id']}")
            if lines[idx] == record["new_raw_line"]:
                continue
            if lines[idx] != record["old_raw_line"]:
                raise RuntimeError(f"raw line mismatch for {record['segment_id']}")
            lines[idx] = record["new_raw_line"]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    timestamp = now_iso()
    for record in records:
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
                record["new_raw_line"],
                sha256_text(record["corrected_text"]),
                timestamp,
                int(record["segment_id"]),
            ),
        )
        existing = conn.execute("SELECT id, locked FROM segment_confirmations WHERE segment_id = ?", (int(record["segment_id"]),)).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE segment_confirmations
                SET confirmation_level='human_confirmed',
                    confirmed_text=?,
                    confirmation_source='local_learning',
                    confirmation_label='ui_tooltips_packet2_context_sublote_corrected',
                    locked=1,
                    confidence_score=1.0,
                    reviewer='user_human_review_release_readiness_ui_tooltips_packet2_context_sublote',
                    confirmed_at=COALESCE(confirmed_at, ?),
                    updated_at=?
                WHERE segment_id=?
                """,
                (record["corrected_text"], timestamp, timestamp, int(record["segment_id"])),
            )
        else:
            conn.execute(
                """
                INSERT INTO segment_confirmations (
                  segment_id, confirmation_level, confirmed_text, confirmation_source,
                  confirmation_label, locked, confidence_score, reviewer, confirmed_at, updated_at
                )
                VALUES (?, 'human_confirmed', ?, 'local_learning', 'ui_tooltips_packet2_context_sublote_corrected', 1, 1.0, ?, ?, ?)
                """,
                (
                    int(record["segment_id"]),
                    record["corrected_text"],
                    "user_human_review_release_readiness_ui_tooltips_packet2_context_sublote",
                    timestamp,
                    timestamp,
                ),
            )
    conn.commit()
    return backup_root


def post_validate(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> dict[str, int]:
    output_ok = 0
    confirmation_ok = 0
    file_ok = 0
    for record in records:
        out = conn.execute("SELECT portuguese_text, output_raw_line FROM output_segments WHERE segment_id=?", (int(record["segment_id"]),)).fetchone()
        conf = conn.execute("SELECT confirmed_text, locked FROM segment_confirmations WHERE segment_id=?", (int(record["segment_id"]),)).fetchone()
        output_ok += int(bool(out and out["portuguese_text"] == record["corrected_text"] and out["output_raw_line"] == record["new_raw_line"]))
        confirmation_ok += int(bool(conf and conf["confirmed_text"] == record["corrected_text"] and int(conf["locked"] or 0) == 1))
        path = output_path(record["relative_path"])
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        file_ok += int(lines[int(record["source_line_number"]) - 1] == record["new_raw_line"])
    return {"output_db_ok": output_ok, "confirmation_ok": confirmation_ok, "file_ok": file_ok}


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_packet2_context_sublote_corrected_apply_{summary['mode']}"
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
                "UI/tooltips packet2 context sublote corrected protected apply",
                f"mode={summary['mode']}",
                f"record_count={summary['record_count']}",
                f"ready_count={summary['ready_count']}",
                f"blocked_count={summary['blocked_count']}",
                f"applied_count={summary['applied_count']}",
                f"backup_root={summary.get('backup_root') or ''}",
                f"post_validation={json.dumps(summary['post_validation'], ensure_ascii=False, sort_keys=True)}",
                "candidate_generation_count=0",
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
    mode = "apply" if args.apply else "dry_run"
    with db.connect(db.load_settings()) as conn:
        records = build_records(conn)
        if len(records) != EXPECTED_CORRECTED_TEXT:
            raise SystemExit(f"expected {EXPECTED_CORRECTED_TEXT} corrected rows, got {len(records)}")
        blocked = [record for record in records if record["status"] != "ready"]
        ready = [record for record in records if record["status"] == "ready"]
        backup_root = None
        post_validation = {"output_db_ok": 0, "confirmation_ok": 0, "file_ok": 0}
        if args.apply:
            if blocked:
                raise SystemExit("blocked records present; refusing apply")
            backup_root = apply_records(conn, ready)
            post_validation = post_validate(conn, ready)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": mode,
        "record_count": len(records),
        "ready_count": len(ready),
        "blocked_count": len(blocked),
        "applied_count": len(ready) if args.apply else 0,
        "token_integrity_ok_count": sum(1 for record in records if record["token_integrity_ok"]),
        "structure_integrity_ok_count": sum(1 for record in records if record["structure_integrity_ok"]),
        "canonical_l10n_changes_count": sum(1 for record in records if record["canonical_l10n_changes"]),
        "block_reason_counts": dict(Counter(reason for record in blocked for reason in record.get("block_reasons", [])).most_common()),
        "backup_root": str(backup_root) if backup_root else None,
        "post_validation": post_validation,
        "candidate_generation_count": 0,
        "apply_count": len(ready) if args.apply else 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": bool(args.apply and ready),
    }
    txt, jsonl, summary_path = write_reports(records, summary)
    print(f"txt={txt}")
    print(f"jsonl={jsonl}")
    print(f"summary={summary_path}")
    print(f"mode={mode}")
    print(f"record_count={summary['record_count']}")
    print(f"ready_count={summary['ready_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"applied_count={summary['applied_count']}")
    print(f"token_integrity_ok_count={summary['token_integrity_ok_count']}")
    print(f"structure_integrity_ok_count={summary['structure_integrity_ok_count']}")
    print(f"canonical_l10n_changes_count={summary['canonical_l10n_changes_count']}")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
