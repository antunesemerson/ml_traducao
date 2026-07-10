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
from release_readiness_ui_tooltips_packet2_spanish_residue_3_decisions import DECISIONS, EXPECTED_CORRECTED_TEXT


SOURCE = "release_readiness_ui_tooltips_packet2_spanish_residue_3_corrected_apply_v1"
CONFIRMATION_LABEL = "ui_tooltips_packet2_spanish_residue_3_corrected"
REVIEWER = "user_human_review_release_readiness_ui_tooltips_packet2_spanish_residue_3"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def output_path(relative_path: str) -> Path:
    return db.project_path(db.load_settings()["output_spanish"]) / relative_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Protected apply for UI/tooltips spanish residue 3 corrected row.")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def yaml_quote_text(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_new_raw_line(old_raw_line: str, corrected_text: str) -> str:
    if ":" not in old_raw_line:
        raise ValueError("raw_line_missing_colon")
    prefix = old_raw_line.split(":", 1)[0]
    indent_match = re.match(r"^(\s*)", old_raw_line)
    indent = indent_match.group(1) if indent_match else ""
    return f"{indent}{prefix.strip()}: {yaml_quote_text(corrected_text)}"


def token_integrity_ok(old: str, new: str) -> bool:
    return Counter(protected_tokens(old)) == Counter(protected_tokens(new))


def structure_integrity_ok(old: str, new: str) -> bool:
    markers = ["#weak", "#WEAK", "#D", "#!", "#EMP", "#V"]
    return all((m in old) == (m in new) for m in markers) and old.count("$") == new.count("$") and old.count("[") == new.count("[") and old.count("]") == new.count("]")


def fetch_context(conn: sqlite3.Connection, segment_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT s.id AS segment_id, s.relative_path, s.source_key, s.source_line_number,
               o.portuguese_text AS output_text, o.output_raw_line, c.confirmed_text
        FROM source_segments s
        JOIN output_segments o ON o.segment_id=s.id
        LEFT JOIN segment_confirmations c ON c.segment_id=s.id
        WHERE s.id=?
        """,
        (segment_id,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"missing segment {segment_id}")
    return dict(row)


def build_records(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for segment_id, (decision, corrected_text) in sorted(DECISIONS.items()):
        if decision != "corrected_text":
            continue
        context = fetch_context(conn, segment_id)
        old = context["output_text"] or ""
        reasons: list[str] = []
        try:
            new_raw_line = build_new_raw_line(context["output_raw_line"] or "", corrected_text)
        except ValueError as exc:
            new_raw_line = ""
            reasons.append(str(exc))
        if not token_integrity_ok(old, corrected_text):
            reasons.append("token_integrity_failed")
        if not structure_integrity_ok(old, corrected_text):
            reasons.append("structure_integrity_failed")
        if canonical_localization_text(old) == canonical_localization_text(corrected_text):
            reasons.append("no_output_change")
        records.append(
            {
                "source": SOURCE,
                "record_type": "corrected_apply_item",
                "segment_id": segment_id,
                "relative_path": context["relative_path"],
                "source_key": context["source_key"],
                "source_line_number": context["source_line_number"],
                "old_output_text": old,
                "corrected_text": corrected_text,
                "old_raw_line": context["output_raw_line"],
                "new_raw_line": new_raw_line,
                "token_integrity_ok": token_integrity_ok(old, corrected_text),
                "structure_integrity_ok": structure_integrity_ok(old, corrected_text),
                "canonical_l10n_changes": canonical_localization_text(old) != canonical_localization_text(corrected_text),
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
    root = db.project_path("memory/backups") / f"release_readiness_ui_tooltips_packet2_spanish_residue_3_{stamp()}"
    root.mkdir(parents=True, exist_ok=True)
    for relative in sorted({r["relative_path"] for r in records}):
        src = output_path(relative)
        dst = root / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return root


def apply_records(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> Path:
    backup_root = backup_files(records)
    timestamp = now_iso()
    for record in records:
        path = output_path(record["relative_path"])
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        idx = int(record["source_line_number"]) - 1
        if lines[idx] != record["old_raw_line"]:
            raise RuntimeError(f"raw line mismatch for {record['segment_id']}")
        lines[idx] = record["new_raw_line"]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        conn.execute(
            """
            UPDATE output_segments
            SET portuguese_text=?, output_raw_line=?, portuguese_hash=?, last_indexed_at=?
            WHERE segment_id=?
            """,
            (record["corrected_text"], record["new_raw_line"], sha256_text(record["corrected_text"]), timestamp, int(record["segment_id"])),
        )
        conn.execute(
            """
            UPDATE segment_confirmations
            SET confirmation_level='human_confirmed', confirmed_text=?, confirmation_source='local_learning',
                confirmation_label=?, locked=1, confidence_score=1.0, reviewer=?,
                confirmed_at=COALESCE(confirmed_at, ?), updated_at=?
            WHERE segment_id=?
            """,
            (record["corrected_text"], CONFIRMATION_LABEL, REVIEWER, timestamp, timestamp, int(record["segment_id"])),
        )
    conn.commit()
    return backup_root


def post_validate(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> dict[str, int]:
    file_ok = output_ok = confirmation_ok = 0
    for record in records:
        out = conn.execute("SELECT portuguese_text, output_raw_line FROM output_segments WHERE segment_id=?", (int(record["segment_id"]),)).fetchone()
        conf = conn.execute("SELECT confirmed_text, locked FROM segment_confirmations WHERE segment_id=?", (int(record["segment_id"]),)).fetchone()
        lines = output_path(record["relative_path"]).read_text(encoding="utf-8-sig").splitlines()
        file_ok += int(lines[int(record["source_line_number"]) - 1] == record["new_raw_line"])
        output_ok += int(bool(out and out["portuguese_text"] == record["corrected_text"] and out["output_raw_line"] == record["new_raw_line"]))
        confirmation_ok += int(bool(conf and conf["confirmed_text"] == record["corrected_text"] and int(conf["locked"] or 0) == 1))
    return {"file_ok": file_ok, "output_db_ok": output_ok, "confirmation_ok": confirmation_ok}


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_packet2_spanish_residue_3_corrected_apply_{summary['mode']}"
    txt = base.with_suffix(".txt")
    jsonl = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt), "jsonl": str(jsonl), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt.write_text("\n".join([
        "UI/tooltips packet2 spanish residue 3 corrected protected apply",
        f"mode={summary['mode']}",
        f"record_count={summary['record_count']}",
        f"ready_count={summary['ready_count']}",
        f"blocked_count={summary['blocked_count']}",
        f"applied_count={summary['applied_count']}",
        f"post_validation={json.dumps(summary['post_validation'], ensure_ascii=False, sort_keys=True)}",
        f"backup_root={summary.get('backup_root') or ''}",
        "candidate_generation_count=0",
        "lifecycle_count=0",
        "segment_state_count=0",
        "reindex_count=0",
        "production_full_count=0",
    ]) + "\n", encoding="utf-8")
    print(f"txt={txt}")
    print(f"jsonl={jsonl}")
    print(f"summary={summary_path}")


def main() -> None:
    args = parse_args()
    mode = "apply" if args.apply else "dry_run"
    with db.connect(db.load_settings()) as conn:
        records = build_records(conn)
        if len(records) != EXPECTED_CORRECTED_TEXT:
            raise SystemExit(f"expected {EXPECTED_CORRECTED_TEXT}, got {len(records)}")
        ready = [r for r in records if r["status"] == "ready"]
        blocked = [r for r in records if r["status"] != "ready"]
        backup_root = None
        post = {"file_ok": 0, "output_db_ok": 0, "confirmation_ok": 0}
        if args.apply:
            if blocked:
                raise SystemExit("blocked records present; refusing apply")
            backup_root = apply_records(conn, ready)
            post = post_validate(conn, ready)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": mode,
        "record_count": len(records),
        "ready_count": len(ready),
        "blocked_count": len(blocked),
        "applied_count": len(ready) if args.apply else 0,
        "token_integrity_ok_count": sum(1 for r in records if r["token_integrity_ok"]),
        "structure_integrity_ok_count": sum(1 for r in records if r["structure_integrity_ok"]),
        "canonical_l10n_changes_count": sum(1 for r in records if r["canonical_l10n_changes"]),
        "block_reason_counts": dict(Counter(x for r in blocked for x in r["block_reasons"]).most_common()),
        "backup_root": str(backup_root) if backup_root else None,
        "post_validation": post,
        "candidate_generation_count": 0,
        "apply_count": len(ready) if args.apply else 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": bool(args.apply and ready),
    }
    write_reports(records, summary)
    print(f"mode={mode}")
    print(f"record_count={summary['record_count']}")
    print(f"ready_count={summary['ready_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"applied_count={summary['applied_count']}")
    print(f"token_integrity_ok_count={summary['token_integrity_ok_count']}")
    print(f"structure_integrity_ok_count={summary['structure_integrity_ok_count']}")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
