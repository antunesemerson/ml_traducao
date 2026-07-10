from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_policy_item_absent_diagnostic_v1"
INPUT_JSONL = Path("reports/20260701_212614_208422_domain_policy_vote_candidate_mapped_action_segment_state_delta_533_535.jsonl")
SOURCE_DIAGNOSTIC_JSONL = Path("reports/20260701_203003_043617_domain_policy_vote_candidate_mapped_action_guard_missing_diagnostic_91.jsonl")
EXPECTED_COUNT = 9
SAMPLE_LIMIT = 8


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only diagnostic for policy_item_absent rows after mapped-action segment-state delta.")
    parser.add_argument("--input-jsonl", type=Path, default=INPUT_JSONL)
    parser.add_argument("--source-diagnostic-jsonl", type=Path, default=SOURCE_DIAGNOSTIC_JSONL)
    parser.add_argument("--expected-count", type=int, default=EXPECTED_COUNT)
    return parser.parse_args()


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


def recommended_policy_family(action: str) -> str:
    if action == "close_reopen_boundary_token_subpolicy_controlled":
        return "missing_boundary_token_subpolicy_controlled_policy_item"
    if action == "close_reopen_same_token_boundary_repair_controlled":
        return "missing_same_token_boundary_repair_controlled_policy_item"
    if "close_reopen_boundary_token_subpolicy_controlled" in action and "close_reopen_same_token_boundary_repair_controlled" in action:
        return "missing_split_mixed_boundary_and_same_token_policy_items"
    return "missing_unknown_policy_item_family"


def build(input_jsonl: Path, source_diagnostic_jsonl: Path, expected_count: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    delta_rows = [row for row in read_jsonl(input_jsonl) if row.get("problem_class") == "policy_item_absent"]
    source_rows = {int(row["segment_id"]): row for row in read_jsonl(source_diagnostic_jsonl)}
    if len(delta_rows) != expected_count:
        raise SystemExit(f"policy_item_absent count guard failed: {len(delta_rows)}")
    records: list[dict[str, Any]] = []
    for row in delta_rows:
        source = source_rows[int(row["segment_id"])]
        action = str(source.get("lifecycle_policy_action") or row.get("lifecycle_policy_action") or "")
        records.append(
            {
                "source": SOURCE,
                "segment_id": int(row["segment_id"]),
                "relative_path": source.get("relative_path"),
                "source_key": source.get("source_key"),
                "problem_class": "policy_item_absent",
                "current_final_state": row.get("after_final_state"),
                "lifecycle_policy_action": action,
                "mapped_lifecycle_actions": source.get("mapped_lifecycle_actions") or [],
                "token_surface": source.get("token_surface"),
                "policy_item_count": int(source.get("policy_item_count") or 0),
                "policy_item_status": source.get("policy_item_status"),
                "confirmed_matches_output": int(source.get("confirmed_matches_output") or 0),
                "needs_output_apply": int(source.get("needs_output_apply") or 0),
                "open_issue_count": int(source.get("open_issue_count") or 0),
                "high_issue_count": int(source.get("high_issue_count") or 0),
                "recommended_policy_family": recommended_policy_family(action),
                "recommended_next_step": "architecture_confirm_missing_policy_item_materialization_before_any_lifecycle",
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )
    records.sort(key=lambda row: (row["recommended_policy_family"], row["segment_id"]))
    action_counts = Counter(row["lifecycle_policy_action"] for row in records)
    family_counts = Counter(row["recommended_policy_family"] for row in records)
    token_counts = Counter(row["token_surface"] for row in records)
    state_counts = Counter(row["current_final_state"] for row in records)
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        if len(samples[row["recommended_policy_family"]]) < SAMPLE_LIMIT:
            samples[row["recommended_policy_family"]].append(row)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_policy_item_absent_diagnostic",
        "input_jsonl": str(input_jsonl),
        "source_diagnostic_jsonl": str(source_diagnostic_jsonl),
        "record_count": len(records),
        "expected_record_count": expected_count,
        "lifecycle_policy_action_counts": dict(action_counts.most_common()),
        "recommended_policy_family_counts": dict(family_counts.most_common()),
        "token_surface_counts": dict(token_counts.most_common()),
        "current_final_state_counts": dict(state_counts.most_common()),
        "confirmed_matches_output_1_count": sum(1 for row in records if row["confirmed_matches_output"] == 1),
        "needs_output_apply_0_count": sum(1 for row in records if row["needs_output_apply"] == 0),
        "open_high_issue_0_count": sum(1 for row in records if row["open_issue_count"] == 0 and row["high_issue_count"] == 0),
        "samples_by_policy_family": samples,
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
            "Ask architecture whether to materialize missing policy items for these 9 boundary/same-token controlled actions. "
            "Do not run lifecycle until the policy items exist and are reviewed."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_policy_item_absent_diagnostic_9"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Policy item absent diagnostic",
        f"record_count={summary['record_count']}",
        "",
        "Recommended policy families:",
    ]
    for key, count in summary["recommended_policy_family_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Lifecycle actions:"])
    for key, count in summary["lifecycle_policy_action_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(
        [
            "",
            "Guards:",
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
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records, summary = build(args.input_jsonl, args.source_diagnostic_jsonl, args.expected_count)
    txt, jsonl, summary_path = write_reports(records, summary)
    print(f"txt={txt}")
    print(f"jsonl={jsonl}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
