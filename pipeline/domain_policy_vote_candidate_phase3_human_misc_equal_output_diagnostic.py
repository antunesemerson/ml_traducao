from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from segment_state_snapshot import canonical_localization_text


SOURCE = "domain_policy_vote_candidate_phase3_human_misc_equal_output_diagnostic_v1"
INPUT_JSONL = Path("reports/20260701_150101_309805_domain_policy_vote_candidate_closure_debt_architecture_packet_512_531.jsonl")
CURRENT_RUN_ID = 531
EXPECTED_COUNT = 1997
PHASE = "phase_3_human_misc_equal_output_bridge"
SAFE_TOKEN_SURFACES = {"plain_text", "light_token"}
UNSAFE_TOKEN_SURFACES = {"dynamic_getter", "dynamic_select", "multiline"}
SAMPLE_LIMIT = 8


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


def canonical_equal(row: dict[str, Any]) -> bool:
    output_text = str(row.get("output_text") or "")
    confirmed_text = str(row.get("confirmed_text") or "")
    return output_text == confirmed_text or canonical_localization_text(output_text) == canonical_localization_text(
        confirmed_text
    )


def current_final_state(row: dict[str, Any]) -> str:
    return str(row.get("to_final_state") or "")


def classify_tranche(row: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if row.get("phase") != PHASE:
        reasons.append("not_phase3")
    if row.get("token_surface") not in SAFE_TOKEN_SURFACES:
        reasons.append("unsafe_token_surface")
    if int(row.get("open_issue_count") or 0) != 0:
        reasons.append("open_issue_count_not_0")
    if int(row.get("high_issue_count") or 0) != 0:
        reasons.append("high_issue_count_not_0")
    if int(row.get("confirmed_matches_output") or 0) != 1:
        reasons.append("confirmed_matches_output_not_1")
    if int(row.get("needs_output_apply") or 0) != 0:
        reasons.append("needs_output_apply_not_0")
    if not canonical_equal(row):
        reasons.append("canonical_output_confirmed_not_equal")
    if row.get("confirmation_level") != "human_confirmed":
        reasons.append("not_human_confirmed")
    if current_final_state(row) != "reopen_auto_confirmed_autofix":
        reasons.append("current_final_state_not_reopen_auto_confirmed_autofix")
    if reasons:
        return "phase3_hold_or_later", reasons
    return "phase3_first_tranche_plain_light_safe", []


def compact(row: dict[str, Any]) -> dict[str, Any]:
    tranche, reasons = classify_tranche(row)
    return {
        "source": SOURCE,
        "current_run_id": CURRENT_RUN_ID,
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "phase": row.get("phase"),
        "final_state_current": current_final_state(row),
        "classification": row.get("classification"),
        "confirmation_bucket": row.get("confirmation_bucket"),
        "confirmation_level": row.get("confirmation_level"),
        "confirmation_source": row.get("confirmation_source"),
        "confirmation_label": row.get("confirmation_label"),
        "locked": int(row.get("locked") or 0),
        "locked_bucket": "locked" if int(row.get("locked") or 0) == 1 else "unlocked",
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "high_issue_count": int(row.get("high_issue_count") or 0),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "canonical_output_equals_confirmed": canonical_equal(row),
        "token_surface": row.get("token_surface"),
        "first_tranche_recommendation": tranche,
        "first_tranche_block_reasons": reasons,
        "recommended_policy_name": "human_confirmed_misc_equal_output_plain_light_lifecycle_bridge"
        if tranche == "phase3_first_tranche_plain_light_safe"
        else None,
        "recommended_policy_action": "close_reopen_human_confirmed_misc_equal_output_plain_light_lifecycle"
        if tranche == "phase3_first_tranche_plain_light_safe"
        else None,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
    }


def build(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    focus = [row for row in rows if row.get("phase") == PHASE]
    records = [compact(row) for row in focus]
    token_surface_counts = Counter(record["token_surface"] for record in records)
    source_counts = Counter(record["confirmation_source"] for record in records)
    label_counts = Counter(record["confirmation_label"] for record in records)
    locked_counts = Counter(record["locked_bucket"] for record in records)
    level_counts = Counter(record["confirmation_level"] for record in records)
    final_state_counts = Counter(record["final_state_current"] for record in records)
    issue_counts = Counter(
        f"open={record['open_issue_count'] > 0};high={record['high_issue_count'] > 0}"
        for record in records
    )
    canonical_counts = Counter(str(record["canonical_output_equals_confirmed"]) for record in records)
    tranche_counts = Counter(record["first_tranche_recommendation"] for record in records)
    block_counts: Counter[str] = Counter()
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        reasons = record["first_tranche_block_reasons"] or ["safe_first_tranche"]
        for reason in reasons:
            block_counts[reason] += 1
            if len(samples[reason]) < SAMPLE_LIMIT:
                samples[reason].append(record)

    safe_records = [record for record in records if record["first_tranche_recommendation"] == "phase3_first_tranche_plain_light_safe"]
    safe_label_counts = Counter(record["confirmation_label"] for record in safe_records)
    safe_source_counts = Counter(record["confirmation_source"] for record in safe_records)
    safe_surface_counts = Counter(record["token_surface"] for record in safe_records)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "read_only_phase3_human_misc_equal_output_diagnostic",
        "input_jsonl": str(INPUT_JSONL),
        "current_run_id": CURRENT_RUN_ID,
        "record_count": len(records),
        "expected_record_count": EXPECTED_COUNT,
        "token_surface_counts": dict(sorted(token_surface_counts.items())),
        "confirmation_source_counts": dict(source_counts.most_common(40)),
        "confirmation_label_counts": dict(label_counts.most_common(60)),
        "locked_counts": dict(sorted(locked_counts.items())),
        "confirmation_level_counts": dict(sorted(level_counts.items())),
        "open_high_issue_presence_counts": dict(sorted(issue_counts.items())),
        "canonical_output_equals_confirmed_counts": dict(sorted(canonical_counts.items())),
        "final_state_counts": dict(sorted(final_state_counts.items())),
        "first_tranche_counts": dict(sorted(tranche_counts.items())),
        "first_tranche_block_reason_counts": dict(block_counts.most_common()),
        "first_tranche_safe_count": len(safe_records),
        "first_tranche_safe_token_surface_counts": dict(sorted(safe_surface_counts.items())),
        "first_tranche_safe_confirmation_source_counts": dict(safe_source_counts.most_common(30)),
        "first_tranche_safe_confirmation_label_counts": dict(safe_label_counts.most_common(40)),
        "excluded_dynamic_getter_count": token_surface_counts.get("dynamic_getter", 0),
        "excluded_dynamic_select_count": token_surface_counts.get("dynamic_select", 0),
        "excluded_multiline_count": token_surface_counts.get("multiline", 0),
        "samples_by_reason": samples,
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
            "Do not materialize a broad phase-3 bridge. First tranche should be a separate read-only dry-run for plain_text/light_token only, excluding dynamic_getter, dynamic_select and multiline."
        ),
        "recommended_first_tranche_policy": {
            "policy_name": "human_confirmed_misc_equal_output_plain_light_lifecycle_bridge",
            "policy_action": "close_reopen_human_confirmed_misc_equal_output_plain_light_lifecycle",
            "scope": "phase_3_human_misc_equal_output_bridge plain_text/light_token only",
            "allowed_token_surfaces": sorted(SAFE_TOKEN_SURFACES),
            "excluded_token_surfaces": sorted(UNSAFE_TOKEN_SURFACES),
            "guards": [
                "phase = phase_3_human_misc_equal_output_bridge",
                "token_surface in plain_text/light_token",
                "confirmation_level = human_confirmed",
                "confirmed_matches_output=1",
                "needs_output_apply=0",
                "canonical_l10n(output_text) == canonical_l10n(confirmed_text)",
                "open_issue_count=0",
                "high_issue_count=0",
                "final_state = reopen_auto_confirmed_autofix",
                "no source/output writes",
            ],
        },
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_phase3_human_misc_equal_output_diagnostic"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "Phase 3 human misc equal-output diagnostic",
        f"record_count={summary['record_count']}",
        f"first_tranche_safe_count={summary['first_tranche_safe_count']}",
        "",
        "Token surfaces:",
    ]
    for key, count in summary["token_surface_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "First tranche safe token surfaces:"])
    for key, count in summary["first_tranche_safe_token_surface_counts"].items():
        lines.append(f"- {key}: {count}")
    lines.extend(["", "Block reasons:"])
    for key, count in summary["first_tranche_block_reason_counts"].items():
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
    rows = read_jsonl(INPUT_JSONL)
    records, summary = build(rows)
    if summary["record_count"] != EXPECTED_COUNT:
        raise SystemExit(f"phase3 count guard failed: {summary['record_count']}")
    txt_path, jsonl_path, summary_path = write_reports(records, summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"first_tranche_safe_count={summary['first_tranche_safe_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
