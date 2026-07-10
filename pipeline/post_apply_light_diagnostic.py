from __future__ import annotations

import argparse
import filecmp
import json
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def project_path(value: str) -> Path:
    return db.project_path(value)


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


def git_status(paths: list[str]) -> list[str]:
    result = subprocess.run(
        ["git", "status", "--short", "--", *paths],
        cwd=db.PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def git_ls_files(paths: list[str]) -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "--", *paths],
        cwd=db.PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return {line.replace("/", "\\") for line in result.stdout.splitlines() if line.strip()}


def git_check_ignore(path: Path) -> str:
    rel = path.relative_to(db.PROJECT_ROOT).as_posix()
    result = subprocess.run(
        ["git", "check-ignore", "-v", "--", rel],
        cwd=db.PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip()


def rel_git(path: Path) -> str:
    return str(path.relative_to(db.PROJECT_ROOT)).replace("/", "\\")


def validate_inputs(apply_summary: dict[str, Any], post_summary: dict[str, Any], snapshot_dir: Path) -> list[str]:
    notes: list[str] = []
    expected_apply = {
        "applied_count": 12,
        "files_changed_count": 9,
        "snapshot_count": 9,
        "rollback_executed": False,
        "post_apply_validation_ok": True,
    }
    for key, expected in expected_apply.items():
        if apply_summary.get(key) != expected:
            notes.append(f"apply summary mismatch: {key} expected {expected!r}, got {apply_summary.get(key)!r}")
    expected_post = {
        "validated_count": 12,
        "post_validation_fail_count": 0,
        "manual_audit_required_count": 0,
        "rollback_executed": False,
    }
    for key, expected in expected_post.items():
        if post_summary.get(key) != expected:
            notes.append(f"post-validation summary mismatch: {key} expected {expected!r}, got {post_summary.get(key)!r}")
    if not snapshot_dir.exists() or not snapshot_dir.is_dir():
        notes.append("snapshot dir missing")
    return notes


def build_records(
    apply_rows: list[dict[str, Any]],
    post_rows: list[dict[str, Any]],
    snapshot_dir: Path,
    tracked_files: set[str],
    status_lines: list[str],
) -> list[dict[str, Any]]:
    candidate_counts = Counter(str(row["target_file"]) for row in apply_rows)
    ok_counts = Counter(str(row["target_file"]) for row in post_rows if row.get("post_validation_ok"))
    status_by_rel: dict[str, str] = {}
    for line in status_lines:
        if len(line) >= 4:
            status_by_rel[line[3:].replace("/", "\\")] = line
    records: list[dict[str, Any]] = []
    for target_name in sorted(candidate_counts):
        target = Path(target_name)
        rel = rel_git(target)
        snapshot = snapshot_dir / target.relative_to(db.PROJECT_ROOT)
        snapshot_exists = snapshot.exists()
        differs = snapshot_exists and target.exists() and not filecmp.cmp(target, snapshot, shallow=False)
        tracked = rel in tracked_files
        ignore_note = "" if tracked else git_check_ignore(target)
        notes: list[str] = []
        if not tracked:
            notes.append("target file is not tracked by git")
        if ignore_note:
            notes.append(f"git check-ignore: {ignore_note}")
        if not snapshot_exists:
            notes.append("snapshot missing")
        if not differs:
            notes.append("target does not differ from snapshot")
        records.append(
            {
                "target_file": str(target),
                "tracked_by_git": tracked,
                "git_status_entry": status_by_rel.get(rel, ""),
                "snapshot_exists": snapshot_exists,
                "differs_from_snapshot": bool(differs),
                "candidate_count_in_file": int(candidate_counts[target_name]),
                "post_validation_ok_count": int(ok_counts[target_name]),
                "notes": "; ".join(notes),
            }
        )
    return records


def write_outputs(records: list[dict[str, Any]], summary: dict[str, Any], status_source_output: list[str], status_output: list[str], status_source: list[str]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_post_apply_light_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    involved = [record["target_file"] for record in records]
    explanation = (
        "Os arquivos alvo diferem dos snapshots, mas nao aparecem no git status porque nao estao rastreados pelo Git"
        if summary["files_diffing_from_snapshot_count"] == summary["files_seen_in_post_validation"]
        and summary["tracked_changed_files_count"] == 0
        else "O estado precisa de auditoria adicional; veja os contadores do summary."
    )
    lines = [
        "post apply light diagnostic",
        "",
        "1. O apply protegido continua valido depois do diagnostico leve?",
        f"   {'Sim' if summary['recommended_next_prompt'] == 'chat_exec_post_apply_lifecycle_reindex_readiness_prompt.md' else 'Nao'}",
        "2. Quais arquivos alvo foram envolvidos?",
        *[f"   - {name}" for name in involved],
        "3. O que explica output_changed=true mas git status -- source output sem linhas?",
        f"   {explanation}",
        "4. Ha risco de perda/nao rastreio das alteracoes?",
        "   As alteracoes estao materialmente presentes e snapshots existem; o risco principal e operacional: output/ nao esta rastreado pelo Git nesta checagem.",
        "5. Devemos rodar lifecycle/reindex agora?",
        "   Ainda nao neste prompt.",
        "6. Qual proximo passo recomendado?",
        f"   {summary['recommended_next_prompt']}",
        "",
        "git status --short -- source output:",
        *(status_source_output or ["   <sem linhas>"]),
        "git status --short -- output:",
        *(status_output or ["   <sem linhas>"]),
        "git status --short -- source:",
        *(status_source or ["   <sem linhas>"]),
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply-summary-json", required=True)
    parser.add_argument("--apply-jsonl", required=True)
    parser.add_argument("--post-validation-summary-json", required=True)
    parser.add_argument("--post-validation-jsonl", required=True)
    parser.add_argument("--snapshot-dir", required=True)
    args = parser.parse_args()

    apply_summary = read_json(project_path(args.apply_summary_json))
    apply_rows = read_jsonl(project_path(args.apply_jsonl))
    post_summary = read_json(project_path(args.post_validation_summary_json))
    post_rows = read_jsonl(project_path(args.post_validation_jsonl))
    snapshot_dir = project_path(args.snapshot_dir)
    input_notes = validate_inputs(apply_summary, post_summary, snapshot_dir)

    status_source_output = git_status(["source", "output"])
    status_output = git_status(["output"])
    status_source = git_status(["source"])
    target_rel_paths = sorted({rel_git(Path(str(row["target_file"]))) for row in apply_rows})
    tracked_files = git_ls_files(target_rel_paths)
    records = build_records(apply_rows, post_rows, snapshot_dir, tracked_files, status_source_output)

    files_seen = len(records)
    files_diffing = sum(1 for record in records if record["differs_from_snapshot"])
    untracked_or_unreported = sum(1 for record in records if not record["tracked_by_git"] or not record["git_status_entry"])
    tracked_changed = sum(1 for record in records if record["tracked_by_git"] and record["git_status_entry"])
    consistent = (
        not input_notes
        and int(apply_summary.get("applied_count") or 0) == 12
        and int(post_summary.get("validated_count") or 0) == 12
        and files_seen == 9
        and files_diffing == 9
        and int(post_summary.get("post_validation_fail_count") or 0) == 0
        and int(post_summary.get("manual_audit_required_count") or 0) == 0
        and not bool(apply_summary.get("rollback_executed"))
    )
    summary = {
        "schema_version": 1,
        "source": "post_apply_light_diagnostic_v1",
        "applied_count": int(apply_summary.get("applied_count") or 0),
        "validated_count": int(post_summary.get("validated_count") or 0),
        "files_changed_count_from_apply": int(apply_summary.get("files_changed_count") or 0),
        "files_seen_in_post_validation": files_seen,
        "tracked_changed_files_count": tracked_changed,
        "untracked_or_unreported_target_files_count": untracked_or_unreported,
        "files_diffing_from_snapshot_count": files_diffing,
        "source_git_status_count": len(status_source),
        "output_git_status_count": len(status_output),
        "post_validation_fail_count": int(post_summary.get("post_validation_fail_count") or 0) + len(input_notes),
        "manual_audit_required_count": int(post_summary.get("manual_audit_required_count") or 0) + len(input_notes),
        "rollback_executed": bool(apply_summary.get("rollback_executed")),
        "ready_for_lifecycle_next": bool(consistent),
        "ready_for_reindex_next": bool(consistent),
        "production_full_recommended_now": False,
        "recommended_next_prompt": "chat_exec_post_apply_lifecycle_reindex_readiness_prompt.md"
        if consistent
        else "chat_exec_post_apply_file_state_audit_prompt.md",
        "notes": input_notes,
    }
    txt_path, jsonl_path, summary_path = write_outputs(records, summary, status_source_output, status_output, status_source)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"files_seen_in_post_validation={summary['files_seen_in_post_validation']}")
    print(f"files_diffing_from_snapshot_count={summary['files_diffing_from_snapshot_count']}")
    print(f"tracked_changed_files_count={summary['tracked_changed_files_count']}")
    print(f"recommended_next_prompt={summary['recommended_next_prompt']}")


if __name__ == "__main__":
    main()
