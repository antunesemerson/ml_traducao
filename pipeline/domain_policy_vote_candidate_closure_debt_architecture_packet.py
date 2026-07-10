from __future__ import annotations

import json
import argparse
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_closure_debt_architecture_packet_v1"
DIAGNOSTIC_JSONL = Path("reports/20260701_012505_977440_domain_policy_vote_candidate_closure_debt_diagnostic_512_526.jsonl")
DIAGNOSTIC_SUMMARY = Path("reports/20260701_012505_977440_domain_policy_vote_candidate_closure_debt_diagnostic_512_526_summary.json")
FROM_RUN_ID = 512
TO_RUN_ID = 526
SAMPLE_LIMIT_PER_FAMILY = 8


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only closure debt architecture packet.")
    parser.add_argument("--diagnostic-jsonl", type=Path, default=DIAGNOSTIC_JSONL)
    parser.add_argument("--diagnostic-summary", type=Path, default=DIAGNOSTIC_SUMMARY)
    parser.add_argument("--from-run-id", type=int, default=FROM_RUN_ID)
    parser.add_argument("--to-run-id", type=int, default=TO_RUN_ID)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


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


def phase_for(record: dict[str, Any]) -> str:
    classification = str(record.get("closure_debt_classification") or "")
    family = str(record.get("recommended_bridge_or_policy_family") or "")
    label = str(record.get("confirmation_label") or "")
    if classification == "human_locked_or_confirmed_output_equal_no_open_issue":
        if label in {"package_close", "codex_package_review"}:
            return "phase_1_human_package_close_bridge"
        if "token_policy_confirmed_text_fixed" in label or "strict_mojibake_fixed" in label:
            return "phase_2_human_repair_label_bridge"
        return "phase_3_human_misc_equal_output_bridge"
    if classification == "auto_confirmed_trusted_surface_output_equal":
        return "phase_4_auto_confirmed_plain_or_light_bridge"
    if classification == "lifecycle_policy_allowed_but_not_closed_current":
        return "debug_existing_policy_consumption"
    if family.startswith("hold::auto_confirmed::multiline") or family.startswith("hold::auto_confirmed::dynamic_getter"):
        return "hold_auto_structural_surface"
    return "hold_policy_absent_or_issue_risk"


def architecture_decision_for(record: dict[str, Any]) -> str:
    classification = str(record.get("closure_debt_classification") or "")
    phase = phase_for(record)
    if phase == "debug_existing_policy_consumption":
        return "architecture_debug_segment_state_policy_consumption"
    if classification in {
        "human_locked_or_confirmed_output_equal_no_open_issue",
        "auto_confirmed_trusted_surface_output_equal",
    }:
        return "architecture_can_materialize_lifecycle_bridge_after_policy_review"
    return "architecture_hold_no_lifecycle_bridge_now"


def compact_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "segment_id": record.get("segment_id"),
        "relative_path": record.get("relative_path"),
        "source_key": record.get("source_key"),
        "from_final_state": record.get("from_final_state"),
        "to_final_state": record.get("to_final_state"),
        "classification": record.get("closure_debt_classification"),
        "phase": phase_for(record),
        "recommended_bridge_or_policy_family": record.get("recommended_bridge_or_policy_family"),
        "confirmation_bucket": record.get("confirmation_bucket"),
        "confirmation_level": record.get("confirmation_level"),
        "confirmation_source": record.get("confirmation_source"),
        "confirmation_label": record.get("confirmation_label"),
        "locked": record.get("locked"),
        "token_surface": record.get("token_surface"),
        "open_issue_count": record.get("open_issue_count"),
        "high_issue_count": record.get("high_issue_count"),
        "lifecycle_policy_allowed": record.get("lifecycle_policy_allowed"),
        "lifecycle_policy_action": record.get("lifecycle_policy_action"),
        "confirmed_matches_output": record.get("confirmed_matches_output"),
        "needs_output_apply": record.get("needs_output_apply"),
        "architecture_decision": architecture_decision_for(record),
        "output_text": record.get("output_text"),
        "confirmed_text": record.get("confirmed_text"),
    }


def build_packet(
    rows: list[dict[str, Any]],
    diagnostic_summary: dict[str, Any],
    diagnostic_jsonl: Path,
    diagnostic_summary_path: Path,
    from_run_id: int,
    to_run_id: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records: list[dict[str, Any]] = []
    samples_by_phase: dict[str, list[dict[str, Any]]] = defaultdict(list)
    samples_by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in rows:
        phase = phase_for(record)
        compact = compact_record(record)
        records.append(compact)
        if len(samples_by_phase[phase]) < SAMPLE_LIMIT_PER_FAMILY:
            samples_by_phase[phase].append(compact)
        family = str(compact["recommended_bridge_or_policy_family"])
        if len(samples_by_family[family]) < 3:
            samples_by_family[family].append(compact)

    phase_counts = Counter(record["phase"] for record in records)
    classification_counts = Counter(record["classification"] for record in records)
    family_counts = Counter(record["recommended_bridge_or_policy_family"] for record in records)
    label_counts = Counter(
        f"{record['confirmation_bucket']}::{record['confirmation_label']}" for record in records
    )
    decision_counts = Counter(record["architecture_decision"] for record in records)
    if phase_counts.get("phase_1_human_package_close_bridge", 0) > 0:
        single_recommendation = (
            "Review/materialize phase-1 human equal-output bridge first, and debug existing lifecycle_policy_allowed rows separately."
        )
    elif phase_counts.get("phase_2_human_repair_label_bridge", 0) > 0:
        single_recommendation = (
            "Phase 1 is drained. Next safe step is a read-only dry-run for phase_2_human_repair_label_bridge, keeping debug_existing_policy_consumption and auto-confirmed phase 4 out of scope."
        )
    elif phase_counts.get("debug_existing_policy_consumption", 0) > 0:
        single_recommendation = (
            "No phase-1/phase-2 bridge remains. Next safe step is read-only debug of existing lifecycle policy consumption."
        )
    else:
        single_recommendation = "No lifecycle bridge should be materialized from this packet; keep holds and reassess separately."

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_architecture_packet",
        "diagnostic_jsonl": str(diagnostic_jsonl),
        "diagnostic_summary": str(diagnostic_summary_path),
        "from_run_id": from_run_id,
        "to_run_id": to_run_id,
        "record_count": len(records),
        "diagnostic_record_count": int(diagnostic_summary.get("record_count") or 0),
        "classification_counts": dict(sorted(classification_counts.items())),
        "phase_counts": dict(sorted(phase_counts.items())),
        "architecture_decision_counts": dict(sorted(decision_counts.items())),
        "top_bridge_or_policy_families": dict(family_counts.most_common(40)),
        "top_confirmation_bucket_labels": dict(label_counts.most_common(40)),
        "phase_samples": samples_by_phase,
        "family_samples": dict(list(samples_by_family.items())[:80]),
        "recommended_policy_plan": [
            {
                "phase": "phase_1_human_package_close_bridge",
                "count": phase_counts.get("phase_1_human_package_close_bridge", 0),
                "recommendation": "Materialize a narrow human equal-output lifecycle bridge for package_close and codex_package_review labels first.",
                "guards": [
                    "confirmed_matches_output=1",
                    "needs_output_apply=0",
                    "output_text == confirmed_text",
                    "open_issue_count=0",
                    "human_confirmed or locked human signal",
                    "no source/output writes",
                ],
            },
            {
                "phase": "phase_2_human_repair_label_bridge",
                "count": phase_counts.get("phase_2_human_repair_label_bridge", 0),
                "recommendation": "Materialize separately for repaired labels such as token_policy_confirmed_text_fixed and strict_mojibake_fixed.",
                "guards": [
                    "same equal-output guards as phase 1",
                    "explicit repaired-label allowlist",
                    "no automatic expansion to structural holds",
                ],
            },
            {
                "phase": "debug_existing_policy_consumption",
                "count": phase_counts.get("debug_existing_policy_consumption", 0),
                "recommendation": "Investigate why lifecycle_policy_allowed=1 did not close before releasing new broad bridges.",
                "guards": ["read-only debug first", "do not rerun lifecycle blindly"],
            },
            {
                "phase": "phase_4_auto_confirmed_plain_or_light_bridge",
                "count": phase_counts.get("phase_4_auto_confirmed_plain_or_light_bridge", 0),
                "recommendation": "Consider only after human phases, restricted to auto-confirmed plain/light-token equal-output rows.",
                "guards": ["plain_text or light_token only", "open_issue_count=0", "source confidence allowlist"],
            },
        ],
        "hold_policy": {
            "hold_count": sum(count for phase, count in phase_counts.items() if phase.startswith("hold_")),
            "hold_phases": {phase: count for phase, count in sorted(phase_counts.items()) if phase.startswith("hold_")},
            "recommendation": "Do not materialize lifecycle for structural, dynamic, multiline, issue-bearing or weak-source holds.",
        },
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": single_recommendation,
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_closure_debt_architecture_packet_{summary['from_run_id']}_{summary['to_run_id']}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Domain policy vote candidate closure debt architecture packet",
        f"from_run_id={summary['from_run_id']}",
        f"to_run_id={summary['to_run_id']}",
        f"record_count={summary['record_count']}",
        "",
        "Phase counts:",
    ]
    for key, count in summary["phase_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Architecture decision counts:"])
    for key, count in summary["architecture_decision_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Recommended plan:"])
    for item in summary["recommended_policy_plan"]:
        lines.append(f"- {item['phase']}: {item['count']} | {item['recommendation']}")
    lines.extend(
        [
            "",
            "Hold policy:",
            f"- hold_count: {summary['hold_policy']['hold_count']}",
            f"- recommendation: {summary['hold_policy']['recommendation']}",
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
            "Single recommendation:",
            summary["single_operational_recommendation"],
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    args = parse_args()
    diagnostic_summary = read_json(args.diagnostic_summary)
    rows = read_jsonl(args.diagnostic_jsonl)
    if len(rows) != int(diagnostic_summary.get("record_count") or 0):
        raise SystemExit("diagnostic row count guard failed")
    records, summary = build_packet(
        rows,
        diagnostic_summary,
        args.diagnostic_jsonl,
        args.diagnostic_summary,
        args.from_run_id,
        args.to_run_id,
    )
    txt_path, jsonl_path, summary_path = write_reports(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
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
