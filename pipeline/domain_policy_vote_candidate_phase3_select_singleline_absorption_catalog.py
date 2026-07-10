from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_phase3_select_singleline_absorption_catalog_v1"
INPUT_JSONL = Path("reports/20260701_180351_491006_domain_policy_vote_candidate_phase3_select_singleline_absorption_dry_run.jsonl")
INPUT_SUMMARY = Path("reports/20260701_180351_491006_domain_policy_vote_candidate_phase3_select_singleline_absorption_dry_run_summary.json")
EXPECTED_COUNT = 14


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only catalog for Select_CString single-line absorption.")
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


def catalog_policy(route: str) -> str:
    if route == "route_select_cstring_gender_literal_policy":
        return "select_cstring_gender_literal_policy"
    if route == "route_select_cstring_player_target_direct_policy":
        return "select_cstring_player_target_direct_policy"
    return "hold_unmapped_select_cstring_policy"


def validate_inputs(summary: dict[str, Any], rows: list[dict[str, Any]], expected_count: int) -> None:
    if summary.get("mode") != "read_only_phase3_select_singleline_absorption_dry_run":
        raise SystemExit("input summary mode guard failed")
    if int(summary.get("record_count") or 0) != expected_count:
        raise SystemExit("summary record_count guard failed")
    if int(summary.get("released_count") or 0) != expected_count:
        raise SystemExit("released_count guard failed")
    if int(summary.get("blocked_count") or 0) != 0:
        raise SystemExit("blocked_count guard failed")
    if summary.get("expected_routes_ok") is not True:
        raise SystemExit("expected_routes_ok guard failed")
    if len(rows) != expected_count:
        raise SystemExit(f"jsonl count guard failed: {len(rows)}")
    forbidden = ["candidate_generation_count", "apply_count", "lifecycle_count", "segment_state_count", "reindex_count", "production_full_count"]
    for key in forbidden:
        if int(summary.get(key) or 0) != 0:
            raise SystemExit(f"{key} guard failed")


def build(input_jsonl: Path, input_summary: Path, expected_count: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    summary_in = read_json(input_summary)
    rows = read_jsonl(input_jsonl)
    validate_inputs(summary_in, rows, expected_count)

    records: list[dict[str, Any]] = []
    for row in rows:
        route = str(row.get("dry_run_route") or "")
        records.append(
            {
                "source": SOURCE,
                "segment_id": int(row["segment_id"]),
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "absorption_status": "absorbed_by_existing_policy",
                "absorbed_route": route,
                "absorbed_policy": catalog_policy(route),
                "candidate_generation_allowed": False,
                "apply_allowed": False,
                "lifecycle_allowed": False,
                "production_full_allowed": False,
                "registry_action": "none_catalog_only",
                "splitter_registered": False,
                "select_functions": row.get("select_functions") or [],
                "select_expressions": row.get("select_expressions") or [],
                "has_gender_literal": bool(row.get("has_gender_literal")),
                "has_player_perspective": bool(row.get("has_player_perspective")),
                "has_es_helper": bool(row.get("has_es_helper")),
                "source_text": row.get("source_text"),
                "output_text": row.get("output_text"),
                "confirmed_text": row.get("confirmed_text"),
                "candidate_generation_count": 0,
                "apply_count": 0,
                "lifecycle_count": 0,
                "segment_state_count": 0,
                "reindex_count": 0,
                "production_full_count": 0,
            }
        )

    policy_counts = Counter(row["absorbed_policy"] for row in records)
    route_counts = Counter(row["absorbed_route"] for row in records)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_absorption_catalog",
        "input_jsonl": str(input_jsonl),
        "input_summary": str(input_summary),
        "record_count": len(records),
        "expected_record_count": expected_count,
        "absorption_status_counts": {"absorbed_by_existing_policy": len(records)},
        "absorbed_policy_counts": dict(policy_counts.most_common()),
        "absorbed_route_counts": dict(route_counts.most_common()),
        "registry_action": "none_catalog_only",
        "splitter_registered": False,
        "candidate_generation_allowed": False,
        "apply_allowed": False,
        "lifecycle_allowed": False,
        "production_full_allowed": False,
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
            "Absorption catalog complete. No splitter registration is needed now; continue with read-only phase 3 dynamic_getter residual diagnostic."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_phase3_select_singleline_absorption_catalog"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Phase 3 Select_CString single-line absorption catalog",
        f"record_count={summary['record_count']}",
        "",
        "Absorbed policies:",
    ]
    for key, count in summary["absorbed_policy_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(
        [
            "",
            "Guards:",
            "registry_action=none_catalog_only",
            "splitter_registered=false",
            "candidate_generation_allowed=false",
            "apply_allowed=false",
            "lifecycle_allowed=false",
            "production_full_allowed=false",
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
