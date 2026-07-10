from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_readiness_ui_tooltips_high_issue_triage_v1"
DIAGNOSTIC_JSONL = Path(
    "reports/20260702_160727_543826_release_readiness_ui_tooltips_reviewed_issue_ledger_diagnostic.jsonl"
)
HIGH_SEGMENT_IDS = {4184, 33224, 47194, 58495, 64929}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def triage_decision(row: dict[str, Any]) -> tuple[str, str]:
    segment_id = int(row["segment_id"])
    if segment_id == 4184:
        return "resolved_by_human_correction", "Spanish phrase 'se reduce un poco' was replaced by Portuguese text."
    if segment_id == 33224:
        return "false_positive_already_ok", "Current Portuguese text is acceptable; no Spanish residue remains."
    if segment_id == 47194:
        return "false_positive_already_ok", "Current Portuguese text is acceptable; #EMP markup is preserved."
    if segment_id == 58495:
        return "false_positive_already_ok", "'Una' is valid Portuguese imperative of unir here, not Spanish residue."
    if segment_id == 64929:
        return "resolved_by_human_correction", "Portuguese imperative 'Una' is valid and wording was improved from postura to posição."
    return "needs_more_context", "No triage rule available."


def build() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = [
        row
        for row in read_jsonl(DIAGNOSTIC_JSONL)
        if int(row.get("segment_id") or 0) in HIGH_SEGMENT_IDS
        and row.get("issue_family") == "spanish_residual_microagent"
        and row.get("issue_kind") == "spanish_residue"
        and str(row.get("issue_severity") or "").lower() == "high"
    ]
    if len(rows) != 5:
        raise SystemExit(f"expected 5 high issue rows, got {len(rows)}")
    records: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: int(item["segment_id"])):
        decision, rationale = triage_decision(row)
        records.append(
            {
                "source": SOURCE,
                "record_type": "high_issue_human_triage_readonly",
                "segment_id": int(row["segment_id"]),
                "issue_id": int(row["issue_id"]),
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "issue_family": row.get("issue_family"),
                "issue_kind": row.get("issue_kind"),
                "issue_severity": row.get("issue_severity"),
                "human_decision": row.get("human_decision"),
                "evidence_text": row.get("evidence_text"),
                "output_text": row.get("output_text"),
                "confirmed_text": row.get("confirmed_text"),
                "triage_decision": decision,
                "triage_rationale": rationale,
                "recommended_action": "allow_issue_closure_dry_run",
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_high_issue_triage",
        "input_jsonl": str(DIAGNOSTIC_JSONL),
        "record_count": len(records),
        "triage_decision_counts": dict(Counter(record["triage_decision"] for record in records).most_common()),
        "recommended_action_counts": dict(Counter(record["recommended_action"] for record in records).most_common()),
        "allow_issue_closure_issue_ids": [record["issue_id"] for record in records],
        "allow_issue_closure_segment_ids": [record["segment_id"] for record in records],
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": (
            "These 5 high spanish-residue issues look resolved or false-positive after human review. "
            "If approved, rerun issue-closure dry-run including the 32 medium issues and these 5 high-triaged issues."
        ),
    }
    return records, summary


def markdown(records: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines = [
        "# High Issue Triage: UI/tooltips Release Review",
        "",
        f"- Record count: {summary['record_count']}",
        "- Mode: read-only",
        "",
    ]
    for record in records:
        lines.extend(
            [
                f"## Segment {record['segment_id']} / Issue {record['issue_id']}",
                "",
                f"- Key: `{record['source_key']}`",
                f"- Decision: `{record['triage_decision']}`",
                f"- Rationale: {record['triage_rationale']}",
                "",
                "Evidence:",
                "```text",
                str(record.get("evidence_text") or ""),
                "```",
                "Output:",
                "```text",
                str(record.get("output_text") or ""),
                "```",
                "",
            ]
        )
    return "\n".join(lines)


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_high_issue_triage"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    txt_path = base.with_suffix(".txt")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {
        "markdown": str(md_path),
        "txt": str(txt_path),
        "jsonl": str(jsonl_path),
        "summary": str(summary_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(markdown(records, summary), encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                "Release readiness UI/tooltips high issue triage",
                f"record_count={summary['record_count']}",
                f"triage_decision_counts={json.dumps(summary['triage_decision_counts'], ensure_ascii=False, sort_keys=True)}",
                f"recommended_action_counts={json.dumps(summary['recommended_action_counts'], ensure_ascii=False, sort_keys=True)}",
                "candidate_generation_count=0",
                "apply_count=0",
                "lifecycle_count=0",
                "segment_state_count=0",
                "reindex_count=0",
                "production_full_count=0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return md_path, txt_path, jsonl_path, summary_path


def main() -> None:
    records, summary = build()
    md_path, txt_path, jsonl_path, summary_path = write_reports(records, summary)
    print(f"markdown={md_path}")
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"triage_decision_counts={summary['triage_decision_counts']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
