from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_phase3_dynamic_getter_light_review_v1"
INPUT_JSONL = Path("reports/20260701_183708_568780_domain_policy_vote_candidate_phase3_dynamic_getter_residual_diagnostic.jsonl")
EXPECTED_COUNT = 47
SAMPLE_LIMIT = 8


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only review for light phase 3 dynamic getter residuals.")
    parser.add_argument("--input-jsonl", type=Path, default=INPUT_JSONL)
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


def policy_route(row: dict[str, Any]) -> str:
    role = row.get("getter_role")
    if role == "character_or_name_getter":
        return "route_getter_character_or_name_policy"
    if role == "title_or_realm_getter":
        return "route_getter_title_or_realm_name_policy"
    if role == "faith_getter":
        return "route_getter_faith_name_policy"
    if role == "culture_getter":
        return "route_getter_culture_name_policy"
    if role == "pronoun_or_relation_getter":
        return "hold_pronoun_relation_context"
    if role == "custom_getter":
        return "hold_custom_getter_context"
    if role == "generic_getter":
        return "hold_generic_getter_context"
    return "hold_unmapped_getter_role"


def policy_decision(route: str, density: str) -> str:
    if route.startswith("route_getter_") and density == "single_getter_light":
        return "catalog_absorption_candidate_existing_getter_role_policy"
    if route.startswith("route_getter_") and density == "medium_getter":
        return "needs_architecture_confirmation_medium_getter"
    return "hold_context_or_parser_later"


def build_record(row: dict[str, Any]) -> dict[str, Any]:
    route = policy_route(row)
    decision = policy_decision(route, str(row.get("density_bucket")))
    return {
        "source": SOURCE,
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "final_state_current": row.get("final_state_current"),
        "confirmation_source": row.get("confirmation_source"),
        "confirmation_label": row.get("confirmation_label"),
        "locked": int(row.get("locked") or 0),
        "structural_family": row.get("structural_family"),
        "getter_role": row.get("getter_role"),
        "density_bucket": row.get("density_bucket"),
        "source_operational_bucket": row.get("operational_bucket"),
        "review_route": route,
        "review_decision": decision,
        "getter_count": int(row.get("getter_count") or 0),
        "bracket_token_count": int(row.get("bracket_token_count") or 0),
        "has_newline": bool(row.get("has_newline")),
        "has_es_token": bool(row.get("has_es_token")),
        "has_select": bool(row.get("has_select")),
        "sample_getter_tokens": row.get("sample_getter_tokens") or [],
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "high_issue_count": int(row.get("high_issue_count") or 0),
        "output_text": row.get("output_text"),
        "confirmed_text": row.get("confirmed_text"),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
    }


def build(input_jsonl: Path, expected_count: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected = [
        row
        for row in read_jsonl(input_jsonl)
        if row.get("operational_bucket") in {"review_single_getter_policy_candidate", "review_medium_getter_route_candidate"}
    ]
    if len(selected) != expected_count:
        raise SystemExit(f"light getter count guard failed: {len(selected)}")

    records = [build_record(row) for row in selected]
    records.sort(key=lambda row: (row["review_decision"], row["review_route"], row["segment_id"]))

    route_counts = Counter(row["review_route"] for row in records)
    decision_counts = Counter(row["review_decision"] for row in records)
    role_counts = Counter(row["getter_role"] for row in records)
    density_counts = Counter(row["density_bucket"] for row in records)
    family_counts = Counter(row["structural_family"] for row in records)
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        if len(samples[row["review_decision"]]) < SAMPLE_LIMIT:
            samples[row["review_decision"]].append(row)

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_phase3_dynamic_getter_light_review",
        "input_jsonl": str(input_jsonl),
        "record_count": len(records),
        "expected_record_count": expected_count,
        "route_counts": dict(route_counts.most_common()),
        "review_decision_counts": dict(decision_counts.most_common()),
        "getter_role_counts": dict(role_counts.most_common()),
        "density_bucket_counts": dict(density_counts.most_common()),
        "structural_family_counts": dict(family_counts.most_common()),
        "samples_by_decision": samples,
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
            "Catalog single-getter absorption candidates, send medium-getter candidates to architecture confirmation, "
            "and keep custom/generic/pronoun relation cases in hold/context. Do not run lifecycle yet."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_phase3_dynamic_getter_light_review"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Phase 3 dynamic getter light review",
        f"record_count={summary['record_count']}",
        "",
        "Review decisions:",
    ]
    for key, count in summary["review_decision_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Routes:"])
    for key, count in summary["route_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Getter roles:"])
    for key, count in summary["getter_role_counts"].items():
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
    records, summary = build(args.input_jsonl, args.expected_count)
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
