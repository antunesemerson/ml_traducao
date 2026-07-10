from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROL_PATH = ROOT / "memory" / "post_release_feedback_status.json"
REPORTS_DIR = ROOT / "reports"
BACKUPS_DIR = ROOT / "memory" / "backups"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def latest_validation_summary() -> Path:
    matches = sorted(
        REPORTS_DIR.glob("*_post_release_feedback_candidate_validation_readonly_summary.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise SystemExit("no post-release feedback validation summary found")
    return matches[0]


def sibling_jsonl(summary_path: Path) -> Path:
    name = summary_path.name.replace("_summary.json", ".jsonl")
    path = summary_path.with_name(name)
    if not path.exists():
        raise SystemExit(f"validation JSONL not found: {path}")
    return path


def recompute_summary(control: dict, *, previous_closed_total: int, closed_now_count: int) -> None:
    findings = control.get("findings") or []
    status_counts = Counter(finding.get("status") for finding in findings)
    accepted_holds = status_counts.get("hold", 0)
    approved_pending_apply = status_counts.get("approved_pending_apply", 0)
    applied_pending_validation = status_counts.get("applied_to_candidate", 0)
    open_findings = status_counts.get("open", 0)
    closed_findings = max(status_counts.get("closed", 0), previous_closed_total + closed_now_count)

    summary = control.setdefault("summary", {})
    summary["known_open_findings"] = open_findings
    summary["approved_pending_apply"] = approved_pending_apply
    summary["applied_pending_validation"] = applied_pending_validation
    summary["closed_findings"] = closed_findings
    summary["accepted_holds"] = accepted_holds
    summary["can_publish_hotfix"] = None
    summary["source_changed"] = False
    summary["output_changed_by_this_control_file"] = False

    queues = control.setdefault("queues", {})
    queues["open"] = open_findings
    queues["approved_pending_apply"] = approved_pending_apply
    queues["applied_pending_validation"] = applied_pending_validation
    queues["closed"] = closed_findings
    queues["hold"] = accepted_holds


def make_markdown(report: dict, updated: list[dict]) -> str:
    lines = [
        "# Post-release Feedback Closure Control",
        "",
        "Atualizacao de controle/metadata. Nenhum source, output, banco, lifecycle, segment-state, reindex ou producao full foi alterado.",
        "",
        "## Resumo",
        "",
        f"- Itens fechados nesta operacao: `{report['closed_now_count']}`",
        f"- Closed total: `{report['summary_after']['closed_findings']}`",
        f"- Pendentes de validacao: `{report['summary_after']['applied_pending_validation']}`",
        f"- Findings abertos: `{report['summary_after']['known_open_findings']}`",
        f"- Holds aceitos: `{report['summary_after']['accepted_holds']}`",
        f"- Validation summary: `{report['validation_summary']}`",
        "",
        "## Fechados",
        "",
    ]
    for item in updated:
        lines.extend(
            [
                f"- `{item['id']}` -> `{item['source_key']}`",
            ]
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validation-summary", type=Path, default=None)
    args = parser.parse_args()

    validation_summary_path = args.validation_summary or latest_validation_summary()
    if not validation_summary_path.is_absolute():
        validation_summary_path = ROOT / validation_summary_path
    validation = read_json(validation_summary_path)
    validation_rows = read_jsonl(sibling_jsonl(validation_summary_path))

    if not validation.get("read_only"):
        raise SystemExit("validation summary must be read_only")
    if int(validation.get("blocked_count") or 0) != 0:
        raise SystemExit("validation has blocked rows; refusing to close feedback")

    validated_ids = {
        row["id"]
        for row in validation_rows
        if row.get("validation_status") == "validated"
    }
    expected_count = int(validation.get("validated_count") or 0)
    if len(validated_ids) != expected_count:
        raise SystemExit("validated row count does not match validation summary")

    control = read_json(CONTROL_PATH)
    now = datetime.now().isoformat(timespec="seconds")
    validation_rel = str(validation_summary_path.relative_to(ROOT))
    updated: list[dict] = []

    for finding in control.get("findings") or []:
        if finding.get("id") not in validated_ids:
            continue
        if finding.get("status") != "applied_to_candidate":
            raise SystemExit(f"finding is not applied_to_candidate: {finding.get('id')}")
        finding["status"] = "closed"
        finding["current_candidate_status"] = "validated_in_stable_hotfix_candidate"
        finding["validation_status"] = "validated"
        finding["validated_at"] = now
        finding["validation_report"] = validation_rel
        finding["lock"] = finding.get("lock") or "post_release_visual_feedback"
        updated.append(
            {
                "id": finding.get("id"),
                "source_key": finding.get("source_key"),
                "segment_id": finding.get("segment_id"),
                "area": finding.get("area"),
            }
        )

    if len(updated) != expected_count:
        raise SystemExit(f"updated {len(updated)} findings, expected {expected_count}")

    control["generated_at"] = now
    previous_closed_total = int((control.get("summary") or {}).get("closed_findings") or 0)
    recompute_summary(
        control,
        previous_closed_total=previous_closed_total,
        closed_now_count=len(updated),
    )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUPS_DIR / f"{stamp}_post_release_feedback_status_before_validated_close.json"
    shutil.copy2(CONTROL_PATH, backup_path)

    CONTROL_PATH.write_text(json.dumps(control, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    report = {
        "schema_version": 1,
        "source": "post_release_feedback_close_validated",
        "generated_at": now,
        "closed_now_count": len(updated),
        "updated_findings": updated,
        "validation_summary": validation_rel,
        "control_path": str(CONTROL_PATH.relative_to(ROOT)),
        "backup_path": str(backup_path.relative_to(ROOT)),
        "guards": {
            "source_changed": False,
            "output_changed": False,
            "database_changed": False,
            "segment_state": 0,
            "reindex": 0,
            "production_full": 0,
        },
        "summary_after": control["summary"],
        "queues_after": control["queues"],
    }

    base = REPORTS_DIR / f"{stamp}_post_release_feedback_close_validated"
    summary_path = base.with_name(base.name + "_summary.json")
    markdown_path = base.with_suffix(".md")
    summary_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(make_markdown(report, updated), encoding="utf-8")

    print(json.dumps({"summary": str(summary_path), "markdown": str(markdown_path), **report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
