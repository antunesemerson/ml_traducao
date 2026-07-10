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


SOURCE = "release_readiness_narrative_plain_light_batch1_apply_v1"
PREVIEW_JSONL = Path("reports/20260703_010547_366364_release_readiness_narrative_plain_light_corrected_preview.jsonl")
TARGET_IDS = [113374, 70298, 30863, 30864, 31045, 55019, 70373, 102046, 102719, 112779]
BLOCKED_IDS = {79601, 112693, 112699, 104908, 65282, 66438, 66439, 70297}
CONFIRMATION_LABEL = "narrative_plain_light_batch1_corrected"
REVIEWER = "user_human_review_release_readiness_narrative_plain_light_batch1"


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
    parser = argparse.ArgumentParser(description="Protected apply for narrative plain/light batch1 corrected rows.")
    parser.add_argument("--preview-jsonl", type=Path, default=PREVIEW_JSONL)
    parser.add_argument("--target-ids", default=",".join(str(segment_id) for segment_id in TARGET_IDS))
    parser.add_argument("--blocked-ids", default=",".join(str(segment_id) for segment_id in sorted(BLOCKED_IDS)))
    parser.add_argument("--confirmation-label", default=CONFIRMATION_LABEL)
    parser.add_argument("--reviewer", default=REVIEWER)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def parse_segment_ids(value: str) -> list[int]:
    if not value.strip():
        return []
    return [int(part.strip()) for part in value.split(",") if part.strip()]


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
    markers = ["#T", "#X", "#P", "#N", "#EMP", "#weak", "#WEAK", "#!", "@warning_icon!", "@provisions_icon!", "@gold_icon!", "@herd_icon!"]
    if any((marker in old) != (marker in new) for marker in markers):
        return False
    return all(old.count(char) == new.count(char) for char in ["[", "]", "$"])


def read_preview(path: Path) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            rows[int(row["segment_id"])] = row
    return rows


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


def build_records(
    conn: sqlite3.Connection,
    preview_jsonl: Path,
    target_ids: list[int],
    blocked_ids: set[int],
) -> list[dict[str, Any]]:
    preview = read_preview(preview_jsonl)
    records: list[dict[str, Any]] = []
    for segment_id in target_ids:
        preview_row = preview.get(segment_id)
        context = fetch_context(conn, segment_id)
        corrected = str(preview_row.get("proposed_corrected_text") if preview_row else "")
        old = context["output_text"] or ""
        reasons: list[str] = []
        if segment_id in blocked_ids:
            reasons.append("segment_in_blocked_set")
        if preview_row is None:
            reasons.append("missing_preview_row")
        elif preview_row.get("status") != "ready_for_protected_apply":
            reasons.append("preview_not_ready")
        if not corrected:
            reasons.append("missing_corrected_text")
        try:
            new_raw_line = build_new_raw_line(context["output_raw_line"] or "", corrected)
        except ValueError as exc:
            new_raw_line = ""
            reasons.append(str(exc))
        tok_ok = token_integrity_ok(old, corrected) if corrected else False
        struct_ok = structure_integrity_ok(old, corrected) if corrected else False
        canon_change = canonical_localization_text(old) != canonical_localization_text(corrected) if corrected else False
        if not tok_ok:
            reasons.append("token_integrity_failed")
        if not struct_ok:
            reasons.append("structure_integrity_failed")
        if not canon_change:
            reasons.append("canonical_l10n_no_change")
        records.append(
            {
                "source": SOURCE,
                "record_type": "protected_apply_item",
                "segment_id": segment_id,
                "relative_path": context["relative_path"],
                "source_key": context["source_key"],
                "source_line_number": context["source_line_number"],
                "old_output_text": old,
                "corrected_text": corrected,
                "old_raw_line": context["output_raw_line"],
                "new_raw_line": new_raw_line,
                "token_integrity_ok": tok_ok,
                "structure_integrity_ok": struct_ok,
                "canonical_l10n_changes": canon_change,
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
    root = db.project_path("memory/backups") / f"release_readiness_narrative_plain_light_batch1_{stamp()}"
    root.mkdir(parents=True, exist_ok=True)
    for relative in sorted({record["relative_path"] for record in records}):
        src = output_path(relative)
        dst = root / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return root


def apply_records(
    conn: sqlite3.Connection,
    records: list[dict[str, Any]],
    confirmation_label: str,
    reviewer: str,
) -> Path:
    backup_root = backup_files(records)
    by_file: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_file.setdefault(record["relative_path"], []).append(record)
    for relative, file_records in by_file.items():
        path = output_path(relative)
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        for record in file_records:
            idx = int(record["source_line_number"]) - 1
            if lines[idx] != record["old_raw_line"]:
                raise RuntimeError(f"raw line mismatch for {record['segment_id']}")
            lines[idx] = record["new_raw_line"]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    timestamp = now_iso()
    for record in records:
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
            (record["corrected_text"], confirmation_label, reviewer, timestamp, timestamp, int(record["segment_id"])),
        )
        if conn.total_changes == 0:
            conn.execute(
                """
                INSERT INTO segment_confirmations (
                  segment_id, confirmation_level, confirmed_text, confirmation_source,
                  confirmation_label, locked, confidence_score, reviewer, confirmed_at, updated_at
                )
                VALUES (?, 'human_confirmed', ?, 'local_learning', ?, 1, 1.0, ?, ?, ?)
                """,
                (int(record["segment_id"]), record["corrected_text"], confirmation_label, reviewer, timestamp, timestamp),
            )
    conn.commit()
    return backup_root


def post_validate(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> dict[str, int]:
    file_ok = output_ok = confirmation_ok = token_ok = structure_ok = 0
    for record in records:
        out = conn.execute("SELECT portuguese_text, output_raw_line FROM output_segments WHERE segment_id=?", (int(record["segment_id"]),)).fetchone()
        conf = conn.execute("SELECT confirmed_text, locked FROM segment_confirmations WHERE segment_id=?", (int(record["segment_id"]),)).fetchone()
        lines = output_path(record["relative_path"]).read_text(encoding="utf-8-sig").splitlines()
        file_ok += int(lines[int(record["source_line_number"]) - 1] == record["new_raw_line"])
        output_ok += int(bool(out and out["portuguese_text"] == record["corrected_text"] and out["output_raw_line"] == record["new_raw_line"]))
        confirmation_ok += int(bool(conf and conf["confirmed_text"] == record["corrected_text"] and int(conf["locked"] or 0) == 1))
        token_ok += int(token_integrity_ok(record["old_output_text"], record["corrected_text"]))
        structure_ok += int(structure_integrity_ok(record["old_output_text"], record["corrected_text"]))
    return {"file_ok": file_ok, "output_db_ok": output_ok, "confirmation_ok": confirmation_ok, "token_integrity_ok": token_ok, "structure_integrity_ok": structure_ok}


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    base = reports_dir() / f"{stamp()}_release_readiness_narrative_plain_light_batch1_apply_{summary['mode']}"
    txt = base.with_suffix(".txt")
    jsonl = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt), "jsonl": str(jsonl), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt.write_text("\n".join([
        "Narrative plain/light batch1 protected apply",
        f"mode={summary['mode']}",
        f"record_count={summary['record_count']}",
        f"ready_count={summary['ready_count']}",
        f"blocked_count={summary['blocked_count']}",
        f"applied_count={summary['applied_count']}",
        f"backup_root={summary.get('backup_root') or ''}",
        f"post_validation={json.dumps(summary['post_validation'], ensure_ascii=False, sort_keys=True)}",
        f"block_reason_counts={json.dumps(summary['block_reason_counts'], ensure_ascii=False, sort_keys=True)}",
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
    target_ids = parse_segment_ids(args.target_ids)
    blocked_ids = set(parse_segment_ids(args.blocked_ids))
    with db.connect(db.load_settings()) as conn:
        records = build_records(conn, args.preview_jsonl, target_ids, blocked_ids)
        ready = [record for record in records if record["status"] == "ready"]
        blocked = [record for record in records if record["status"] != "ready"]
        backup_root = None
        post = {"file_ok": 0, "output_db_ok": 0, "confirmation_ok": 0, "token_integrity_ok": 0, "structure_integrity_ok": 0}
        if args.apply:
            if blocked:
                raise SystemExit("blocked records present; refusing apply")
            backup_root = apply_records(conn, ready, args.confirmation_label, args.reviewer)
            post = post_validate(conn, ready)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": mode,
        "record_count": len(records),
        "ready_count": len(ready),
        "blocked_count": len(blocked),
        "applied_count": len(ready) if args.apply else 0,
        "preview_jsonl": str(args.preview_jsonl),
        "confirmation_label": args.confirmation_label,
        "reviewer": args.reviewer,
        "target_segment_ids": target_ids,
        "excluded_blocked_segment_ids": sorted(blocked_ids),
        "token_integrity_ok_count": sum(1 for record in records if record["token_integrity_ok"]),
        "structure_integrity_ok_count": sum(1 for record in records if record["structure_integrity_ok"]),
        "canonical_l10n_changes_count": sum(1 for record in records if record["canonical_l10n_changes"]),
        "block_reason_counts": dict(Counter(reason for record in blocked for reason in record["block_reasons"]).most_common()),
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
    print(f"canonical_l10n_changes_count={summary['canonical_l10n_changes_count']}")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
