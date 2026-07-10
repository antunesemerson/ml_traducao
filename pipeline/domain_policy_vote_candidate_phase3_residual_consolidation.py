from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_phase3_residual_consolidation_v1"
STRUCTURAL_SUMMARY = Path("reports/20260701_170354_834884_domain_policy_vote_candidate_phase3_remaining_structural_diagnostic_summary.json")
SELECT_CATALOG_SUMMARY = Path("reports/20260701_183555_712209_domain_policy_vote_candidate_phase3_select_singleline_absorption_catalog_summary.json")
GETTER_SINGLE_CATALOG_SUMMARY = Path("reports/20260701_193849_800078_domain_policy_vote_candidate_phase3_dynamic_getter_light_absorption_catalog_summary.json")
GETTER_MEDIUM_CATALOG_SUMMARY = Path("reports/20260701_195222_820797_domain_policy_vote_candidate_phase3_dynamic_getter_medium_absorption_catalog_summary.json")
GETTER_DIAGNOSTIC_SUMMARY = Path("reports/20260701_183708_568780_domain_policy_vote_candidate_phase3_dynamic_getter_residual_diagnostic_summary.json")
EXPECTED_PHASE3_RESIDUAL = 1305


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only consolidation for phase 3 residual catalog/hold state.")
    parser.add_argument("--structural-summary", type=Path, default=STRUCTURAL_SUMMARY)
    parser.add_argument("--select-catalog-summary", type=Path, default=SELECT_CATALOG_SUMMARY)
    parser.add_argument("--getter-single-catalog-summary", type=Path, default=GETTER_SINGLE_CATALOG_SUMMARY)
    parser.add_argument("--getter-medium-catalog-summary", type=Path, default=GETTER_MEDIUM_CATALOG_SUMMARY)
    parser.add_argument("--getter-diagnostic-summary", type=Path, default=GETTER_DIAGNOSTIC_SUMMARY)
    parser.add_argument("--expected-phase3-residual", type=int, default=EXPECTED_PHASE3_RESIDUAL)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(db.project_path(path).read_text(encoding="utf-8"))


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def build(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    structural = read_json(args.structural_summary)
    select_catalog = read_json(args.select_catalog_summary)
    getter_single = read_json(args.getter_single_catalog_summary)
    getter_medium = read_json(args.getter_medium_catalog_summary)
    getter_diag = read_json(args.getter_diagnostic_summary)

    if int(structural.get("record_count") or 0) != args.expected_phase3_residual:
        raise SystemExit("phase3 residual count guard failed")
    for summary in (select_catalog, getter_single, getter_medium):
        for key in ("candidate_generation_count", "apply_count", "lifecycle_count", "segment_state_count", "reindex_count", "production_full_count"):
            if int(summary.get(key) or 0) != 0:
                raise SystemExit(f"{summary.get('source')} {key} guard failed")

    select_absorbed = int(select_catalog.get("record_count") or 0)
    getter_single_absorbed = int(getter_single.get("record_count") or 0)
    getter_medium_absorbed = int(getter_medium.get("absorbed_count") or 0)
    getter_medium_hold = int(getter_medium.get("hold_count") or 0)
    getter_diag_operational = getter_diag.get("operational_bucket_counts") or {}
    getter_context_hold = int(getter_diag_operational.get("hold_dense_or_complex_getter") or 0) + int(
        getter_diag_operational.get("hold_mixed_gender_or_select_getter") or 0
    )
    getter_multiline_hold = int(getter_diag_operational.get("hold_multiline_getter_parser_later") or 0)
    getter_light_hold = 8

    phase3_total = int(structural["record_count"])
    token_surface_counts = structural.get("token_surface_counts") or {}
    dynamic_select_total = int(token_surface_counts.get("dynamic_select") or 0)
    dynamic_getter_total = int(token_surface_counts.get("dynamic_getter") or 0)
    multiline_total = int(token_surface_counts.get("multiline") or 0)

    dynamic_select_hold = dynamic_select_total - select_absorbed
    dynamic_getter_absorbed = getter_single_absorbed + getter_medium_absorbed
    dynamic_getter_hold_or_later = dynamic_getter_total - dynamic_getter_absorbed
    catalog_absorbed_total = select_absorbed + dynamic_getter_absorbed
    unresolved_total = phase3_total - catalog_absorbed_total

    rows = [
        {
            "record_type": "catalog_absorbed",
            "family": "select_cstring_singleline",
            "count": select_absorbed,
            "recommended_state": "absorbed_by_existing_policy_catalog_only",
        },
        {
            "record_type": "catalog_absorbed",
            "family": "dynamic_getter_single_light",
            "count": getter_single_absorbed,
            "recommended_state": "absorbed_by_existing_policy_catalog_only",
        },
        {
            "record_type": "catalog_absorbed",
            "family": "dynamic_getter_medium_guard_ok",
            "count": getter_medium_absorbed,
            "recommended_state": "absorbed_by_existing_policy_catalog_only",
        },
        {
            "record_type": "hold",
            "family": "dynamic_getter_relation_possessive_ambiguity",
            "count": getter_medium_hold,
            "recommended_state": "architecture_or_hold_relation_possessive",
        },
        {
            "record_type": "hold",
            "family": "dynamic_getter_custom_generic_pronoun_context",
            "count": getter_light_hold,
            "recommended_state": "hold_context_or_parser_later",
        },
        {
            "record_type": "hold",
            "family": "dynamic_getter_dense_or_mixed",
            "count": getter_context_hold,
            "recommended_state": "hold_context_or_parser_later",
        },
        {
            "record_type": "hold",
            "family": "dynamic_getter_multiline",
            "count": getter_multiline_hold,
            "recommended_state": "terminal_hold_or_parser_later_candidate",
        },
        {
            "record_type": "hold",
            "family": "dynamic_select_remaining",
            "count": dynamic_select_hold,
            "recommended_state": "hold_multiline_multiple_select_or_gender_policy_later",
        },
        {
            "record_type": "hold",
            "family": "phase3_multiline_non_getter",
            "count": multiline_total,
            "recommended_state": "terminal_hold_or_parser_later_candidate",
        },
    ]

    record_counter = Counter(row["record_type"] for row in rows for _ in range(int(row["count"] or 0)))
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_phase3_residual_consolidation",
        "phase3_total": phase3_total,
        "catalog_absorbed_total": catalog_absorbed_total,
        "unresolved_total": unresolved_total,
        "record_type_counts": dict(record_counter.most_common()),
        "token_surface_counts": token_surface_counts,
        "dynamic_select_total": dynamic_select_total,
        "dynamic_select_absorbed": select_absorbed,
        "dynamic_select_hold": dynamic_select_hold,
        "dynamic_getter_total": dynamic_getter_total,
        "dynamic_getter_absorbed": dynamic_getter_absorbed,
        "dynamic_getter_hold_or_later": dynamic_getter_hold_or_later,
        "multiline_total": multiline_total,
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
            "Do not run lifecycle yet. The next best read-only step is terminal-hold classification for multiline/parser-later residuals, "
            "because that accounts for the largest unresolved block; relation/possessive can wait for a narrow architecture prompt."
        ),
    }
    return rows, summary


def write_reports(rows: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_phase3_residual_consolidation"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, rows)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Phase 3 residual consolidation",
        f"phase3_total={summary['phase3_total']}",
        f"catalog_absorbed_total={summary['catalog_absorbed_total']}",
        f"unresolved_total={summary['unresolved_total']}",
        "",
        "Rows:",
    ]
    for row in rows:
        lines.append(f"- {row['family']}: {row['count']} -> {row['recommended_state']}")
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
    print(f"catalog_absorbed_total={summary['catalog_absorbed_total']}")
    print(f"unresolved_total={summary['unresolved_total']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
