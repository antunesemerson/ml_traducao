from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_phase3_plain_light_bridge_dry_run_v1"
INPUT_JSONL = Path("reports/20260701_152025_595314_domain_policy_vote_candidate_phase3_human_misc_equal_output_diagnostic.jsonl")
EXPECTED_COUNT = 635
POLICY_NAME = "human_confirmed_misc_equal_output_plain_light_lifecycle_bridge"
POLICY_ACTION = "close_reopen_human_confirmed_misc_equal_output_plain_light_lifecycle"
FINAL_STATE = "closed_auto_confirmed_human_confirmed_misc_equal_output_plain_light_lifecycle"
ALLOWED_SURFACES = {"plain_text", "light_token"}


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
    if row.get("first_tranche_recommendation") != "phase3_first_tranche_plain_light_safe":
        reasons.append("not_first_tranche_safe")
    if row.get("token_surface") not in ALLOWED_SURFACES:
        reasons.append("token_surface_not_plain_or_light")
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
    if row.get("canonical_output_equals_confirmed") is not True:
        reasons.append("canonical_output_confirmed_not_equal")
    if row.get("final_state_current") != "reopen_auto_confirmed_autofix":
        reasons.append("final_state_not_reopen_auto_confirmed_autofix")

    released = not reasons
    return {
        "source": SOURCE,
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "phase": row.get("phase"),
        "token_surface": row.get("token_surface"),
        "confirmation_level": row.get("confirmation_level"),
        "confirmation_source": row.get("confirmation_source"),
        "confirmation_label": row.get("confirmation_label"),
        "locked": int(row.get("locked") or 0),
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "high_issue_count": int(row.get("high_issue_count") or 0),
        "confirmed_matches_output": int(row.get("confirmed_matches_output") or 0),
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "canonical_output_equals_confirmed": bool(row.get("canonical_output_equals_confirmed")),
        "final_state_current": row.get("final_state_current"),
        "dry_run_decision": "released" if released else "blocked",
        "block_reasons": reasons,
        "recommended_policy_name": POLICY_NAME,
        "recommended_policy_action": POLICY_ACTION,
        "recommended_final_state": FINAL_STATE,
        "candidate_generation_allowed": False,
        "apply_allowed": False,
        "lifecycle_run_allowed_now": False,
    }


def build(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    focus = [row for row in rows if row.get("first_tranche_recommendation") == "phase3_first_tranche_plain_light_safe"]
    records = [evaluate(row) for row in focus]
    decisions = Counter(row["dry_run_decision"] for row in records)
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
        "mode": "read_only_phase3_plain_light_bridge_dry_run",
        "input_jsonl": str(INPUT_JSONL),
        "record_count": len(records),
        "expected_record_count": EXPECTED_COUNT,
        "released_count": decisions.get("released", 0),
        "blocked_count": decisions.get("blocked", 0),
        "consumer_supported_now_count": decisions.get("released", 0),
        "token_surface_counts": dict(sorted(Counter(row["token_surface"] for row in records).items())),
        "confirmation_source_counts": dict(Counter(row["confirmation_source"] for row in records).most_common(50)),
        "confirmation_label_counts": dict(Counter(row["confirmation_label"] for row in records).most_common(60)),
        "block_reason_counts": dict(block_counts.most_common()),
        "samples_by_reason": samples,
        "policy_name": POLICY_NAME,
        "policy_action": POLICY_ACTION,
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
    }
    return records, summary


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_phase3_plain_light_bridge_dry_run"
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
                "Phase3 plain/light bridge dry-run",
                f"record_count={summary['record_count']}",
                f"released_count={summary['released_count']}",
                f"blocked_count={summary['blocked_count']}",
                f"consumer_supported_now_count={summary['consumer_supported_now_count']}",
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
    rows = read_jsonl(INPUT_JSONL)
    records, summary = build(rows)
    if summary["record_count"] != EXPECTED_COUNT:
        raise SystemExit(f"record count guard failed: {summary['record_count']}")
    txt, jsonl, summary_path = write_reports(records, summary)
    print(f"txt={txt}")
    print(f"jsonl={jsonl}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"released_count={summary['released_count']}")
    print(f"blocked_count={summary['blocked_count']}")
    print(f"consumer_supported_now_count={summary['consumer_supported_now_count']}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("lifecycle_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")


if __name__ == "__main__":
    main()
