from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_phase3_dynamic_getter_medium_absorption_catalog_v1"
INPUT_JSONL = Path("reports/20260701_194933_896214_domain_policy_vote_candidate_phase3_dynamic_getter_medium_absorption_dry_run.jsonl")
INPUT_SUMMARY = Path("reports/20260701_194933_896214_domain_policy_vote_candidate_phase3_dynamic_getter_medium_absorption_dry_run_summary.json")
EXPECTED_RELEASED_COUNT = 12
EXPECTED_BLOCKED_COUNT = 5


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only catalog for released medium dynamic getter absorption.")
    parser.add_argument("--input-jsonl", type=Path, default=INPUT_JSONL)
    parser.add_argument("--input-summary", type=Path, default=INPUT_SUMMARY)
    parser.add_argument("--expected-released-count", type=int, default=EXPECTED_RELEASED_COUNT)
    parser.add_argument("--expected-blocked-count", type=int, default=EXPECTED_BLOCKED_COUNT)
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


def validate(summary: dict[str, Any], rows: list[dict[str, Any]], expected_released: int, expected_blocked: int) -> None:
    if summary.get("mode") != "read_only_dynamic_getter_medium_absorption_dry_run":
        raise SystemExit("input summary mode guard failed")
    if int(summary.get("released_count") or 0) != expected_released:
        raise SystemExit("released_count guard failed")
    if int(summary.get("blocked_count") or 0) != expected_blocked:
        raise SystemExit("blocked_count guard failed")
    if int(summary.get("record_count") or 0) != expected_released + expected_blocked:
        raise SystemExit("record_count guard failed")
    for key in ("candidate_generation_count", "apply_count", "lifecycle_count", "segment_state_count", "reindex_count", "production_full_count"):
        if int(summary.get(key) or 0) != 0:
            raise SystemExit(f"{key} guard failed")
    if len(rows) != expected_released + expected_blocked:
        raise SystemExit(f"jsonl count guard failed: {len(rows)}")


def build_record(row: dict[str, Any]) -> dict[str, Any]:
    released = bool(row.get("would_absorb"))
    return {
        "source": SOURCE,
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "catalog_status": "absorbed_by_existing_policy" if released else "hold_relation_possessive_ambiguity",
        "absorbed_policy": row.get("dry_run_route") if released else None,
        "hold_reason": None if released else ";".join(row.get("guard_failures") or []),
        "getter_role": row.get("getter_role"),
        "density_bucket": row.get("density_bucket"),
        "getter_count": int(row.get("getter_count") or 0),
        "bracket_token_count": int(row.get("bracket_token_count") or 0),
        "sample_getter_tokens": row.get("sample_getter_tokens") or [],
        "registry_action": "none_catalog_only",
        "splitter_registered": False,
        "candidate_generation_allowed": False,
        "apply_allowed": False,
        "lifecycle_allowed": False,
        "production_full_allowed": False,
        "output_text": row.get("output_text"),
        "confirmed_text": row.get("confirmed_text"),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
    }


def build(input_jsonl: Path, input_summary: Path, expected_released: int, expected_blocked: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    summary_in = read_json(input_summary)
    rows = read_jsonl(input_jsonl)
    validate(summary_in, rows, expected_released, expected_blocked)
    records = [build_record(row) for row in rows]
    records.sort(key=lambda row: (row["catalog_status"], row["absorbed_policy"] or "", row["segment_id"]))

    status_counts = Counter(row["catalog_status"] for row in records)
    policy_counts = Counter(row["absorbed_policy"] for row in records if row["absorbed_policy"])
    hold_counts = Counter(row["hold_reason"] for row in records if row["hold_reason"])
    role_counts = Counter(row["getter_role"] for row in records)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_dynamic_getter_medium_absorption_catalog",
        "input_jsonl": str(input_jsonl),
        "input_summary": str(input_summary),
        "record_count": len(records),
        "absorbed_count": expected_released,
        "hold_count": expected_blocked,
        "catalog_status_counts": dict(status_counts.most_common()),
        "absorbed_policy_counts": dict(policy_counts.most_common()),
        "hold_reason_counts": dict(hold_counts.most_common()),
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
            "Medium-getter catalog complete for the 12 guard-ok rows. Keep the 5 possessive/relation ambiguous rows in hold; next safe step is consolidating phase 3 residual totals before deciding whether to ask architecture about relation/possessive or move to multiline hold classification."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_phase3_dynamic_getter_medium_absorption_catalog"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Phase 3 dynamic getter medium absorption catalog",
        f"record_count={summary['record_count']}",
        f"absorbed_count={summary['absorbed_count']}",
        f"hold_count={summary['hold_count']}",
        "",
        "Absorbed policies:",
    ]
    for key, count in summary["absorbed_policy_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Hold reasons:"])
    for key, count in summary["hold_reason_counts"].items():
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
    records, summary = build(args.input_jsonl, args.input_summary, args.expected_released_count, args.expected_blocked_count)
    txt, jsonl, summary_path = write_reports(records, summary)
    print(f"txt={txt}")
    print(f"jsonl={jsonl}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"absorbed_count={summary['absorbed_count']}")
    print(f"hold_count={summary['hold_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
