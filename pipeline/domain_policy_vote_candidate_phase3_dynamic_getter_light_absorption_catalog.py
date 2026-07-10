from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_phase3_dynamic_getter_light_absorption_catalog_v1"
INPUT_JSONL = Path("reports/20260701_183924_613583_domain_policy_vote_candidate_phase3_dynamic_getter_light_review.jsonl")
EXPECTED_ABSORBED_COUNT = 22


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only catalog for phase 3 light dynamic getter absorption.")
    parser.add_argument("--input-jsonl", type=Path, default=INPUT_JSONL)
    parser.add_argument("--expected-absorbed-count", type=int, default=EXPECTED_ABSORBED_COUNT)
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


def absorbed_policy(route: str) -> str:
    mapping = {
        "route_getter_character_or_name_policy": "getter_character_or_name_policy",
        "route_getter_title_or_realm_name_policy": "getter_title_or_realm_name_policy",
        "route_getter_faith_name_policy": "getter_faith_name_policy",
        "route_getter_culture_name_policy": "getter_culture_name_policy",
    }
    return mapping.get(route, "hold_unmapped_getter_policy")


def build(input_jsonl: Path, expected_absorbed_count: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = read_jsonl(input_jsonl)
    selected = [
        row
        for row in rows
        if row.get("review_decision") == "catalog_absorption_candidate_existing_getter_role_policy"
    ]
    if len(selected) != expected_absorbed_count:
        raise SystemExit(f"absorbed count guard failed: {len(selected)}")

    records: list[dict[str, Any]] = []
    for row in selected:
        route = str(row.get("review_route") or "")
        records.append(
            {
                "source": SOURCE,
                "segment_id": int(row["segment_id"]),
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "absorption_status": "absorbed_by_existing_policy",
                "absorbed_route": route,
                "absorbed_policy": absorbed_policy(route),
                "getter_role": row.get("getter_role"),
                "density_bucket": row.get("density_bucket"),
                "structural_family": row.get("structural_family"),
                "sample_getter_tokens": row.get("sample_getter_tokens") or [],
                "candidate_generation_allowed": False,
                "apply_allowed": False,
                "lifecycle_allowed": False,
                "production_full_allowed": False,
                "registry_action": "none_catalog_only",
                "splitter_registered": False,
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
    role_counts = Counter(row["getter_role"] for row in records)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_dynamic_getter_light_absorption_catalog",
        "input_jsonl": str(input_jsonl),
        "record_count": len(records),
        "expected_absorbed_count": expected_absorbed_count,
        "absorption_status_counts": {"absorbed_by_existing_policy": len(records)},
        "absorbed_policy_counts": dict(policy_counts.most_common()),
        "absorbed_route_counts": dict(route_counts.most_common()),
        "getter_role_counts": dict(role_counts.most_common()),
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
            "Absorption catalog complete for 22 single-getter rows. Next safe step is a read-only architecture packet for the 17 medium-getter rows."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_phase3_dynamic_getter_light_absorption_catalog"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Phase 3 dynamic getter light absorption catalog",
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
    records, summary = build(args.input_jsonl, args.expected_absorbed_count)
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
