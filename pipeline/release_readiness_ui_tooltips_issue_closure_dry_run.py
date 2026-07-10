from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_readiness_ui_tooltips_issue_closure_dry_run_v1"
ISSUE_DIAGNOSTIC_JSONL = Path(
    "reports/20260702_160727_543826_release_readiness_ui_tooltips_reviewed_issue_ledger_diagnostic.jsonl"
)
ISSUE_DIAGNOSTIC_SUMMARY = Path(
    "reports/20260702_160727_543826_release_readiness_ui_tooltips_reviewed_issue_ledger_diagnostic_summary.json"
)
HIGH_TRIAGE_JSONL = Path(
    "reports/20260702_162428_682719_release_readiness_ui_tooltips_high_issue_triage.jsonl"
)
EXPECTED_SEGMENT_COUNT = 17
EXPECTED_ISSUE_COUNT = 37


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only issue-closure dry-run for reviewed UI/tooltips issues.")
    parser.add_argument("--diagnostic-jsonl", type=Path, default=ISSUE_DIAGNOSTIC_JSONL)
    parser.add_argument("--diagnostic-summary", type=Path, default=ISSUE_DIAGNOSTIC_SUMMARY)
    parser.add_argument("--high-triage-jsonl", type=Path, default=HIGH_TRIAGE_JSONL)
    parser.add_argument("--expected-segment-count", type=int, default=EXPECTED_SEGMENT_COUNT)
    parser.add_argument("--expected-issue-count", type=int, default=EXPECTED_ISSUE_COUNT)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(db.project_path(path).read_text(encoding="utf-8"))


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


def evaluate(row: dict[str, Any], high_triaged_issue_ids: set[int]) -> dict[str, Any]:
    reasons: list[str] = []
    if row.get("record_type") != "reviewed_segment_open_issue_reassessment":
        reasons.append("not_issue_reassessment_row")
    if row.get("reassessment") != "likely_resolved_by_human_review":
        reasons.append("not_likely_resolved_by_human_review")
    if row.get("recommended_action") != "candidate_for_issue_closure_dry_run":
        reasons.append("not_candidate_for_issue_closure")
    issue_id = int(row["issue_id"])
    if str(row.get("issue_severity") or "").lower() in {"high", "critical", "error"} and issue_id not in high_triaged_issue_ids:
        reasons.append("high_issue_requires_manual_triage")
    if row.get("human_decision") == "hold_parser_later":
        reasons.append("hold_parser_later")
    if int(row.get("confirmed_matches_output") or 0) != 1:
        reasons.append("confirmed_matches_output_not_1")
    if int(row.get("needs_output_apply") or 0) != 0:
        reasons.append("needs_output_apply_not_0")
    if str(row.get("final_state") or "") != "reopen_auto_confirmed_autofix":
        reasons.append("unexpected_final_state")
    released = not reasons
    return {
        "source": SOURCE,
        "record_type": "issue_closure_dry_run_item",
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "issue_id": int(row["issue_id"]),
        "issue_run_id": row.get("issue_run_id"),
        "issue_family": row.get("issue_family"),
        "issue_kind": row.get("issue_kind"),
        "issue_severity": row.get("issue_severity"),
        "issue_status": row.get("issue_status"),
        "agent_key": row.get("agent_key"),
        "human_decision": row.get("human_decision"),
        "confirmation_label_after": row.get("confirmation_label_after"),
        "final_state": row.get("final_state"),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "dry_run_decision": "released" if released else "blocked",
        "block_reasons": reasons,
        "proposed_status_after": "closed_resolved_by_human_release_review" if released else row.get("issue_status"),
        "proposed_closure_reason": (
            "human_release_review_confirmed_output_and_confirmation_synchronized" if released else None
        ),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
    }


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    summary_in = read_json(args.diagnostic_summary)
    rows = read_jsonl(args.diagnostic_jsonl)
    high_triage_rows = read_jsonl(args.high_triage_jsonl)
    high_triaged_issue_ids = {
        int(row["issue_id"])
        for row in high_triage_rows
        if row.get("recommended_action") == "allow_issue_closure_dry_run"
    }
    candidate_segment_ids = set(int(value) for value in summary_in.get("candidate_for_issue_closure_segment_ids") or [])
    total_likely_resolved = [
        row
        for row in rows
        if row.get("reassessment") == "likely_resolved_by_human_review"
        and row.get("recommended_action") == "candidate_for_issue_closure_dry_run"
    ]
    focus = [
        row
        for row in rows
        if int(row.get("segment_id") or 0) in candidate_segment_ids
        and row.get("reassessment") == "likely_resolved_by_human_review"
        and row.get("recommended_action") == "candidate_for_issue_closure_dry_run"
    ]
    records = [evaluate(row, high_triaged_issue_ids) for row in focus]
    segment_ids = sorted({int(record["segment_id"]) for record in records})
    if len(segment_ids) != args.expected_segment_count:
        raise SystemExit(f"segment count guard failed: {len(segment_ids)} != {args.expected_segment_count}")
    if len(records) != args.expected_issue_count:
        raise SystemExit(f"issue count guard failed: {len(records)} != {args.expected_issue_count}")

    decisions = Counter(record["dry_run_decision"] for record in records)
    block_counts = Counter(reason for record in records for reason in record["block_reasons"])
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        key = record["dry_run_decision"] if not record["block_reasons"] else ",".join(record["block_reasons"])
        if len(samples[key]) < 5:
            samples[key].append(record)

    released_records = [record for record in records if record["dry_run_decision"] == "released"]
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_issue_closure_dry_run",
        "diagnostic_jsonl": str(args.diagnostic_jsonl),
        "diagnostic_summary": str(args.diagnostic_summary),
        "high_triage_jsonl": str(args.high_triage_jsonl),
        "high_triaged_issue_count": len(high_triaged_issue_ids),
        "record_count": len(records),
        "total_likely_resolved_issue_count": len(total_likely_resolved),
        "excluded_mixed_segment_likely_resolved_issue_count": len(total_likely_resolved) - len(records),
        "segment_count": len(segment_ids),
        "released_count": decisions.get("released", 0),
        "blocked_count": decisions.get("blocked", 0),
        "released_segment_count": len({int(record["segment_id"]) for record in released_records}),
        "blocked_segment_count": len({int(record["segment_id"]) for record in records if record["dry_run_decision"] == "blocked"}),
        "issue_family_counts": dict(Counter(str(record["issue_family"]) for record in records).most_common(50)),
        "issue_kind_counts": dict(Counter(str(record["issue_kind"]) for record in records).most_common(50)),
        "issue_severity_counts": dict(Counter(str(record["issue_severity"]) for record in records).most_common()),
        "released_issue_family_counts": dict(
            Counter(str(record["issue_family"]) for record in released_records).most_common(50)
        ),
        "block_reason_counts": dict(block_counts.most_common()),
        "released_segment_ids": sorted({int(record["segment_id"]) for record in released_records}),
        "released_issue_ids": sorted(int(record["issue_id"]) for record in released_records),
        "samples_by_decision": dict(samples),
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
            "If released_count equals record_count and the user approves, run a separate protected issue-ledger closure materializer for these exact issue_ids only. "
            "Do not combine with lifecycle, segment-state, output apply or reindex."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_release_readiness_ui_tooltips_issue_closure_dry_run"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                "Release readiness UI/tooltips issue closure dry-run",
                f"record_count={summary['record_count']}",
                f"segment_count={summary['segment_count']}",
                f"released_count={summary['released_count']}",
                f"blocked_count={summary['blocked_count']}",
                f"issue_family_counts={json.dumps(summary['issue_family_counts'], ensure_ascii=False, sort_keys=True)}",
                f"issue_severity_counts={json.dumps(summary['issue_severity_counts'], ensure_ascii=False, sort_keys=True)}",
                f"block_reason_counts={json.dumps(summary['block_reason_counts'], ensure_ascii=False, sort_keys=True)}",
                "candidate_generation_count=0",
                "apply_count=0",
                "lifecycle_count=0",
                "segment_state_count=0",
                "reindex_count=0",
                "production_full_count=0",
                "",
                "Recommendation:",
                summary["single_operational_recommendation"],
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records, summary = build(args)
    txt_path, jsonl_path, summary_path = write_reports(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"segment_count={summary['segment_count']}")
    print(f"released_count={summary['released_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
