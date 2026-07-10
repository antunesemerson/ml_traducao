from __future__ import annotations

import json
import argparse
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_phase3_remaining_structural_diagnostic_v1"
INPUT_JSONL = Path("reports/20260701_155604_125890_domain_policy_vote_candidate_closure_debt_architecture_packet_512_532.jsonl")
CURRENT_RUN_ID = 532
EXPECTED_COUNT = 1362
PHASE = "phase_3_human_misc_equal_output_bridge"
SAMPLE_LIMIT = 8


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only phase 3 remaining structural diagnostic.")
    parser.add_argument("--input-jsonl", type=Path, default=INPUT_JSONL)
    parser.add_argument("--current-run-id", type=int, default=CURRENT_RUN_ID)
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


def structural_family(row: dict[str, Any]) -> str:
    text = "\n".join(
        str(row.get(key) or "")
        for key in ("output_text", "confirmed_text", "source_key", "relative_path")
    )
    surface = str(row.get("token_surface") or "")
    if surface == "multiline":
        if "$EFFECT_LIST_BULLET$" in text:
            return "multiline_effect_list"
        if "\\n\\n" in text or "\n\n" in text:
            return "multiline_paragraph"
        return "multiline_other"
    if "Select_CString" in text or "SelectLocalization" in text:
        return "dynamic_select_localization"
    if "Custom('ES_" in text or 'Custom("ES_' in text:
        return "dynamic_getter_gender_suffix"
    if ".Custom(" in text or ".Custom('" in text:
        return "dynamic_getter_custom"
    if ".GetFaith" in text or "Faith." in text:
        return "dynamic_getter_faith"
    if ".GetCulture" in text or "Culture." in text:
        return "dynamic_getter_culture"
    if ".GetPrimaryTitle" in text or ".GetTitle" in text or "GetTitle" in text:
        return "dynamic_getter_title_realm"
    if ".GetFirstName" in text or ".GetTitledFirstName" in text or ".GetName" in text:
        return "dynamic_getter_name"
    if "ROOT." in text or "SCOPE." in text or "scope:" in text:
        return "dynamic_getter_scope_root"
    if surface == "dynamic_select":
        return "dynamic_select_other"
    if surface == "dynamic_getter":
        return "dynamic_getter_other"
    return f"other_{surface}"


def operational_bucket(row: dict[str, Any], family: str) -> str:
    surface = str(row.get("token_surface") or "")
    if surface in {"plain_text", "light_token"}:
        return "unexpected_plain_light_after_tranche"
    if family in {"dynamic_getter_gender_suffix", "dynamic_select_localization"}:
        return "architecture_or_parser_candidate"
    if family.startswith("multiline_"):
        return "hold_multiline_or_needs_parser"
    if family in {
        "dynamic_getter_faith",
        "dynamic_getter_culture",
        "dynamic_getter_title_realm",
        "dynamic_getter_name",
        "dynamic_getter_scope_root",
        "dynamic_getter_custom",
        "dynamic_getter_other",
    }:
        return "needs_structural_policy_review"
    return "hold_contextual"


def compact(row: dict[str, Any], current_run_id: int) -> dict[str, Any]:
    family = structural_family(row)
    bucket = operational_bucket(row, family)
    return {
        "source": SOURCE,
        "current_run_id": current_run_id,
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "phase": row.get("phase"),
        "final_state_current": row.get("to_final_state"),
        "classification": row.get("classification"),
        "confirmation_bucket": row.get("confirmation_bucket"),
        "confirmation_level": row.get("confirmation_level"),
        "confirmation_source": row.get("confirmation_source"),
        "confirmation_label": row.get("confirmation_label"),
        "locked": int(row.get("locked") or 0),
        "token_surface": row.get("token_surface"),
        "structural_family": family,
        "operational_bucket": bucket,
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "high_issue_count": int(row.get("high_issue_count") or 0),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
    }


def build(
    rows: list[dict[str, Any]],
    input_jsonl: Path,
    current_run_id: int,
    expected_count: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    focus = [row for row in rows if row.get("phase") == PHASE]
    records = [compact(row, current_run_id) for row in focus]
    surface_counts = Counter(row["token_surface"] for row in records)
    family_counts = Counter(row["structural_family"] for row in records)
    bucket_counts = Counter(row["operational_bucket"] for row in records)
    source_counts = Counter(row["confirmation_source"] for row in records)
    label_counts = Counter(row["confirmation_label"] for row in records)
    locked_counts = Counter("locked" if row["locked"] else "unlocked" for row in records)
    family_surface_counts = Counter(f"{row['token_surface']}::{row['structural_family']}" for row in records)
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        if len(samples[row["structural_family"]]) < SAMPLE_LIMIT:
            samples[row["structural_family"]].append(row)

    candidate_family_counts = {
        key: value
        for key, value in family_counts.items()
        if key in {"dynamic_getter_gender_suffix", "dynamic_select_localization"}
    }
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_phase3_remaining_structural_diagnostic",
        "input_jsonl": str(input_jsonl),
        "current_run_id": current_run_id,
        "record_count": len(records),
        "expected_record_count": expected_count,
        "token_surface_counts": dict(sorted(surface_counts.items())),
        "structural_family_counts": dict(family_counts.most_common(40)),
        "operational_bucket_counts": dict(sorted(bucket_counts.items())),
        "family_surface_counts": dict(family_surface_counts.most_common(50)),
        "confirmation_source_counts": dict(source_counts.most_common(50)),
        "confirmation_label_counts": dict(label_counts.most_common(60)),
        "locked_counts": dict(sorted(locked_counts.items())),
        "candidate_family_counts": candidate_family_counts,
        "samples_by_structural_family": samples,
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
            "Do not materialize broad dynamic/multiline lifecycle. Review the candidate parser families first, especially dynamic_getter_gender_suffix and dynamic_select_localization, and keep multiline in hold/parser-later."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_phase3_remaining_structural_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Phase 3 remaining structural diagnostic",
        f"record_count={summary['record_count']}",
        "",
        "Token surfaces:",
    ]
    for key, count in summary["token_surface_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Structural families:"])
    for key, count in summary["structural_family_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Operational buckets:"])
    for key, count in summary["operational_bucket_counts"].items():
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
    rows = read_jsonl(args.input_jsonl)
    records, summary = build(rows, args.input_jsonl, args.current_run_id, args.expected_count)
    if summary["record_count"] != args.expected_count:
        raise SystemExit(f"phase3 remaining count guard failed: {summary['record_count']}")
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
