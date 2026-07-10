from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_mapped_action_guard_missing_diagnostic_v1"
INPUT_JSONL = Path("reports/20260701_202847_809944_domain_policy_vote_candidate_lifecycle_policy_consumption_debug_371.jsonl")
INPUT_SUMMARY = Path("reports/20260701_202847_809944_domain_policy_vote_candidate_lifecycle_policy_consumption_debug_371_summary.json")
EXPECTED_COUNT = 91
ROOT_CAUSE = "mapped_action_but_fast_path_or_state_guard_missing"
SAMPLE_LIMIT = 8


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only diagnostic for mapped lifecycle actions not consumed by fast path/state guard.")
    parser.add_argument("--input-jsonl", type=Path, default=INPUT_JSONL)
    parser.add_argument("--input-summary", type=Path, default=INPUT_SUMMARY)
    parser.add_argument("--expected-count", type=int, default=EXPECTED_COUNT)
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


def expected_final_state(row: dict[str, Any]) -> str:
    from_state = str(row.get("from_final_state") or "")
    if from_state.startswith("closed_"):
        return from_state
    action = str(row.get("lifecycle_policy_action") or "")
    if action:
        return f"closed_auto_confirmed_{action}"
    return "unknown_expected_closed_state"


def policy_item_status(row: dict[str, Any]) -> str:
    count = int(row.get("policy_item_count") or 0)
    allowed = int(row.get("lifecycle_policy_allowed") or 0)
    action = str(row.get("lifecycle_policy_action") or "")
    if count > 0 and allowed == 1:
        return "policy_item_present_and_allowed"
    if count == 0 and action:
        return "inherited_lifecycle_action_without_materialized_policy_item"
    if count > 0 and allowed != 1:
        return "policy_item_present_but_not_allowed_current_state"
    return "missing_policy_item_and_action"


def problem_class(row: dict[str, Any]) -> str:
    status = policy_item_status(row)
    mapped = row.get("mapped_lifecycle_actions") or []
    if status == "policy_item_present_and_allowed" and mapped:
        return "fast_path_does_not_consume_existing_policy_item"
    if status == "inherited_lifecycle_action_without_materialized_policy_item":
        return "policy_item_absent"
    if mapped:
        return "action_mapped_but_consumer_specific_missing"
    return "hold_structural_despite_issue_0"


def guard_ok(row: dict[str, Any]) -> bool:
    return (
        int(row.get("confirmed_matches_output") or 0) == 1
        and int(row.get("needs_output_apply") or 0) == 0
        and int(row.get("open_issue_count") or 0) == 0
        and int(row.get("high_issue_count") or 0) == 0
    )


def compact(row: dict[str, Any]) -> dict[str, Any]:
    items = row.get("policy_items") or []
    return {
        "source": SOURCE,
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "base_run_id": row.get("base_run_id"),
        "current_run_id": row.get("current_run_id"),
        "from_final_state": row.get("from_final_state"),
        "expected_final_state": expected_final_state(row),
        "current_final_state": row.get("current_final_state"),
        "current_state_group": row.get("current_state_group"),
        "lifecycle_policy_action": row.get("lifecycle_policy_action"),
        "mapped_lifecycle_actions": row.get("mapped_lifecycle_actions") or [],
        "unmapped_lifecycle_actions": row.get("unmapped_lifecycle_actions") or [],
        "lifecycle_policy_allowed": int(row.get("lifecycle_policy_allowed") or 0),
        "policy_item_count": int(row.get("policy_item_count") or 0),
        "policy_item_status": policy_item_status(row),
        "policy_run_ids": sorted({int(item.get("policy_run_id")) for item in items if item.get("policy_run_id")}),
        "policy_names": sorted({str(item.get("policy_name")) for item in items if item.get("policy_name")}),
        "policy_allowed_count": sum(1 for item in items if int(item.get("policy_allowed") or 0) == 1),
        "policy_item_actions": sorted({str(item.get("policy_action")) for item in items if item.get("policy_action")}),
        "token_surface": row.get("token_surface") or "unknown",
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "high_issue_count": int(row.get("high_issue_count") or 0),
        "guard_ok": guard_ok(row),
        "root_cause": row.get("root_cause"),
        "problem_class": problem_class(row),
        "recommendation": recommendation(problem_class(row)),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
    }


def recommendation(problem: str) -> str:
    if problem == "fast_path_does_not_consume_existing_policy_item":
        return "architecture_debug_fast_path_or_state_guard_consumption_for_existing_policy_item"
    if problem == "policy_item_absent":
        return "materialize_missing_policy_item_only_after_architecture_confirms_action_family"
    if problem == "action_mapped_but_consumer_specific_missing":
        return "architecture_add_specific_consumer_or_state_mapping_for_action"
    return "hold_structural_despite_clean_issue_surface"


def build(input_jsonl: Path, input_summary: Path, expected_count: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    summary_in = read_json(input_summary)
    if int(summary_in.get("record_count") or 0) != 371:
        raise SystemExit("input 371 summary guard failed")
    for key in ("candidate_generation_count", "apply_count", "lifecycle_count", "segment_state_count", "reindex_count", "production_full_count"):
        if int(summary_in.get(key) or 0) != 0:
            raise SystemExit(f"input summary {key} guard failed")
    rows = [row for row in read_jsonl(input_jsonl) if row.get("root_cause") == ROOT_CAUSE]
    if len(rows) != expected_count:
        raise SystemExit(f"mapped action focus count guard failed: {len(rows)}")
    records = [compact(row) for row in rows]
    records.sort(key=lambda row: (row["problem_class"], row["lifecycle_policy_action"] or "", row["segment_id"]))

    action_counts = Counter(str(row["lifecycle_policy_action"] or "") for row in records)
    policy_status_counts = Counter(row["policy_item_status"] for row in records)
    problem_counts = Counter(row["problem_class"] for row in records)
    token_counts = Counter(row["token_surface"] for row in records)
    expected_state_counts = Counter(row["expected_final_state"] for row in records)
    current_state_counts = Counter(row["current_final_state"] for row in records)
    guard_counts = Counter("guard_ok" if row["guard_ok"] else "guard_failed" for row in records)
    policy_item_count_buckets = Counter("policy_item_count_gt_0" if row["policy_item_count"] > 0 else "policy_item_count_0" for row in records)
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        if len(samples[row["problem_class"]]) < SAMPLE_LIMIT:
            samples[row["problem_class"]].append(row)

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_mapped_action_fast_path_state_guard_missing_diagnostic",
        "input_jsonl": str(input_jsonl),
        "input_summary": str(input_summary),
        "record_count": len(records),
        "expected_record_count": expected_count,
        "root_cause": ROOT_CAUSE,
        "lifecycle_policy_action_counts": dict(action_counts.most_common()),
        "policy_item_status_counts": dict(policy_status_counts.most_common()),
        "policy_item_count_buckets": dict(policy_item_count_buckets.most_common()),
        "problem_class_counts": dict(problem_counts.most_common()),
        "token_surface_counts": dict(token_counts.most_common()),
        "expected_final_state_counts": dict(expected_state_counts.most_common()),
        "current_final_state_counts": dict(current_state_counts.most_common()),
        "guard_counts": dict(guard_counts.most_common()),
        "confirmed_matches_output_1_count": sum(1 for row in records if row["confirmed_matches_output"] == 1),
        "needs_output_apply_0_count": sum(1 for row in records if row["needs_output_apply"] == 0),
        "open_high_issue_0_count": sum(1 for row in records if row["open_issue_count"] == 0 and row["high_issue_count"] == 0),
        "samples_by_problem_class": samples,
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
            "Send to architecture as a fast-path/state-guard consumption bug if policy_item_present_and_allowed dominates; "
            "otherwise materialize missing policy items only for absent-item families after architecture review."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_mapped_action_guard_missing_diagnostic_91"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Mapped action fast-path/state-guard missing diagnostic",
        f"record_count={summary['record_count']}",
        "",
        "Problem classes:",
    ]
    for key, count in summary["problem_class_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Policy item status:"])
    for key, count in summary["policy_item_status_counts"].items():
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
            "source_changed=false",
            "output_changed=false",
            "",
            "Recommendation:",
            summary["single_operational_recommendation"],
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    records, summary = build(args.input_jsonl, args.input_summary, args.expected_count)
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
