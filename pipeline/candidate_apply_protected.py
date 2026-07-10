from __future__ import annotations

import argparse
import difflib
import json
import shutil
import sqlite3
import subprocess
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


EXPECTED_SEGMENT_STATE_RUN_ID = 400
APPROVED_INDEXES = {1, 2, 3, 4, 5, 7, 8, 13, 14, 15, 16, 17}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def output_paths(mode: str) -> tuple[Path, Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_candidate_apply_protected_{mode}"
    md_suffix = "applied_diff" if mode == "apply" else "diff_preview"
    md_path = reports_dir() / f"{base.name.replace('_apply', '')}_{md_suffix}.md"
    if mode == "apply":
        md_path = reports_dir() / f"{base.name}_applied_diff.md"
    else:
        md_path = reports_dir() / f"{base.name}_diff_preview.md"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl"), reports_dir() / f"{base.name}_summary.json", md_path


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def connect_readonly() -> sqlite3.Connection:
    database_path = db.get_database_path(db.load_settings())
    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise SystemExit("database connection is not query_only")
    return conn


def git_status_source_output() -> list[str]:
    result = subprocess.run(
        ["git", "status", "--short", "--", "source", "output"],
        cwd=db.PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def validate_inputs(plan_rows: list[dict[str, Any]], plan_summary: dict[str, Any], decision_rows: list[dict[str, Any]], packet_rows: list[dict[str, Any]]) -> None:
    expected_summary = {
        "approved_input_count": 12,
        "plan_candidate_count": 12,
        "unique_segment_count": 12,
        "excluded_needs_more_context_count": 4,
        "excluded_duplicate_count": 1,
        "excluded_reject_count": 0,
        "guard_fail_count": 0,
        "eligible_for_future_apply_count": 12,
        "apply_now_count": 0,
        "requires_apply_later_count": 0,
        "requires_lifecycle_later_count": 0,
        "false_safe_risk_count": 0,
    }
    for key, expected in expected_summary.items():
        actual = int(plan_summary.get(key) or 0)
        if actual != expected:
            raise SystemExit(f"plan summary guard failed: {key} expected {expected}, got {actual}")
    if bool(plan_summary.get("source_output_modified")):
        raise SystemExit("plan summary source_output_modified guard failed")
    if len(plan_rows) != 12:
        raise SystemExit(f"plan row count guard failed: {len(plan_rows)}")
    indexes = {int(row["candidate_index"]) for row in plan_rows}
    if indexes != APPROVED_INDEXES:
        raise SystemExit(f"approved index guard failed: {sorted(indexes)}")
    for row in plan_rows:
        if not row.get("eligible_for_future_apply"):
            raise SystemExit(f"plan row eligibility guard failed: {row.get('candidate_index')}")
        if row.get("requires_lifecycle_later") or row.get("false_safe_risk"):
            raise SystemExit(f"plan row risk guard failed: {row.get('candidate_index')}")
    decisions = Counter(str(row.get("human_review_decision")) for row in decision_rows)
    if decisions.get("approve_for_future_apply", 0) != 12 or decisions.get("needs_more_context", 0) != 4 or decisions.get("duplicate_of_existing_candidate", 0) != 1:
        raise SystemExit("decision count guard failed")
    if len(packet_rows) != 17:
        raise SystemExit("packet row count guard failed")


def fetch_metadata(conn: sqlite3.Connection, segment_ids: list[int], run_id: int) -> dict[int, sqlite3.Row]:
    unique_ids = sorted(set(segment_ids))
    placeholders = ",".join("?" for _ in unique_ids)
    rows = conn.execute(
        f"""
        SELECT
            s.segment_id,
            s.state_group,
            s.is_closed,
            s.needs_output_apply,
            s.confirmed_matches_output,
            out.relative_path,
            out.output_line_number,
            out.output_raw_line,
            out.portuguese_text
        FROM segment_state_items s
        JOIN output_segments out ON out.segment_id = s.segment_id
        WHERE s.run_id = ? AND s.segment_id IN ({placeholders})
        """,
        (run_id, *unique_ids),
    ).fetchall()
    if len(rows) != len(unique_ids):
        raise SystemExit(f"metadata row count guard failed: expected {len(unique_ids)}, got {len(rows)}")
    return {int(row["segment_id"]): row for row in rows}


def target_file(relative_path: str) -> Path:
    return db.PROJECT_ROOT / "output" / "spanish" / relative_path


def replace_raw_value(raw_line: str, candidate_text: str) -> str:
    first = raw_line.find('"')
    last = raw_line.rfind('"')
    if first < 0 or last <= first:
        raise ValueError("raw line does not contain a quoted value")
    return raw_line[: first + 1] + candidate_text + raw_line[last:]


def file_encoding(path: Path) -> str:
    with path.open("rb") as handle:
        prefix = handle.read(3)
    return "utf-8-sig" if prefix == b"\xef\xbb\xbf" else "utf-8"


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


def build_records(plan_rows: list[dict[str, Any]], metadata: dict[int, sqlite3.Row], snapshot_dir: Path | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_targets: Counter[tuple[str, int]] = Counter()
    for row in plan_rows:
        meta = metadata[int(row["segment_id"])]
        path = target_file(str(meta["relative_path"]))
        line_number = int(meta["output_line_number"])
        target_key = (str(path), line_number)
        seen_targets[target_key] += 1
    for row in sorted(plan_rows, key=lambda item: int(item["candidate_index"])):
        segment_id = int(row["segment_id"])
        meta = metadata[segment_id]
        path = target_file(str(meta["relative_path"]))
        line_number = int(meta["output_line_number"])
        current_text = str(row["current_output_text"])
        candidate_text = str(row["candidate_text"])
        state_guard_ok = (
            meta["state_group"] == "pending"
            and int(meta["is_closed"] or 0) == 0
            and int(meta["needs_output_apply"] or 0) == 0
            and int(meta["confirmed_matches_output"] or 0) == 1
        )
        file_guard_ok = False
        unique_replacement_ok = False
        notes: list[str] = []
        if not path.exists():
            notes.append("target file missing")
        else:
            lines = read_lines(path)
            if 1 <= line_number <= len(lines):
                expected_raw = str(meta["output_raw_line"])
                actual_raw = line_without_newline(lines[line_number - 1])
                file_guard_ok = actual_raw == expected_raw and str(meta["portuguese_text"]) == current_text
                if not file_guard_ok:
                    notes.append("target line or db output does not match expected current output")
                unique_replacement_ok = seen_targets[(str(path), line_number)] == 1 and candidate_text != current_text
            else:
                notes.append("target line number out of range")
        token_ok = bool(row["token_integrity_ok"])
        structure_ok = bool(row["structure_integrity_ok"])
        snapshot_path = ""
        if snapshot_dir is not None and path.exists():
            snapshot_path = str(snapshot_dir / path.relative_to(db.PROJECT_ROOT))
        records.append(
            {
                "candidate_index": int(row["candidate_index"]),
                "segment_id": segment_id,
                "target_file": str(path),
                "target_line_number": line_number,
                "current_output_text": current_text,
                "candidate_text": candidate_text,
                "expected_applied_raw_line": replace_raw_value(str(meta["output_raw_line"]), candidate_text),
                "dry_run_decision": "eligible_for_apply"
                if all([state_guard_ok, file_guard_ok, unique_replacement_ok, token_ok, structure_ok])
                else "guard_failed",
                "apply_decision": "not_run",
                "token_integrity_ok": token_ok,
                "structure_integrity_ok": structure_ok,
                "state_guard_ok": state_guard_ok,
                "file_guard_ok": file_guard_ok,
                "unique_replacement_ok": unique_replacement_ok,
                "snapshot_path": snapshot_path,
                "applied": False,
                "rollback_executed": False,
                "requires_lifecycle_later": False,
                "false_safe_risk": False,
                "notes": "; ".join(notes),
            }
        )
    return records


def summary_from_records(records: list[dict[str, Any]], pre_dirty: bool, mode: str, snapshots: dict[str, str] | None = None, rollback: bool = False, post_ok: bool | None = None) -> dict[str, Any]:
    guard_fail = sum(1 for row in records if row["dry_run_decision"] != "eligible_for_apply")
    summary = {
        "schema_version": 1,
        "source": f"candidate_apply_protected_{mode}_v1",
        "approved_input_count": 12,
        "dry_run_candidate_count": len(records),
        "eligible_for_apply_count": sum(1 for row in records if row["dry_run_decision"] == "eligible_for_apply"),
        "guard_fail_count": guard_fail,
        "state_guard_fail_count": sum(1 for row in records if not row["state_guard_ok"]),
        "file_guard_fail_count": sum(1 for row in records if not row["file_guard_ok"]),
        "unique_replacement_fail_count": sum(1 for row in records if not row["unique_replacement_ok"]),
        "token_integrity_fail_count": sum(1 for row in records if not row["token_integrity_ok"]),
        "structure_integrity_fail_count": sum(1 for row in records if not row["structure_integrity_ok"]),
        "requires_lifecycle_later_count": sum(1 for row in records if row["requires_lifecycle_later"]),
        "false_safe_risk_count": sum(1 for row in records if row["false_safe_risk"]),
        "pre_apply_source_output_dirty": pre_dirty,
        "applied_count": sum(1 for row in records if row["applied"]),
        "skipped_count": sum(1 for row in records if not row["applied"]) if mode == "apply" else 0,
        "files_changed_count": len({row["target_file"] for row in records if row["applied"]}),
        "snapshot_count": len(snapshots or {}),
        "rollback_executed": rollback,
        "post_apply_validation_ok": bool(post_ok) if post_ok is not None else False,
        "source_changed": False,
        "output_changed": any(row["applied"] for row in records),
        "production_full_recommended_now": False,
        "next_prompt": "chat_exec_candidate_apply_post_validation_prompt.md" if mode == "apply" and not rollback and post_ok else "chat_exec_candidate_apply_plan_guard_audit_prompt.md",
    }
    return summary


def validate_dry_run_summary(summary: dict[str, Any]) -> bool:
    expected = {
        "approved_input_count": 12,
        "dry_run_candidate_count": 12,
        "eligible_for_apply_count": 12,
        "guard_fail_count": 0,
        "state_guard_fail_count": 0,
        "file_guard_fail_count": 0,
        "unique_replacement_fail_count": 0,
        "token_integrity_fail_count": 0,
        "structure_integrity_fail_count": 0,
        "requires_lifecycle_later_count": 0,
        "false_safe_risk_count": 0,
    }
    return all(int(summary.get(key) or 0) == value for key, value in expected.items()) and not bool(summary.get("pre_apply_source_output_dirty"))


def unified_file_diff(path: Path, old_lines: list[str], new_lines: list[str]) -> str:
    return "\n".join(
        difflib.unified_diff(
            [line.rstrip("\n") for line in old_lines],
            [line.rstrip("\n") for line in new_lines],
            fromfile=str(path),
            tofile=str(path),
            lineterm="",
        )
    )


def render_diff_markdown(path: Path, records: list[dict[str, Any]], title: str, file_diffs: dict[str, str] | None = None) -> None:
    lines = [f"# {title}", ""]
    for record in records:
        lines.extend(
            [
                f"## Candidate {record['candidate_index']} / segment {record['segment_id']}",
                "",
                f"Target: `{record['target_file']}` line `{record['target_line_number']}`",
                f"Dry-run decision: `{record['dry_run_decision']}`",
                f"Apply decision: `{record['apply_decision']}`",
                "",
                "Current:",
                "```text",
                record["current_output_text"],
                "```",
                "",
                "Candidate:",
                "```text",
                record["candidate_text"],
                "```",
                "",
            ]
        )
    if file_diffs:
        lines.append("# File Diffs")
        lines.append("")
        for file_path, diff in file_diffs.items():
            lines.extend([f"## {file_path}", "", "```diff", diff, "```", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def write_report(mode: str, records: list[dict[str, Any]], summary: dict[str, Any], file_diffs: dict[str, str] | None = None) -> tuple[Path, Path, Path, Path]:
    txt_path, jsonl_path, summary_path, md_path = output_paths(mode)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    render_diff_markdown(md_path, records, "Candidate Apply Protected Applied Diff" if mode == "apply" else "Candidate Apply Protected Diff Preview", file_diffs)
    keys = [
        "approved_input_count",
        "dry_run_candidate_count",
        "eligible_for_apply_count",
        "applied_count",
        "skipped_count",
        "guard_fail_count",
        "files_changed_count",
        "snapshot_count",
        "rollback_executed",
        "post_apply_validation_ok",
        "source_changed",
        "output_changed",
        "requires_lifecycle_later_count",
        "false_safe_risk_count",
        "production_full_recommended_now",
        "next_prompt",
    ]
    lines = [f"candidate apply protected {mode}", *[f"{key}={summary.get(key)}" for key in keys]]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path, md_path


def create_snapshots(records: list[dict[str, Any]], snapshot_dir: Path) -> dict[str, str]:
    snapshots: dict[str, str] = {}
    for file_name in sorted({row["target_file"] for row in records}):
        source = Path(file_name)
        rel = source.relative_to(db.PROJECT_ROOT)
        dest = snapshot_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        snapshots[str(source)] = str(dest)
    mapping_path = snapshot_dir / "snapshot_mapping.json"
    mapping_path.write_text(json.dumps(snapshots, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return snapshots


def apply_records(records: list[dict[str, Any]], snapshots: dict[str, str]) -> tuple[list[dict[str, Any]], dict[str, str], bool]:
    file_to_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        file_to_records[record["target_file"]].append(record)
    file_diffs: dict[str, str] = {}
    try:
        for file_name, file_records in file_to_records.items():
            path = Path(file_name)
            old_lines = read_lines(path)
            new_lines = list(old_lines)
            for record in sorted(file_records, key=lambda item: int(item["target_line_number"])):
                line_index = int(record["target_line_number"]) - 1
                current_raw = line_without_newline(new_lines[line_index])
                if current_raw != replace_raw_value(current_raw, record["current_output_text"]):
                    raise ValueError(f"target raw line changed before apply: {record['target_file']}:{record['target_line_number']}")
                new_raw = record["expected_applied_raw_line"]
                new_lines[line_index] = new_raw + line_ending(new_lines[line_index])
                record["applied"] = True
                record["apply_decision"] = "applied"
            file_diffs[file_name] = unified_file_diff(path, old_lines, new_lines)
            write_lines(path, new_lines)
        return records, file_diffs, False
    except Exception:
        for source, snap in snapshots.items():
            shutil.copy2(snap, source)
        for record in records:
            record["rollback_executed"] = True
            if not record["applied"]:
                record["apply_decision"] = "rollback_after_error"
        return records, file_diffs, True


def post_apply_validate(records: list[dict[str, Any]]) -> bool:
    for record in records:
        path = Path(record["target_file"])
        lines = read_lines(path)
        actual = line_without_newline(lines[int(record["target_line_number"]) - 1])
        if actual != record["expected_applied_raw_line"]:
            return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-jsonl", required=True)
    parser.add_argument("--plan-summary-json", required=True)
    parser.add_argument("--decision-jsonl", required=True)
    parser.add_argument("--packet-jsonl", required=True)
    parser.add_argument("--segment-state-run-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.segment_state_run_id != EXPECTED_SEGMENT_STATE_RUN_ID:
        raise SystemExit("segment_state_run_id guard failed")

    plan_rows = read_jsonl(db.project_path(args.plan_jsonl))
    plan_summary = read_json(db.project_path(args.plan_summary_json))
    decision_rows = read_jsonl(db.project_path(args.decision_jsonl))
    packet_rows = read_jsonl(db.project_path(args.packet_jsonl))
    validate_inputs(plan_rows, plan_summary, decision_rows, packet_rows)
    pre_dirty_lines = git_status_source_output()
    pre_dirty = bool(pre_dirty_lines)
    with connect_readonly() as conn:
        metadata = fetch_metadata(conn, [int(row["segment_id"]) for row in plan_rows], args.segment_state_run_id)
    records = build_records(plan_rows, metadata)
    dry_summary = summary_from_records(records, pre_dirty, "dry_run")
    dry_ok = validate_dry_run_summary(dry_summary)
    if not args.apply:
        txt_path, jsonl_path, summary_path, md_path = write_report("dry_run", records, dry_summary)
        print(f"txt={txt_path}")
        print(f"jsonl={jsonl_path}")
        print(f"summary={summary_path}")
        print(f"markdown={md_path}")
        print(f"dry_run_ok={dry_ok}")
        print(f"guard_fail_count={dry_summary['guard_fail_count']}")
        print(f"eligible_for_apply_count={dry_summary['eligible_for_apply_count']}")
        return
    if not dry_ok:
        write_report("dry_run", records, dry_summary)
        raise SystemExit("dry-run guards failed; apply aborted")
    snapshot_dir = reports_dir() / f"{stamp()}_candidate_apply_protected_snapshots"
    records = build_records(plan_rows, metadata, snapshot_dir)
    snapshots = create_snapshots(records, snapshot_dir)
    records, file_diffs, rollback = apply_records(records, snapshots)
    post_ok = (not rollback) and post_apply_validate(records)
    if not post_ok and not rollback:
        for source, snap in snapshots.items():
            shutil.copy2(snap, source)
        rollback = True
        for record in records:
            record["rollback_executed"] = True
    apply_summary = summary_from_records(records, pre_dirty, "apply", snapshots, rollback, post_ok and not rollback)
    txt_path, jsonl_path, summary_path, md_path = write_report("apply", records, apply_summary, file_diffs)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"markdown={md_path}")
    print(f"applied_count={apply_summary['applied_count']}")
    print(f"files_changed_count={apply_summary['files_changed_count']}")
    print(f"rollback_executed={apply_summary['rollback_executed']}")
    print(f"post_apply_validation_ok={apply_summary['post_apply_validation_ok']}")


if __name__ == "__main__":
    main()
