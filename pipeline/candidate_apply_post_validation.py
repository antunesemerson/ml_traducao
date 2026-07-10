from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import db


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def project_path(value: str) -> Path:
    return db.project_path(value)


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


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
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def file_encoding(path: Path) -> str:
    with path.open("rb") as handle:
        return "utf-8-sig" if handle.read(3) == b"\xef\xbb\xbf" else "utf-8"


def read_lines(path: Path) -> list[str]:
    with path.open("r", encoding=file_encoding(path), newline="") as handle:
        return handle.readlines()


def line_without_newline(value: str) -> str:
    return value.rstrip("\r\n")


def git_status_source_output() -> tuple[list[str], bool, bool]:
    result = subprocess.run(
        ["git", "status", "--short", "--", "source", "output"],
        cwd=db.PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    source_changed = any(line[3:].startswith("source/") or line[3:] == "source" for line in lines)
    output_changed = any(line[3:].startswith("output/") or line[3:] == "output" for line in lines)
    return lines, source_changed, output_changed


def derived_apply_jsonl(apply_summary_path: Path) -> Path:
    name = apply_summary_path.name
    if not name.endswith("_summary.json"):
        raise SystemExit("apply summary path does not follow expected naming")
    return apply_summary_path.with_name(name.replace("_summary.json", ".jsonl"))


def validate_summary(apply_summary: dict[str, Any], dry_summary: dict[str, Any], applied_diff: Path, snapshot_dir: Path) -> list[str]:
    notes: list[str] = []
    expected_apply = {
        "applied_count": 12,
        "skipped_count": 0,
        "files_changed_count": 9,
        "snapshot_count": 9,
        "rollback_executed": False,
        "post_apply_validation_ok": True,
        "source_changed": False,
        "output_changed": True,
    }
    for key, expected in expected_apply.items():
        if apply_summary.get(key) != expected:
            notes.append(f"apply summary mismatch: {key} expected {expected!r}, got {apply_summary.get(key)!r}")
    expected_dry = {
        "dry_run_candidate_count": 12,
        "eligible_for_apply_count": 12,
        "guard_fail_count": 0,
        "pre_apply_source_output_dirty": False,
    }
    for key, expected in expected_dry.items():
        if dry_summary.get(key) != expected:
            notes.append(f"dry-run summary mismatch: {key} expected {expected!r}, got {dry_summary.get(key)!r}")
    if not applied_diff.exists():
        notes.append("applied diff markdown missing")
    if not snapshot_dir.exists() or not snapshot_dir.is_dir():
        notes.append("snapshot dir missing")
    return notes


def validate_records(apply_rows: list[dict[str, Any]], snapshot_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in sorted(apply_rows, key=lambda item: int(item["candidate_index"])):
        target = Path(str(row["target_file"]))
        line_number = int(row["target_line_number"])
        snapshot = snapshot_dir / target.relative_to(db.PROJECT_ROOT)
        notes: list[str] = []
        candidate_text_present = False
        old_text_still_present_at_target = False
        expected_applied = str(row.get("expected_applied_raw_line", ""))
        if not target.exists():
            notes.append("target file missing")
        else:
            lines = read_lines(target)
            if not (1 <= line_number <= len(lines)):
                notes.append("target line number out of range")
            else:
                actual = line_without_newline(lines[line_number - 1])
                candidate_text_present = actual == expected_applied
                old_text_still_present_at_target = str(row["current_output_text"]) in actual
                if not candidate_text_present:
                    notes.append("candidate text not present at expected target line")
                if old_text_still_present_at_target:
                    notes.append("old text still present at expected target line")
        snapshot_exists = snapshot.exists()
        if not snapshot_exists:
            notes.append("snapshot missing")
        token_ok = bool(row.get("token_integrity_ok"))
        structure_ok = bool(row.get("structure_integrity_ok"))
        if not token_ok:
            notes.append("token integrity false in apply record")
        if not structure_ok:
            notes.append("structure integrity false in apply record")
        post_ok = (
            bool(row.get("applied"))
            and candidate_text_present
            and not old_text_still_present_at_target
            and snapshot_exists
            and token_ok
            and structure_ok
        )
        records.append(
            {
                "candidate_index": int(row["candidate_index"]),
                "segment_id": int(row["segment_id"]),
                "target_file": str(target),
                "applied": bool(row.get("applied")),
                "post_validation_ok": post_ok,
                "candidate_text_present": candidate_text_present,
                "old_text_still_present_at_target": old_text_still_present_at_target,
                "snapshot_exists": snapshot_exists,
                "token_integrity_ok": token_ok,
                "structure_integrity_ok": structure_ok,
                "needs_manual_audit": not post_ok,
                "notes": "; ".join(notes),
            }
        )
    return records


def write_outputs(records: list[dict[str, Any]], summary: dict[str, Any], changed_files: list[str], git_status_lines: list[str]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_candidate_apply_post_validation"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "candidate apply post validation",
        f"validated={summary['post_validation_fail_count'] == 0}",
        "",
        "1. O apply protegido foi validado?",
        f"   {'Sim' if summary['post_validation_fail_count'] == 0 else 'Nao'}.",
        "2. Quais arquivos mudaram?",
        *[f"   - {file_name}" for file_name in changed_files],
        "3. source/ ficou limpo?",
        f"   {'Sim' if not summary['source_changed'] else 'Nao'}.",
        "4. output/ mudou como esperado?",
        f"   {'Sim' if summary['output_changed'] else 'Nao'}.",
        "5. Alguma linha precisa auditoria manual?",
        f"   {'Nao' if summary['manual_audit_required_count'] == 0 else 'Sim'}.",
        "6. Devemos rodar lifecycle/reindex agora?",
        "   Ainda nao neste prompt.",
        "7. Qual proximo passo recomendado?",
        f"   {summary['recommended_next_prompt']}",
        "",
        "git status --short -- source output:",
        *(git_status_lines or ["   <sem linhas reportadas pelo git>"]),
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply-summary-json", required=True)
    parser.add_argument("--dry-run-summary-json", required=True)
    parser.add_argument("--applied-diff-md", required=True)
    parser.add_argument("--snapshot-dir", required=True)
    args = parser.parse_args()

    apply_summary_path = project_path(args.apply_summary_json)
    dry_summary_path = project_path(args.dry_run_summary_json)
    applied_diff = project_path(args.applied_diff_md)
    snapshot_dir = project_path(args.snapshot_dir)
    apply_jsonl_path = derived_apply_jsonl(apply_summary_path)

    apply_summary = read_json(apply_summary_path)
    dry_summary = read_json(dry_summary_path)
    summary_notes = validate_summary(apply_summary, dry_summary, applied_diff, snapshot_dir)
    apply_rows = read_jsonl(apply_jsonl_path)
    records = validate_records(apply_rows, snapshot_dir)
    git_lines, git_source_changed, git_output_changed = git_status_source_output()

    changed_files = sorted({record["target_file"] for record in records if record["post_validation_ok"]})
    fail_count = sum(1 for record in records if not record["post_validation_ok"])
    manual_count = sum(1 for record in records if record["needs_manual_audit"])
    summary = {
        "schema_version": 1,
        "source": "candidate_apply_post_validation_v1",
        "applied_count": int(apply_summary.get("applied_count") or 0),
        "validated_count": sum(1 for record in records if record["post_validation_ok"]),
        "post_validation_fail_count": fail_count + len(summary_notes),
        "files_changed_count": len(changed_files),
        "snapshot_count": int(apply_summary.get("snapshot_count") or 0),
        "source_changed": bool(git_source_changed),
        "output_changed": bool(apply_summary.get("output_changed")) or bool(git_output_changed),
        "git_output_changed_reported": bool(git_output_changed),
        "rollback_executed": bool(apply_summary.get("rollback_executed")),
        "manual_audit_required_count": manual_count + len(summary_notes),
        "ready_for_lifecycle_now": False,
        "ready_for_reindex_now": False,
        "production_full_recommended_now": False,
        "recommended_next_prompt": "chat_exec_post_apply_light_diagnostic_prompt.md"
        if fail_count == 0 and not summary_notes
        else "chat_exec_candidate_apply_rollback_audit_prompt.md",
        "notes": summary_notes,
    }
    txt_path, jsonl_path, summary_path = write_outputs(records, summary, changed_files, git_lines)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"validated_count={summary['validated_count']}")
    print(f"post_validation_fail_count={summary['post_validation_fail_count']}")
    print(f"manual_audit_required_count={summary['manual_audit_required_count']}")
    print(f"recommended_next_prompt={summary['recommended_next_prompt']}")


if __name__ == "__main__":
    main()
