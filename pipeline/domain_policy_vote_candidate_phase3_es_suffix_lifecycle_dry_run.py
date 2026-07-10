from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_phase3_es_suffix_lifecycle_dry_run_v1"
INPUT_JSONL = Path("reports/20260701_162621_786127_domain_policy_vote_candidate_phase3_gender_suffix_review.jsonl")
EXPECTED_COUNT = 57
POLICY_NAME = "human_confirmed_misc_equal_output_es_suffix_lifecycle_bridge"
POLICY_ACTION = "close_reopen_human_confirmed_misc_equal_output_es_suffix_lifecycle"
FINAL_STATE = "closed_auto_confirmed_human_confirmed_misc_equal_output_es_suffix_lifecycle"


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


def evaluate(row: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    if row.get("parser_readiness") != "candidate_for_es_suffix_lifecycle_policy":
        reasons.append("not_es_suffix_candidate")
    if row.get("token_surface") != "dynamic_getter":
        reasons.append("not_dynamic_getter")
    if row.get("confirmation_level") != "human_confirmed":
        reasons.append("not_human_confirmed")
    if int(row.get("open_issue_count") or 0) != 0:
        reasons.append("open_issue_count_not_0")
    if int(row.get("high_issue_count") or 0) != 0:
        reasons.append("high_issue_count_not_0")
    if int(row.get("confirmed_matches_output") or 0) != 1:
        reasons.append("confirmed_matches_output_not_1")
    if int(row.get("needs_output_apply") or 0) != 0:
        reasons.append("needs_output_apply_not_0")
    if row.get("final_state_current") != "reopen_auto_confirmed_autofix":
        reasons.append("final_state_not_reopen_auto_confirmed_autofix")
    if row.get("surrounding_pattern") in {"contains_newline", "effect_list", "has_select_localization"}:
        reasons.append("excluded_context_surface")
    if row.get("es_token_context") == "multiple_es_token_types":
        reasons.append("multiple_es_token_types")

    safety_eligible = not reasons
    consumer_supported_now = safety_eligible

    return {
        "source": SOURCE,
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "phase": row.get("phase"),
        "token_surface": row.get("token_surface"),
        "es_token_types": row.get("es_token_types"),
        "es_token_context": row.get("es_token_context"),
        "surrounding_pattern": row.get("surrounding_pattern"),
        "confirmation_level": row.get("confirmation_level"),
        "confirmation_source": row.get("confirmation_source"),
        "confirmation_label": row.get("confirmation_label"),
        "locked": int(row.get("locked") or 0),
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "high_issue_count": int(row.get("high_issue_count") or 0),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "final_state_current": row.get("final_state_current"),
        "safety_eligible": safety_eligible,
        "segment_state_consumer_supported_now": consumer_supported_now,
        "dry_run_decision": "released" if safety_eligible and consumer_supported_now else "blocked",
        "block_reasons": reasons,
        "recommended_policy_name": POLICY_NAME,
        "recommended_policy_action": POLICY_ACTION,
        "recommended_final_state": FINAL_STATE,
        "candidate_generation_allowed": False,
        "apply_allowed": False,
        "lifecycle_run_allowed_now": False,
    }


def build(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    focus = [row for row in rows if row.get("parser_readiness") == "candidate_for_es_suffix_lifecycle_policy"]
    records = [evaluate(row) for row in focus]
    decisions = Counter(record["dry_run_decision"] for record in records)
    block_counts: Counter[str] = Counter()
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        for reason in record["block_reasons"] or ["released"]:
            block_counts[reason] += 1
            if len(samples[reason]) < 8:
                samples[reason].append(record)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_phase3_es_suffix_lifecycle_dry_run",
        "input_jsonl": str(INPUT_JSONL),
        "record_count": len(records),
        "expected_record_count": EXPECTED_COUNT,
        "safety_eligible_count": sum(1 for record in records if record["safety_eligible"]),
        "consumer_supported_now_count": sum(1 for record in records if record["segment_state_consumer_supported_now"]),
        "released_count": decisions.get("released", 0),
        "blocked_count": decisions.get("blocked", 0),
        "blocked_only_by_missing_consumer_count": sum(
            1
            for record in records
            if record["safety_eligible"]
            and record["block_reasons"] == ["segment_state_consumer_missing_for_es_suffix_lifecycle"]
        ),
        "block_reason_counts": dict(block_counts.most_common()),
        "es_token_context_counts": dict(Counter(str(record["es_token_context"]) for record in records)),
        "surrounding_pattern_counts": dict(Counter(str(record["surrounding_pattern"]) for record in records)),
        "confirmation_source_counts": dict(Counter(str(record["confirmation_source"]) for record in records).most_common(40)),
        "confirmation_label_counts": dict(Counter(str(record["confirmation_label"]) for record in records).most_common(40)),
        "samples_by_reason": samples,
        "architecture_change_required": True,
        "required_architecture_adjustment": {
            "summary": "Add a dedicated phase-3 ES suffix lifecycle consumer under strict equal-output/no-issue guards.",
            "recommended_policy_name": POLICY_NAME,
            "recommended_policy_action": POLICY_ACTION,
            "recommended_final_state": FINAL_STATE,
            "guards": [
                "parser_readiness = candidate_for_es_suffix_lifecycle_policy",
                "phase = phase_3_human_misc_equal_output_bridge",
                "token_surface = dynamic_getter",
                "confirmation_level = human_confirmed",
                "confirmed_matches_output=1",
                "needs_output_apply=0",
                "canonical_l10n(output_text) == canonical_l10n(confirmed_text)",
                "open_issue_count=0",
                "high_issue_count=0",
                "final_state = reopen_auto_confirmed_autofix",
                "exclude multiline, effect_list, SelectLocalization/Select_CString, mixed ES token types and missing ES token",
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
            "Send this dry-run to architecture. Do not materialize policy until segment-state supports the dedicated ES suffix lifecycle consumer."
        ),
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_phase3_es_suffix_lifecycle_dry_run"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                "Phase3 ES suffix lifecycle dry-run",
                f"record_count={summary['record_count']}",
                f"safety_eligible_count={summary['safety_eligible_count']}",
                f"consumer_supported_now_count={summary['consumer_supported_now_count']}",
                f"released_count={summary['released_count']}",
                f"blocked_count={summary['blocked_count']}",
                f"blocked_only_by_missing_consumer_count={summary['blocked_only_by_missing_consumer_count']}",
                "candidate_generation_count=0",
                "apply_count=0",
                "lifecycle_count=0",
                "segment_state_count=0",
                "reindex_count=0",
                "production_full_count=0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return txt_path, jsonl_path, summary_path


def main() -> None:
    records, summary = build(read_jsonl(INPUT_JSONL))
    if summary["record_count"] != EXPECTED_COUNT:
        raise SystemExit(f"record count guard failed: {summary['record_count']}")
    txt, jsonl, summary_path = write_reports(records, summary)
    print(f"txt={txt}")
    print(f"jsonl={jsonl}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"safety_eligible_count={summary['safety_eligible_count']}")
    print(f"consumer_supported_now_count={summary['consumer_supported_now_count']}")
    print(f"released_count={summary['released_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"blocked_only_by_missing_consumer_count={summary['blocked_only_by_missing_consumer_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
