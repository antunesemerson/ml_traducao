from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_phase3_dynamic_getter_medium_absorption_dry_run_v1"
INPUT_JSONL = Path("reports/20260701_193938_041725_domain_policy_vote_candidate_phase3_dynamic_getter_medium_architecture_packet.jsonl")
EXPECTED_COUNT = 17
SAMPLE_LIMIT = 8

SELECT_RE = re.compile(r"Select_CString|SelectLocalization|AddLocalizationIf|LocalPlayerString", re.IGNORECASE)
ES_TOKEN_RE = re.compile(r"\[[^\]]+?\.Custom\(['\"]ES_[A-Za-z0-9_]+['\"]\)\]")
EFFECT_LIST_RE = re.compile(r"\$EFFECT_LIST_BULLET\$|\$EFFECT\$", re.IGNORECASE)
RELATION_RE = re.compile(r"Get(?:HerHis|HerHim|SheHe|WomanMan|WifeHusband|MotherFather)|possessiv|relation", re.IGNORECASE)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only absorption dry-run for phase 3 medium getter rows.")
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


def has_newline(text: str) -> bool:
    return "\\n" in text or "\n" in text


def canonical_l10n(text: str | None) -> str:
    value = str(text or "")
    value = value.replace("\\\"", '"')
    value = value.replace("\\n", "\n")
    return value.strip()


def route_for(row: dict[str, Any]) -> str:
    recommendation = str(row.get("architecture_recommendation") or "")
    if "getter_character_or_name_policy" in recommendation:
        return "getter_character_or_name_policy"
    if "getter_title_or_realm_name_policy" in recommendation:
        return "getter_title_or_realm_name_policy"
    if "getter_faith_name_policy" in recommendation:
        return "getter_faith_name_policy"
    if "getter_culture_name_policy" in recommendation:
        return "getter_culture_name_policy"
    return "hold_unmapped_medium_getter"


def guard_failures(row: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    text = str(row.get("confirmed_text") or "")
    output = str(row.get("output_text") or "")

    if row.get("density_bucket") != "medium_getter":
        failures.append("not_medium_getter")
    if row.get("getter_role") not in {
        "character_or_name_getter",
        "title_or_realm_getter",
        "faith_getter",
        "culture_getter",
    }:
        failures.append("not_single_dominant_allowed_getter_role")
    if SELECT_RE.search(text):
        failures.append("select_surface_present")
    if ES_TOKEN_RE.search(text) or row.get("has_es_token"):
        failures.append("es_helper_present")
    if has_newline(text) or has_newline(output) or row.get("has_newline"):
        failures.append("multiline_present")
    if EFFECT_LIST_RE.search(text):
        failures.append("effect_list_present")
    if RELATION_RE.search(text):
        failures.append("possessive_relation_ambiguity")
    if canonical_l10n(output) != canonical_l10n(text):
        failures.append("canonical_output_confirmed_mismatch")
    if int(row.get("needs_output_apply") or 0) != 0:
        failures.append("needs_output_apply_not_0")
    if int(row.get("open_issue_count") or 0) != 0:
        failures.append("open_issue_count_not_0")
    if int(row.get("high_issue_count") or 0) != 0:
        failures.append("high_issue_count_not_0")
    if int(row.get("getter_count") or 0) < 1:
        failures.append("getter_count_missing")
    if int(row.get("getter_count") or 0) > 2:
        failures.append("getter_count_too_dense_for_medium_guard")
    return failures


def build_record(row: dict[str, Any]) -> dict[str, Any]:
    failures = guard_failures(row)
    route = route_for(row)
    released = not failures and route != "hold_unmapped_medium_getter"
    return {
        "source": SOURCE,
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "getter_role": row.get("getter_role"),
        "density_bucket": row.get("density_bucket"),
        "architecture_recommendation": row.get("architecture_recommendation"),
        "dry_run_route": route if released else "hold_guard_failed",
        "would_absorb": released,
        "guard_ok": not failures,
        "guard_failures": failures,
        "getter_count": int(row.get("getter_count") or 0),
        "bracket_token_count": int(row.get("bracket_token_count") or 0),
        "sample_getter_tokens": row.get("sample_getter_tokens") or [],
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
    records.sort(key=lambda row: (row["dry_run_route"], row["segment_id"]))

    route_counts = Counter(row["dry_run_route"] for row in records)
    role_counts = Counter(row["getter_role"] for row in records)
    guard_counts = Counter("guard_ok" if row["guard_ok"] else "guard_failed" for row in records)
    failure_counts = Counter(failure for row in records for failure in row["guard_failures"])
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        if len(samples[row["dry_run_route"]]) < SAMPLE_LIMIT:
            samples[row["dry_run_route"]].append(row)

    released_count = sum(1 for row in records if row["would_absorb"])
    blocked_count = len(records) - released_count
    expected_routes_ok = (
        route_counts.get("getter_character_or_name_policy", 0) == 10
        and route_counts.get("getter_title_or_realm_name_policy", 0) == 5
        and route_counts.get("getter_faith_name_policy", 0) == 1
        and route_counts.get("getter_culture_name_policy", 0) == 1
        and blocked_count == 0
    )

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_dynamic_getter_medium_absorption_dry_run",
        "input_jsonl": str(input_jsonl),
        "record_count": len(records),
        "expected_record_count": expected_count,
        "released_count": released_count,
        "blocked_count": blocked_count,
        "expected_routes_ok": expected_routes_ok,
        "route_counts": dict(route_counts.most_common()),
        "getter_role_counts": dict(role_counts.most_common()),
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
            "If expected_routes_ok is true, catalog-only absorption is sufficient; no registry shadow is needed unless architecture wants explicit audit routing."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_phase3_dynamic_getter_medium_absorption_dry_run"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Phase 3 dynamic getter medium absorption dry-run",
        f"record_count={summary['record_count']}",
        f"released_count={summary['released_count']}",
        f"blocked_count={summary['blocked_count']}",
        f"expected_routes_ok={str(summary['expected_routes_ok']).lower()}",
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
