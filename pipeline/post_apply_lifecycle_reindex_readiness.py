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


def git_status_source_output() -> list[str]:
    result = subprocess.run(
        ["git", "status", "--short", "--", "source", "output"],
        cwd=db.PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def validate_inputs(apply_summary: dict[str, Any], post_summary: dict[str, Any], light_summary: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    expected = {
        "applied_count": (apply_summary, 12),
        "validated_count": (post_summary, 12),
        "post_validation_fail_count": (post_summary, 0),
        "manual_audit_required_count": (post_summary, 0),
        "files_diffing_from_snapshot_count": (light_summary, 9),
        "rollback_executed": (apply_summary, False),
        "source_changed": (post_summary, False),
        "output_changed": (post_summary, True),
    }
    for key, (source, expected_value) in expected.items():
        if source.get(key) != expected_value:
            notes.append(f"{key} expected {expected_value!r}, got {source.get(key)!r}")
    return notes


def build_readiness_rows(input_ok: bool, git_reports_output: bool, output_tracking_decision: bool) -> list[dict[str, Any]]:
    return [
        {
            "readiness_key": "ready_for_lifecycle_planning",
            "ready": bool(input_ok),
            "risk": "low" if input_ok else "high",
            "reason": "Apply, post-validation and light diagnostic are consistent." if input_ok else "Input validation failed.",
            "recommended_next_step": "Plan lifecycle commands and guards; do not execute yet.",
        },
        {
            "readiness_key": "ready_for_reindex_planning",
            "ready": bool(input_ok),
            "risk": "medium" if output_tracking_decision else "low",
            "reason": "Output files are materially changed and validated against snapshots; Git does not report tracked output changes.",
            "recommended_next_step": "Plan reindex only after deciding how to preserve/report untracked output changes.",
        },
        {
            "readiness_key": "ready_for_segment_state_update_planning",
            "ready": bool(input_ok),
            "risk": "medium",
            "reason": "Planning is reasonable, but segment-state execution must wait for an explicit later prompt.",
            "recommended_next_step": "Prepare guard plan; do not update segment-state now.",
        },
        {
            "readiness_key": "ready_for_full_production",
            "ready": False,
            "risk": "high",
            "reason": "Only 12 protected candidates were applied; production full remains out of scope.",
            "recommended_next_step": "Keep production full blocked.",
        },
        {
            "readiness_key": "requires_manual_file_tracking_review",
            "ready": bool(not git_reports_output),
            "risk": "medium" if not git_reports_output else "low",
            "reason": "Git status does not show output changes, so artifact preservation must rely on reports/snapshots unless tracking is decided.",
            "recommended_next_step": "Review whether output/ is intentionally untracked or should be captured through a separate artifact path.",
        },
        {
            "readiness_key": "requires_output_tracking_decision",
            "ready": bool(output_tracking_decision),
            "risk": "medium" if output_tracking_decision else "low",
            "reason": "Validated changes exist materially, but Git does not report them under source/output.",
            "recommended_next_step": "Decide tracking/preservation policy before executing lifecycle/reindex.",
        },
    ]


def write_outputs(rows: list[dict[str, Any]], summary: dict[str, Any], git_lines: list[str]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_post_apply_lifecycle_reindex_readiness"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "post apply lifecycle/reindex readiness",
        "",
        "1. O apply esta pronto para planejar lifecycle/reindex?",
        f"   {'Sim' if summary['ready_for_lifecycle_planning'] and summary['ready_for_reindex_planning'] else 'Nao'}.",
        "2. O que fazer com o fato de output/ nao aparecer no Git?",
        "   Tratar como decisao operacional pendente: as mudancas existem e foram validadas, mas precisam de politica de preservacao/rastreamento antes de qualquer execucao destrutiva ou ampla.",
        "3. Devemos rodar lifecycle/reindex agora?",
        "   Nao neste prompt.",
        "4. Qual proximo prompt recomendado?",
        f"   {summary['recommended_next_prompt']}",
        "5. Producao full faz sentido agora?",
        "   Nao.",
        "",
        "git status --short -- source output:",
        *(git_lines or ["   <sem linhas>"]),
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply-summary-json", required=True)
    parser.add_argument("--post-validation-summary-json", required=True)
    parser.add_argument("--light-diagnostic-summary-json", required=True)
    parser.add_argument("--light-diagnostic-jsonl", required=True)
    args = parser.parse_args()

    apply_summary = read_json(project_path(args.apply_summary_json))
    post_summary = read_json(project_path(args.post_validation_summary_json))
    light_summary = read_json(project_path(args.light_diagnostic_summary_json))
    light_rows = read_jsonl(project_path(args.light_diagnostic_jsonl))
    input_notes = validate_inputs(apply_summary, post_summary, light_summary)
    git_lines = git_status_source_output()
    git_reports_output = any(line[3:].replace("\\", "/").startswith("output/") or line[3:] == "output" for line in git_lines if len(line) >= 4)
    output_tracking_decision = bool(post_summary.get("output_changed")) and not git_reports_output
    input_ok = not input_notes and len(light_rows) == 9
    rows = build_readiness_rows(input_ok, git_reports_output, output_tracking_decision)
    row_by_key = {row["readiness_key"]: row for row in rows}
    recommended_next = (
        "chat_exec_post_apply_lifecycle_reindex_plan_prompt.md"
        if input_ok
        else "chat_exec_output_tracking_decision_prompt.md"
    )
    summary = {
        "schema_version": 1,
        "source": "post_apply_lifecycle_reindex_readiness_v1",
        "applied_count": int(apply_summary.get("applied_count") or 0),
        "validated_count": int(post_summary.get("validated_count") or 0),
        "post_validation_fail_count": int(post_summary.get("post_validation_fail_count") or 0) + len(input_notes),
        "manual_audit_required_count": int(post_summary.get("manual_audit_required_count") or 0) + len(input_notes),
        "files_diffing_from_snapshot_count": int(light_summary.get("files_diffing_from_snapshot_count") or 0),
        "source_changed": bool(post_summary.get("source_changed")),
        "output_changed": bool(post_summary.get("output_changed")),
        "git_status_reports_output_changes": git_reports_output,
        "ready_for_lifecycle_planning": bool(row_by_key["ready_for_lifecycle_planning"]["ready"]),
        "ready_for_reindex_planning": bool(row_by_key["ready_for_reindex_planning"]["ready"]),
        "ready_for_segment_state_update_planning": bool(row_by_key["ready_for_segment_state_update_planning"]["ready"]),
        "ready_for_full_production": False,
        "requires_output_tracking_decision": output_tracking_decision,
        "recommended_next_prompt": recommended_next,
        "notes": input_notes,
    }
    txt_path, jsonl_path, summary_path = write_outputs(rows, summary, git_lines)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"ready_for_lifecycle_planning={summary['ready_for_lifecycle_planning']}")
    print(f"ready_for_reindex_planning={summary['ready_for_reindex_planning']}")
    print(f"requires_output_tracking_decision={summary['requires_output_tracking_decision']}")
    print(f"recommended_next_prompt={summary['recommended_next_prompt']}")


if __name__ == "__main__":
    main()
