from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROL_PATH = ROOT / "memory" / "post_release_feedback_status.json"
REPORTS_DIR = ROOT / "reports"
OUTPUT_PATH = ROOT / "output" / "spanish"


CHECKS = {
    "qa-visual-002-ai-personality-vengefulness-duplicate": {
        "absent": ["Ressentido [Select_CString", "'resentida'", "'resentido'"],
        "present": ["[Select_CString", "'Ressentida'", "'Ressentido'"],
    },
    "qa-visual-003-event-other-person-leading-exclamation": {
        "absent": ["![lover_spouse.Custom"],
        "present": ["[lover_spouse.Custom('ES_ElElla')|U]"],
    },
    "qa-visual-004-ai-personality-honor-label": {
        "absent": ["deshonrosa", "deshonroso", "Desonrado [Select_CString"],
        "present": ["[Select_CString", "'Desonrada'", "'Desonrado'"],
    },
    "qa-visual-005-ai-personality-villain-label": {
        "absent": ["Villana", "Villano", "Vilão [Select_CString"],
        "present": ["[Select_CString", "'Vilã'", "'Vilão'"],
    },
    "qa-visual-006-ai-personality-honor-explanation": {
        "absent": ["muy deshonroso"],
        "present": ["muito desonrado"],
    },
}


def read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(errors="replace")


def load_json(path: Path) -> dict:
    return json.loads(read_text(path))


def find_entry(root: Path, source_key: str) -> dict:
    if not root.exists():
        return {"found": False, "error": f"root_missing:{root}"}

    key_pattern = re.compile(rf"^\s*{re.escape(source_key)}\s*:\s*(.*)$")
    for path in root.rglob("*.yml"):
        text = read_text(path)
        for line_no, line in enumerate(text.splitlines(), 1):
            match = key_pattern.match(line.lstrip("\ufeff"))
            if not match:
                continue
            raw_value = match.group(1).strip()
            value = raw_value
            if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                value = value[1:-1]
            return {
                "found": True,
                "relative_path": str(path.relative_to(root)),
                "line_no": line_no,
                "raw_line": line,
                "value": value,
            }
    return {"found": False, "error": f"source_key_not_found:{source_key}"}


def validate_text(finding_id: str, candidate_value: str) -> tuple[bool, list[str]]:
    check = CHECKS.get(finding_id, {"absent": [], "present": []})
    failures: list[str] = []

    for token in check["absent"]:
        if token in candidate_value:
            failures.append(f"unexpected_token_present:{token}")

    for token in check["present"]:
        if token not in candidate_value:
            failures.append(f"expected_token_missing:{token}")

    return not failures, failures


def make_markdown(summary: dict, records: list[dict]) -> str:
    lines = [
        "# Post-release Feedback Candidate Validation",
        "",
        "Modo: read-only. Nenhum arquivo de source/output, banco, lifecycle ou segment-state foi alterado.",
        "",
        "## Resumo",
        "",
        f"- Candidato: `{summary['candidate_path']}`",
        f"- Baseline: `{summary['baseline_path']}`",
        f"- Itens avaliados: `{summary['checked_count']}`",
        f"- Validados: `{summary['validated_count']}`",
        f"- Bloqueados: `{summary['blocked_count']}`",
        f"- Diferem da baseline: `{summary['changed_from_baseline_count']}`",
        f"- Diferem do output atual: `{summary['changed_from_current_output_count']}`",
        "",
        "## Itens",
        "",
    ]

    for record in records:
        lines.extend(
            [
                f"### {record['id']}",
                "",
                f"- Status de validação: `{record['validation_status']}`",
                f"- Segmento/chave: `{record.get('segment_id') or 'n/a'}` / `{record['source_key']}`",
                f"- Arquivo: `{record.get('relative_path') or 'n/a'}`",
                f"- Baseline: `{record.get('baseline_value') or 'n/a'}`",
                f"- Candidato: `{record.get('candidate_value') or 'n/a'}`",
                f"- Output atual: `{record.get('current_output_value') or 'n/a'}`",
                f"- Checks: `{', '.join(record['checks']) if record['checks'] else 'ok'}`",
                "",
            ]
        )

    return "\n".join(lines)


def main() -> int:
    control = load_json(CONTROL_PATH)
    baseline_path = ROOT / control["baseline_control"]["stable_baseline_path"]
    candidate_path = ROOT / control["baseline_control"]["current_candidate_path"]

    records = []
    for finding in control["findings"]:
        if finding.get("status") != "applied_to_candidate":
            continue

        source_key = finding["source_key"]
        candidate_entry = find_entry(candidate_path, source_key)
        baseline_entry = find_entry(baseline_path, source_key)
        output_entry = find_entry(OUTPUT_PATH, source_key)

        candidate_value = candidate_entry.get("value", "")
        baseline_value = baseline_entry.get("value", "")
        current_output_value = output_entry.get("value", "")

        text_ok = bool(candidate_entry.get("found")) and bool(baseline_entry.get("found"))
        check_ok, check_failures = validate_text(finding["id"], candidate_value)
        changed_from_baseline = text_ok and candidate_value != baseline_value
        changed_from_current_output = (
            bool(output_entry.get("found")) and candidate_value != current_output_value
        )

        checks = []
        if not candidate_entry.get("found"):
            checks.append(candidate_entry.get("error", "candidate_missing"))
        if not baseline_entry.get("found"):
            checks.append(baseline_entry.get("error", "baseline_missing"))
        if not changed_from_baseline:
            checks.append("candidate_not_changed_from_baseline")
        checks.extend(check_failures)

        validation_status = "validated" if text_ok and check_ok and changed_from_baseline else "blocked"
        records.append(
            {
                "id": finding["id"],
                "source_key": source_key,
                "segment_id": finding.get("segment_id"),
                "area": finding.get("area"),
                "severity": finding.get("severity"),
                "evidence": finding.get("evidence"),
                "decision": finding.get("decision"),
                "relative_path": candidate_entry.get("relative_path") or baseline_entry.get("relative_path"),
                "candidate_line_no": candidate_entry.get("line_no"),
                "baseline_line_no": baseline_entry.get("line_no"),
                "candidate_value": candidate_value,
                "baseline_value": baseline_value,
                "current_output_value": current_output_value,
                "changed_from_baseline": changed_from_baseline,
                "changed_from_current_output": changed_from_current_output,
                "validation_status": validation_status,
                "checks": checks,
            }
        )

    now = datetime.now()
    stamp = now.strftime("%Y%m%d_%H%M%S_%f")
    summary = {
        "schema_version": 1,
        "source": "post_release_feedback_candidate_validation_readonly",
        "generated_at": now.isoformat(timespec="seconds"),
        "read_only": True,
        "apply": 0,
        "ingest": 0,
        "issue_closure": 0,
        "lifecycle_materializer": 0,
        "segment_state": 0,
        "reindex": 0,
        "production_full": 0,
        "source_changed": False,
        "output_changed": False,
        "control_path": str(CONTROL_PATH.relative_to(ROOT)),
        "candidate_path": str(candidate_path.relative_to(ROOT)),
        "baseline_path": str(baseline_path.relative_to(ROOT)),
        "checked_count": len(records),
        "validated_count": sum(1 for record in records if record["validation_status"] == "validated"),
        "blocked_count": sum(1 for record in records if record["validation_status"] == "blocked"),
        "changed_from_baseline_count": sum(1 for record in records if record["changed_from_baseline"]),
        "changed_from_current_output_count": sum(
            1 for record in records if record["changed_from_current_output"]
        ),
        "ready_to_close_feedback_count": sum(
            1 for record in records if record["validation_status"] == "validated"
        ),
    }

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    base = REPORTS_DIR / f"{stamp}_post_release_feedback_candidate_validation_readonly"
    summary_path = base.with_name(base.name + "_summary.json")
    jsonl_path = base.with_suffix(".jsonl")
    markdown_path = base.with_suffix(".md")

    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    markdown_path.write_text(make_markdown(summary, records), encoding="utf-8")

    print(json.dumps({"summary": str(summary_path), "jsonl": str(jsonl_path), "markdown": str(markdown_path), **summary}, ensure_ascii=False, indent=2))
    return 0 if summary["blocked_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
