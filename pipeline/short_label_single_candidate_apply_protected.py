from __future__ import annotations

import argparse
import difflib
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "short_label_single_candidate_apply_protected_v1"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


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


def replace_raw_value(raw_line: str, candidate_text: str) -> str:
    first = raw_line.find('"')
    last = raw_line.rfind('"')
    if first < 0 or last <= first:
        raise ValueError("raw line does not contain a quoted value")
    return raw_line[: first + 1] + candidate_text + raw_line[last:]


def target_file(relative_path: str) -> Path:
    return db.PROJECT_ROOT / "output" / "spanish" / relative_path


def fetch_metadata(conn: sqlite3.Connection, segment_id: int, run_id: int):
    return conn.execute(
        """
        SELECT
            s.segment_id,
            s.state_group,
            s.is_closed,
            s.needs_output_apply,
            s.confirmed_matches_output,
            o.relative_path,
            o.output_line_number,
            o.portuguese_text,
            o.output_raw_line
        FROM segment_state_items s
        JOIN output_segments o ON o.segment_id = s.segment_id
        WHERE s.run_id = ?
          AND s.segment_id = ?
        """,
        (run_id, segment_id),
    ).fetchone()


def applied_segment_ids(paths: list[Path]) -> set[int]:
    applied: set[int] = set()
    for path in paths:
        if not path.exists():
            continue
        applied.update(int(row["segment_id"]) for row in read_jsonl(path) if row.get("applied"))
    return applied


def build_records(
    audit_rows: list[dict[str, Any]],
    already_applied: set[int],
    state_run_id: int,
    expected_count: int,
) -> list[dict[str, Any]]:
    safe_rows = [
        row
        for row in audit_rows
        if row.get("safe_for_future_apply_batch")
        and not row.get("false_safe_risk")
        and row.get("token_integrity_ok")
        and row.get("structure_integrity_ok")
    ]
    pending_rows = [row for row in safe_rows if int(row["segment_id"]) not in already_applied]
    if len(pending_rows) != expected_count:
        raise SystemExit(f"expected {expected_count} non-duplicate safe candidate(s), got {len(pending_rows)}")

    records: list[dict[str, Any]] = []
    with connect_readonly() as conn:
        for row in pending_rows:
            segment_id = int(row["segment_id"])
            meta = fetch_metadata(conn, segment_id, state_run_id)
            if meta is None:
                raise SystemExit(f"missing segment-state/output metadata for segment {segment_id}")

            path = target_file(str(meta["relative_path"]))
            notes: list[str] = []
            state_guard_ok = (
                meta["state_group"] == "pending"
                and int(meta["is_closed"] or 0) == 0
                and int(meta["needs_output_apply"] or 0) == 0
                and int(meta["confirmed_matches_output"] or 0) == 1
            )
            if not state_guard_ok:
                notes.append("state guard failed")

            file_guard_ok = False
            expected_raw = replace_raw_value(str(meta["output_raw_line"]), str(row["candidate_text"]))
            if not path.exists():
                notes.append("target file missing")
            else:
                lines = read_lines(path)
                line_number = int(meta["output_line_number"])
                if 1 <= line_number <= len(lines):
                    actual_raw = line_without_newline(lines[line_number - 1])
                    file_guard_ok = (
                        actual_raw == str(meta["output_raw_line"])
                        and str(meta["portuguese_text"]) == str(row["current_output_text"])
                    )
                    if not file_guard_ok:
                        notes.append("target line no longer matches indexed current output")
                else:
                    notes.append("target line number out of range")

            guard_ok = state_guard_ok and file_guard_ok
            records.append({
            "segment_id": segment_id,
            "target_file": str(path),
            "target_line_number": int(meta["output_line_number"]),
            "current_output_text": str(row["current_output_text"]),
            "candidate_text": str(row["candidate_text"]),
            "current_raw_line": str(meta["output_raw_line"]),
            "expected_applied_raw_line": expected_raw,
            "candidate_type": str(row.get("candidate_type") or ""),
            "audit_decision": str(row.get("audit_decision") or ""),
            "token_integrity_ok": bool(row.get("token_integrity_ok")),
            "structure_integrity_ok": bool(row.get("structure_integrity_ok")),
            "false_safe_risk": bool(row.get("false_safe_risk")),
            "state_guard_ok": state_guard_ok,
            "file_guard_ok": file_guard_ok,
            "guard_ok": guard_ok,
            "notes": "; ".join(notes),
            })
    return records


def write_diff(records: list[dict[str, Any]], mode: str, base_name: str) -> Path:
    path = reports_dir() / f"{base_name}_{mode}_diff_preview.md"
    lines = [f"# {RULE_VERSION} {mode}", ""]
    for record in records:
        diff = difflib.unified_diff(
            [record["current_raw_line"] + "\n"],
            [record["expected_applied_raw_line"] + "\n"],
            fromfile=f"before:{record['target_file']}:{record['target_line_number']}",
            tofile=f"after:{record['target_file']}:{record['target_line_number']}",
        )
        lines.extend(["```diff", *[line.rstrip("\n") for line in diff], "```", ""])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def apply_records(records: list[dict[str, Any]], base_name: str) -> Path:
    snapshot_root = reports_dir() / f"{base_name}_snapshots"
    for record in records:
        if not record["guard_ok"]:
            raise SystemExit("cannot apply record with failed guards")
        target = Path(record["target_file"])
        snapshot = snapshot_root / target.relative_to(db.PROJECT_ROOT)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, snapshot)
        lines = read_lines(target)
        index = int(record["target_line_number"]) - 1
        lines[index] = record["expected_applied_raw_line"] + line_ending(lines[index])
        write_lines(target, lines)
    return snapshot_root


def post_validate(records: list[dict[str, Any]]) -> tuple[int, list[str]]:
    notes: list[str] = []
    ok_count = 0
    for record in records:
        target = Path(record["target_file"])
        lines = read_lines(target)
        actual = line_without_newline(lines[int(record["target_line_number"]) - 1])
        if actual == record["expected_applied_raw_line"]:
            ok_count += 1
        else:
            notes.append(f"post validation failed for segment {record['segment_id']}")
    return ok_count, notes


def write_outputs(records: list[dict[str, Any]], summary: dict[str, Any], base_name: str) -> tuple[Path, Path]:
    txt_path = reports_dir() / f"{base_name}.txt"
    jsonl_path = reports_dir() / f"{base_name}.jsonl"
    summary_path = reports_dir() / f"{base_name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        RULE_VERSION,
        f"mode={summary['mode']}",
        f"candidate_count={summary['candidate_count']}",
        f"applied_count={summary['applied_count']}",
        f"guard_fail_count={summary['guard_fail_count']}",
        f"post_validation_fail_count={summary['post_validation_fail_count']}",
        f"snapshot_dir={summary['snapshot_dir']}",
        f"diff_preview={summary['diff_preview']}",
        f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit-jsonl", required=True)
    parser.add_argument("--applied-jsonl", action="append", required=True)
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--expected-count", type=int, default=1)
    parser.add_argument("--auto-apply", action="store_true")
    args = parser.parse_args()

    mode = "apply" if args.auto_apply else "dry_run"
    base_name = f"{stamp()}_short_label_single_candidate_apply_protected_{mode}"
    audit_rows = read_jsonl(db.project_path(args.audit_jsonl))
    already_applied = applied_segment_ids([db.project_path(path) for path in args.applied_jsonl])
    records = build_records(audit_rows, already_applied, args.segment_state_run_id, args.expected_count)
    diff_path = write_diff(records, mode, base_name)
    guard_fail_count = sum(1 for record in records if not record["guard_ok"])
    snapshot_dir = ""
    post_validation_fail_count = 0
    applied_count = 0
    post_notes: list[str] = []
    if args.auto_apply:
        if guard_fail_count:
            raise SystemExit("guard failed; apply blocked")
        snapshot_dir = str(apply_records(records, base_name))
        applied_count, post_notes = post_validate(records)
        post_validation_fail_count = len(post_notes) + (len(records) - applied_count)
        for record in records:
            record["applied"] = post_validation_fail_count == 0
            record["snapshot_dir"] = snapshot_dir
            record["rollback_path"] = str(Path(snapshot_dir) / Path(record["target_file"]).relative_to(db.PROJECT_ROOT))
    else:
        for record in records:
            record["applied"] = False
            record["snapshot_dir"] = ""
            record["rollback_path"] = ""
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "mode": mode,
        "candidate_count": len(records),
        "applied_count": applied_count,
        "guard_fail_count": guard_fail_count,
        "post_validation_fail_count": post_validation_fail_count,
        "diff_preview": str(diff_path),
        "snapshot_dir": snapshot_dir,
        "rollback_path_documented": bool(snapshot_dir) if args.auto_apply else False,
        "notes": post_notes,
        "production_full_recommended_now": False,
    }
    txt_path, summary_path = write_outputs(records, summary, base_name)
    print(f"txt={txt_path}")
    print(f"jsonl={reports_dir() / f'{base_name}.jsonl'}")
    print(f"summary={summary_path}")
    print(f"diff_preview={diff_path}")
    print(f"candidate_count={summary['candidate_count']}")
    print(f"applied_count={summary['applied_count']}")
    print(f"guard_fail_count={summary['guard_fail_count']}")
    print(f"post_validation_fail_count={summary['post_validation_fail_count']}")


if __name__ == "__main__":
    main()
