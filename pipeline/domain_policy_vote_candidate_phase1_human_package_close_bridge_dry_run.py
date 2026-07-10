from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from segment_state_snapshot import canonical_localization_text


SOURCE = "domain_policy_vote_candidate_phase1_human_package_close_bridge_dry_run_v1"
INPUT_JSONL = Path("reports/20260701_020828_439617_domain_policy_vote_candidate_closure_debt_architecture_packet.jsonl")
EXPECTED_COUNT = 1560
ALLOWED_PHASE = "phase_1_human_package_close_bridge"
ALLOWED_LABELS = {"codex_package_review", "package_close"}
EXISTING_CONSUMER_ALLOWED_SOURCES = {
    "codex_manual",
    "codex_manual+manual_mojibake_cleanup",
    "codex_review",
    "codex_review+manual_mojibake_cleanup",
    "codex_review+manual_mojibake_cleanup+context_mojibake_cleanup",
    "codex_review+manual_mojibake_cleanup+shadow_hygiene_cleanup",
}
EXISTING_CONSUMER_ALLOWED_LABELS = ALLOWED_LABELS


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


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


def output_equals_confirmed(row: dict[str, Any]) -> bool:
    output_text = str(row.get("output_text") or "")
    confirmed_text = str(row.get("confirmed_text") or "")
    return output_text == confirmed_text or canonical_localization_text(output_text) == canonical_localization_text(confirmed_text)


def evaluate(row: dict[str, Any]) -> dict[str, Any]:
    block_reasons: list[str] = []
    if row.get("phase") != ALLOWED_PHASE:
        block_reasons.append("excluded_not_phase_1")
    if row.get("phase") == "debug_existing_policy_consumption":
        block_reasons.append("excluded_debug_existing_policy_consumption")
    if row.get("phase") in {"phase_2_human_repair_label_bridge", "phase_3_human_misc_equal_output_bridge", "phase_4_auto_confirmed_plain_or_light_bridge"}:
        block_reasons.append("excluded_other_phase")
    if str(row.get("confirmation_bucket") or "").startswith("auto_confirmed") or row.get("confirmation_level") == "auto_confirmed":
        block_reasons.append("excluded_auto_confirmed")
    if int(row.get("open_issue_count") or 0) > 0:
        block_reasons.append("open_issue_count_gt_0")
    if int(row.get("high_issue_count") or 0) > 0:
        block_reasons.append("high_issue_count_gt_0")
    if int(row.get("needs_output_apply") or 0) != 0:
        block_reasons.append("needs_output_apply_not_0")
    if int(row.get("confirmed_matches_output") or 0) != 1:
        block_reasons.append("confirmed_matches_output_not_1")
    if not output_equals_confirmed(row):
        block_reasons.append("output_not_equal_confirmed_even_canonical")
    if row.get("confirmation_label") not in ALLOWED_LABELS:
        block_reasons.append("confirmation_label_not_phase1_allowed")
    if row.get("confirmation_level") != "human_confirmed" and row.get("confirmation_bucket") not in {"human_locked", "human_confirmed_unlocked"}:
        block_reasons.append("not_human_confirmed_or_human_locked")
    if str(row.get("phase") or "").startswith("hold_") or str(row.get("classification") or "").startswith("hold_"):
        block_reasons.append("excluded_hold")

    phase_safe = not block_reasons
    existing_consumer_supported = (
        row.get("confirmation_source") in EXISTING_CONSUMER_ALLOWED_SOURCES
        and row.get("confirmation_label") in EXISTING_CONSUMER_ALLOWED_LABELS
    )
    consumer_block_reasons: list[str] = []
    if phase_safe and not existing_consumer_supported:
        consumer_block_reasons.append("segment_state_existing_human_confirmed_post_apply_fast_path_does_not_accept_phase1_source_label")

    return {
        "source": SOURCE,
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "phase": row.get("phase"),
        "classification": row.get("classification"),
        "confirmation_bucket": row.get("confirmation_bucket"),
        "confirmation_level": row.get("confirmation_level"),
        "confirmation_source": row.get("confirmation_source"),
        "confirmation_label": row.get("confirmation_label"),
        "locked": int(row.get("locked") or 0),
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "high_issue_count": int(row.get("high_issue_count") or 0),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "output_equals_confirmed_canonical": output_equals_confirmed(row),
        "phase1_safety_eligible": phase_safe,
        "segment_state_consumer_supported_now": existing_consumer_supported,
        "dry_run_decision": "released" if phase_safe and existing_consumer_supported else "blocked",
        "block_reasons": block_reasons + consumer_block_reasons,
        "recommended_policy_name": "human_confirmed_package_close_lifecycle_bridge",
        "recommended_policy_action": "close_reopen_human_confirmed_package_close_lifecycle",
        "requires_architecture_change_before_apply": phase_safe and not existing_consumer_supported,
        "output_text": row.get("output_text"),
        "confirmed_text": row.get("confirmed_text"),
    }


def build(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    phase_rows = [row for row in rows if row.get("phase") == ALLOWED_PHASE]
    records = [evaluate(row) for row in phase_rows]
    decision_counts = Counter(record["dry_run_decision"] for record in records)
    block_counts: Counter[str] = Counter()
    label_counts = Counter(str(record["confirmation_label"]) for record in records)
    source_counts = Counter(str(record["confirmation_source"]) for record in records)
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        reasons = record["block_reasons"] or ["released"]
        for reason in reasons:
            block_counts[reason] += 1
            if len(samples[reason]) < 8:
                samples[reason].append(record)
    apply_policy_bridge_allowed_now = (
        decision_counts.get("released", 0) == EXPECTED_COUNT and decision_counts.get("blocked", 0) == 0
    )
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_phase1_human_package_close_bridge_dry_run",
        "input_jsonl": str(INPUT_JSONL),
        "record_count": len(records),
        "expected_record_count": EXPECTED_COUNT,
        "phase1_safety_eligible_count": sum(1 for record in records if record["phase1_safety_eligible"]),
        "consumer_supported_now_count": sum(1 for record in records if record["segment_state_consumer_supported_now"]),
        "released_count": decision_counts.get("released", 0),
        "blocked_count": decision_counts.get("blocked", 0),
        "decision_counts": dict(sorted(decision_counts.items())),
        "block_reason_counts": dict(block_counts.most_common()),
        "confirmation_label_counts": dict(sorted(label_counts.items())),
        "confirmation_source_counts": dict(sorted(source_counts.items())),
        "samples_by_reason": samples,
        "apply_policy_bridge_allowed_now": apply_policy_bridge_allowed_now,
        "architecture_change_required": decision_counts.get("blocked", 0) > 0,
        "required_architecture_adjustment": {
            "summary": "Add a dedicated phase-1 human package close lifecycle bridge consumer, or extend the existing human-confirmed post-apply fast path to allow phase1 sources/labels under strict equal-output/no-issue guards.",
            "allowed_phase": ALLOWED_PHASE,
            "allowed_labels": sorted(ALLOWED_LABELS),
            "observed_sources": dict(sorted(source_counts.items())),
            "recommended_policy_name": "human_confirmed_package_close_lifecycle_bridge",
            "recommended_policy_action": "close_reopen_human_confirmed_package_close_lifecycle",
            "recommended_final_state": "closed_auto_confirmed_human_confirmed_package_close_lifecycle",
            "guards": [
                "phase = phase_1_human_package_close_bridge",
                "confirmation_level = human_confirmed or human_locked signal",
                "confirmation_label in codex_package_review/package_close",
                "confirmed_matches_output=1",
                "needs_output_apply=0",
                "canonical_l10n(output_text) == canonical_l10n(confirmed_text)",
                "open_issue_count=0",
                "high_issue_count=0",
                "no source/output writes",
            ],
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
        "single_operational_recommendation": (
            "Materialize only the human_confirmed_package_close_lifecycle_bridge policy, then run only segment-state and a delta report."
            if apply_policy_bridge_allowed_now
            else "Do not apply the bridge yet. Send the required architecture adjustment, then rerun this dry-run expecting 1560 released / 0 blocked."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_phase1_human_package_close_bridge_dry_run"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Phase 1 human package close bridge dry-run",
        f"record_count={summary['record_count']}",
        f"phase1_safety_eligible_count={summary['phase1_safety_eligible_count']}",
        f"consumer_supported_now_count={summary['consumer_supported_now_count']}",
        f"released_count={summary['released_count']}",
        f"blocked_count={summary['blocked_count']}",
        "",
        "Block reasons:",
    ]
    for key, count in summary["block_reason_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(
        [
            "",
            "Architecture / policy note:",
            (
                "Existing consumer supports this phase under the strict no-issue/equal-output guards."
                if summary["apply_policy_bridge_allowed_now"]
                else json.dumps(summary["required_architecture_adjustment"], ensure_ascii=False, sort_keys=True)
            ),
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
    rows = read_jsonl(INPUT_JSONL)
    records, summary = build(rows)
    if summary["record_count"] != EXPECTED_COUNT:
        raise SystemExit(f"phase1 count guard failed: {summary['record_count']}")
    txt_path, jsonl_path, summary_path = write_reports(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
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
