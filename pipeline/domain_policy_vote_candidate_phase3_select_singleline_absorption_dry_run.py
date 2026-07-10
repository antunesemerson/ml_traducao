from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_phase3_select_singleline_absorption_dry_run_v1"
INPUT_JSONL = Path("reports/20260701_174714_581159_domain_policy_vote_candidate_phase3_select_singleline_architecture_packet.jsonl")
EXPECTED_COUNT = 14
SAMPLE_LIMIT = 8

SELECT_CSTRING_RE = re.compile(r"Select_CString\s*\(", re.IGNORECASE)
FORBIDDEN_SELECT_RE = re.compile(r"SelectLocalization\s*\(|AddLocalizationIf\s*\(|LocalPlayerString\s*\(", re.IGNORECASE)
ES_TOKEN_RE = re.compile(r"\[[^\]]+?\.Custom\(['\"]ES_[A-Za-z0-9_]+['\"]\)\]")
PROTECTED_TOKEN_RE = re.compile(r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|!/-]+|#!|@[A-Za-z0-9_]+!")
SELECT_STRUCT_RE = re.compile(r"(Select_CString)\s*\(\s*([^,)\]]+)")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only absorption dry-run for phase 3 single-line Select_CString items.")
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


def text_fields(row: dict[str, Any]) -> list[str]:
    return [
        str(row.get("source_text") or ""),
        str(row.get("output_text") or ""),
        str(row.get("confirmed_text") or ""),
    ]


def has_newline(text: str) -> bool:
    return "\\n" in text or "\n" in text


def protected_tokens(text: str) -> list[str]:
    return PROTECTED_TOKEN_RE.findall(text or "")


def token_signature(text: str) -> list[str]:
    return sorted(protected_tokens(text))


def select_structure_signature(text: str) -> list[tuple[str, str]]:
    return [(match.group(1), re.sub(r"\s+", "", match.group(2))) for match in SELECT_STRUCT_RE.finditer(text or "")]


def guard_failures(row: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    confirmed = str(row.get("confirmed_text") or "")
    output = str(row.get("output_text") or "")
    source = str(row.get("source_text") or "")
    select_functions = set(row.get("select_functions") or [])
    structure = row.get("structure_flags") or {}

    if select_functions != {"Select_CString"}:
        failures.append("not_select_cstring_only")
    if FORBIDDEN_SELECT_RE.search(confirmed):
        failures.append("forbidden_select_surface_present")
    if len(SELECT_CSTRING_RE.findall(confirmed)) != 1:
        failures.append("select_cstring_count_not_1")
    if any(has_newline(text) for text in text_fields(row)):
        failures.append("not_single_line")
    if row.get("has_es_helper") or ES_TOKEN_RE.search(confirmed):
        failures.append("es_helper_present")
    if int(structure.get("select_total_count") or 0) != 1:
        failures.append("structure_select_total_count_not_1")
    if int(structure.get("confirmed_matches_output") or 0) != 1:
        failures.append("confirmed_matches_output_not_1")
    if int(structure.get("needs_output_apply") or 0) != 0:
        failures.append("needs_output_apply_not_0")
    if int(structure.get("open_issue_count") or 0) != 0:
        failures.append("open_issue_count_not_0")
    if int(structure.get("high_issue_count") or 0) != 0:
        failures.append("high_issue_count_not_0")
    if token_signature(output) != token_signature(confirmed):
        failures.append("output_confirmed_token_signature_mismatch")
    if not token_signature(confirmed):
        failures.append("missing_protected_token_signature")
    if SELECT_CSTRING_RE.search(source) and select_structure_signature(source) != select_structure_signature(confirmed):
        failures.append("source_confirmed_select_structure_mismatch")
    return failures


def route_for(row: dict[str, Any]) -> str:
    family = row.get("architecture_family")
    if family == "select_cstring_gender_literal_singleline":
        return "route_select_cstring_gender_literal_policy"
    if family == "select_cstring_player_perspective_singleline":
        return "route_select_cstring_player_target_direct_policy"
    return "route_hold_unmapped_singleline_select_cstring"


def build_record(row: dict[str, Any]) -> dict[str, Any]:
    failures = guard_failures(row)
    route = route_for(row)
    released = not failures and route != "route_hold_unmapped_singleline_select_cstring"
    return {
        "source": SOURCE,
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "architecture_family": row.get("architecture_family"),
        "policy_fit_recommendation": row.get("policy_fit_recommendation"),
        "dry_run_route": route if released else "hold_guard_failed",
        "would_absorb": released,
        "guard_ok": not failures,
        "guard_failures": failures,
        "select_functions": row.get("select_functions") or [],
        "select_expressions": row.get("select_expressions") or [],
        "has_gender_literal": bool(row.get("has_gender_literal")),
        "has_player_perspective": bool(row.get("has_player_perspective")),
        "has_es_helper": bool(row.get("has_es_helper")),
        "protected_token_count": int(row.get("protected_token_count") or 0),
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


def build(input_jsonl: Path, expected_count: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = read_jsonl(input_jsonl)
    if len(rows) != expected_count:
        raise SystemExit(f"input count guard failed: {len(rows)}")
    records = [build_record(row) for row in rows]
    records.sort(key=lambda item: (item["dry_run_route"], item["segment_id"]))

    route_counts = Counter(row["dry_run_route"] for row in records)
    family_counts = Counter(row["architecture_family"] for row in records)
    fit_counts = Counter(row["policy_fit_recommendation"] for row in records)
    guard_counts = Counter("guard_ok" if row["guard_ok"] else "guard_failed" for row in records)
    failure_counts = Counter(failure for row in records for failure in row["guard_failures"])
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        if len(samples[row["dry_run_route"]]) < SAMPLE_LIMIT:
            samples[row["dry_run_route"]].append(row)

    released_count = sum(1 for row in records if row["would_absorb"])
    blocked_count = len(records) - released_count
    expected_routes_ok = (
        route_counts.get("route_select_cstring_gender_literal_policy", 0) == 13
        and route_counts.get("route_select_cstring_player_target_direct_policy", 0) == 1
        and blocked_count == 0
    )
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_phase3_select_singleline_absorption_dry_run",
        "input_jsonl": str(input_jsonl),
        "record_count": len(records),
        "expected_record_count": expected_count,
        "released_count": released_count,
        "blocked_count": blocked_count,
        "expected_routes_ok": expected_routes_ok,
        "route_counts": dict(route_counts.most_common()),
        "architecture_family_counts": dict(family_counts.most_common()),
        "policy_fit_recommendation_counts": dict(fit_counts.most_common()),
        "guard_counts": dict(guard_counts.most_common()),
        "guard_failure_counts": dict(failure_counts.most_common()),
        "samples_by_route": samples,
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
            "Catalog absorption only. Do not register a new splitter unless architecture wants an explicit shadow route for audit; "
            "the 14 cases can be routed by existing Select_CString/gender/player policies under read-only guards."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_phase3_select_singleline_absorption_dry_run"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Phase 3 Select_CString single-line absorption dry-run",
        f"record_count={summary['record_count']}",
        f"released_count={summary['released_count']}",
        f"blocked_count={summary['blocked_count']}",
        "",
        "Routes:",
    ]
    for key, count in summary["route_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Guards:"])
    for key, count in summary["guard_counts"].items():
        lines.append(f"- {key}: {count}")
    if summary["guard_failure_counts"]:
        lines.extend(["", "Guard failures:"])
        for key, count in summary["guard_failure_counts"].items():
            lines.append(f"- {key}: {count}")
    lines.extend(
        [
            "",
            "Operation counters:",
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
