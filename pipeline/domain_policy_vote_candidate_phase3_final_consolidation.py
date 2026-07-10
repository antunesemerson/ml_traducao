from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_phase3_final_consolidation_v1"
RESIDUAL_SUMMARY = Path("reports/20260701_195438_916356_domain_policy_vote_candidate_phase3_residual_consolidation_summary.json")
MULTILINE_HOLD_SUMMARY = Path("reports/20260701_195852_997587_domain_policy_vote_candidate_phase3_multiline_terminal_hold_diagnostic_summary.json")
EXPECTED_PHASE3_TOTAL = 1305


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only final consolidation for phase 3 residual work.")
    parser.add_argument("--residual-summary", type=Path, default=RESIDUAL_SUMMARY)
    parser.add_argument("--multiline-hold-summary", type=Path, default=MULTILINE_HOLD_SUMMARY)
    parser.add_argument("--expected-phase3-total", type=int, default=EXPECTED_PHASE3_TOTAL)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(db.project_path(path).read_text(encoding="utf-8"))


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    residual = read_json(args.residual_summary)
    multiline = read_json(args.multiline_hold_summary)

    if int(residual.get("phase3_total") or 0) != args.expected_phase3_total:
        raise SystemExit("phase3 total guard failed")
    if int(multiline.get("record_count") or 0) != 1064:
        raise SystemExit("multiline hold count guard failed")
    for summary in (residual, multiline):
        for key in ("candidate_generation_count", "apply_count", "lifecycle_count", "segment_state_count", "reindex_count", "production_full_count"):
            if int(summary.get(key) or 0) != 0:
                raise SystemExit(f"{summary.get('source')} {key} guard failed")

    absorbed_total = int(residual.get("catalog_absorbed_total") or 0)
    multiline_terminal_total = int(multiline.get("record_count") or 0)
    dynamic_select_hold = int(residual.get("dynamic_select_hold") or 0)
    dense_or_mixed = 49
    custom_generic_pronoun = 8
    relation_possessive = 5
    small_non_terminal_hold = dynamic_select_hold + dense_or_mixed + custom_generic_pronoun + relation_possessive
    accounted_total = absorbed_total + multiline_terminal_total + small_non_terminal_hold
    if accounted_total != args.expected_phase3_total:
        raise SystemExit(f"accounting guard failed: {accounted_total}")

    rows: list[dict[str, Any]] = [
        {
            "record_type": "absorbed_catalog_only",
            "family": "policy_absorbed_select_and_getter_light",
            "count": absorbed_total,
            "state": "absorbed_by_existing_policy_catalog_only",
            "next_action": "no_lifecycle_yet",
        },
        {
            "record_type": "terminal_hold",
            "family": "multiline_parser_later_all",
            "count": multiline_terminal_total,
            "state": "terminal_hold_parser_later",
            "next_action": "optional_architecture_only_for_specific_parser_family",
        },
        {
            "record_type": "hold",
            "family": "dynamic_select_remaining",
            "count": dynamic_select_hold,
            "state": "hold_select_multiline_multiple_or_gender_policy_later",
            "next_action": "architecture_if_select_policy_becomes_priority",
        },
        {
            "record_type": "hold",
            "family": "dynamic_getter_dense_or_mixed",
            "count": dense_or_mixed,
            "state": "hold_context_or_parser_later",
            "next_action": "architecture_if_dense_getter_policy_becomes_priority",
        },
        {
            "record_type": "hold",
            "family": "dynamic_getter_custom_generic_pronoun_context",
            "count": custom_generic_pronoun,
            "state": "hold_context",
            "next_action": "manual_or_architecture_context_later",
        },
        {
            "record_type": "hold",
            "family": "dynamic_getter_relation_possessive_ambiguity",
            "count": relation_possessive,
            "state": "hold_relation_possessive_ambiguity",
            "next_action": "narrow_architecture_prompt_if_needed",
        },
    ]
    for family, count in (multiline.get("terminal_hold_family_counts") or {}).items():
        rows.append(
            {
                "record_type": "terminal_hold_detail",
                "family": family,
                "count": int(count),
                "state": "terminal_hold_parser_later",
                "next_action": "no_lifecycle_close",
            }
        )

    type_counts = Counter()
    for row in rows:
        if row["record_type"] != "terminal_hold_detail":
            type_counts[row["record_type"]] += int(row["count"])

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_phase3_final_consolidation",
        "phase3_total": args.expected_phase3_total,
        "accounted_total": accounted_total,
        "absorbed_catalog_only_total": absorbed_total,
        "terminal_hold_total": multiline_terminal_total,
        "small_hold_total": small_non_terminal_hold,
        "record_type_counts": dict(type_counts.most_common()),
        "terminal_hold_family_counts": multiline.get("terminal_hold_family_counts") or {},
        "parser_recommendation_counts": multiline.get("parser_recommendation_counts") or {},
        "consolidated_rows": rows,
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
            "Keep lifecycle on hold for phase 3 residual. The block is now mostly terminal hold/parser-later; "
            "the only architecture-worthy narrow prompt is relation/possessive ambiguity (5) or a chosen multiline parser family."
        ),
    }
    return rows, summary


def write_reports(rows: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_phase3_final_consolidation"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, rows)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Phase 3 final consolidation",
        f"phase3_total={summary['phase3_total']}",
        f"accounted_total={summary['accounted_total']}",
        f"absorbed_catalog_only_total={summary['absorbed_catalog_only_total']}",
        f"terminal_hold_total={summary['terminal_hold_total']}",
        f"small_hold_total={summary['small_hold_total']}",
        "",
        "Consolidated rows:",
    ]
    for row in rows:
        lines.append(f"- {row['record_type']} | {row['family']}: {row['count']} -> {row['state']}")
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
    rows, summary = build(args)
    txt, jsonl, summary_path = write_reports(rows, summary)
    print(f"txt={txt}")
    print(f"jsonl={jsonl}")
    print(f"summary={summary_path}")
    print(f"phase3_total={summary['phase3_total']}")
    print(f"accounted_total={summary['accounted_total']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
