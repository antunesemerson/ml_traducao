from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import canonical_localization_text, structural_tokens


SOURCE = "release_promotion_remaining_review_readonly_v1"
APPROVED_ISSUE_REVIEW_IDS = {
    132952, 76568, 283804, 97799, 159147, 230093, 160085, 230057,
    229225, 113813, 50820, 229255, 159160, 70407, 30717, 229889,
    74000, 229357, 229897, 155532, 229271, 34003, 63243, 56101,
    287886, 229955,
}
TOKEN_RECHECK_ID = 109951
CONTEXT_FALSE_HOLD_IDS = {57298}
PARTIAL_REPAIR_HOLD_IDS = {58593, 58592, 229510, 30555, 30553, 30554, 30556, 30557}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review the remaining audited release promotions.")
    parser.add_argument("--audit-jsonl", type=Path, required=True)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def latest_state_run_id() -> int:
    settings = db.load_settings()
    database_path = db.get_database_path(settings)
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as conn:
        row = conn.execute(
            """
            SELECT id FROM segment_state_runs
            WHERE finished_at IS NOT NULL AND total_segments > 1000
            ORDER BY finished_at DESC, id DESC LIMIT 1
            """
        ).fetchone()
    if not row:
        raise SystemExit("No completed segment-state run found.")
    return int(row[0])


def current_states(state_run_id: int, segment_ids: set[int]) -> dict[int, str]:
    settings = db.load_settings()
    database_path = db.get_database_path(settings)
    placeholders = ",".join("?" for _ in segment_ids)
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            f"SELECT segment_id, final_state FROM segment_state_items WHERE run_id=? AND segment_id IN ({placeholders})",
            [state_run_id, *sorted(segment_ids)],
        ).fetchall()
    return {int(row[0]): str(row[1]) for row in rows}


def review(row: dict[str, Any], state_run_id: int, current_state: str | None) -> dict[str, Any]:
    segment_id = int(row["segment_id"])
    record = dict(row)
    record["source"] = SOURCE
    record["record_type"] = "release_promotion_remaining_review"
    record["segment_state_run_id"] = state_run_id
    record["reviewer"] = "codex_architecture_review"
    record["reviewed_at"] = datetime.now().isoformat(timespec="seconds")
    record["apply_count"] = 0
    record["output_changed"] = False
    record["current_final_state"] = current_state

    if current_state and current_state.startswith("closed_"):
        record.update(
            {
                "review_decision": "already_closed_prior_cohort",
                "review_reason": "Segment was closed by an earlier validated cohort.",
                "eligible_for_controlled_validation": False,
                "readiness": "already_closed",
            }
        )
        return record

    if segment_id in CONTEXT_FALSE_HOLD_IDS:
        record.update(
            {
                "lane": "reviewed_context_false_hold",
                "review_decision": "approve_current_output_for_lifecycle",
                "review_reason": (
                    "English and PT-BR context confirm a complete conditional outcome: if successful, the actor gains the target."
                ),
                "eligible_for_controlled_validation": True,
                "readiness": "ready_for_lifecycle_dry_run",
                "token_status": "ok",
            }
        )
    elif segment_id in APPROVED_ISSUE_REVIEW_IDS:
        record.update(
            {
                "lane": "reviewed_score_issue_superseded",
                "review_decision": "approve_current_output_for_lifecycle",
                "review_reason": (
                    "Current output resolves the visible Spanish, agreement, auxiliary, possessive or conjugation defect; "
                    "the score-run issue signal is historical and the current ledger has no open issue."
                ),
                "eligible_for_controlled_validation": True,
                "readiness": "ready_for_lifecycle_dry_run",
                "token_status": "ok",
            }
        )
    elif segment_id == TOKEN_RECHECK_ID:
        old_text = str(row.get("old_text") or "")
        output_text = str(row.get("output_text") or "")
        token_equal = structural_tokens(old_text) == structural_tokens(output_text)
        canonical_changed = canonical_localization_text(old_text) != canonical_localization_text(output_text)
        approved = token_equal and canonical_changed
        record.update(
            {
                "lane": "reviewed_token_false_mismatch" if approved else "hold_token_signature",
                "review_decision": (
                    "approve_current_output_for_lifecycle" if approved else "hold_token_parser_review"
                ),
                "review_reason": (
                    "Current structural-token parser confirms an identical signature; only the PT-BR accent repair changes."
                    if approved
                    else "Current structural-token parser still detects a material mismatch."
                ),
                "eligible_for_controlled_validation": approved,
                "readiness": "ready_for_lifecycle_dry_run" if approved else "hold",
                "token_status": "ok" if approved else "mismatch",
                "review_token_signature_equal": token_equal,
            }
        )
    elif segment_id in PARTIAL_REPAIR_HOLD_IDS:
        record.update(
            {
                "review_decision": "hold_partial_repair",
                "review_reason": row.get("next_action"),
                "eligible_for_controlled_validation": False,
                "readiness": "hold_human_correction",
            }
        )
    else:
        record.update(
            {
                "review_decision": "hold_unclassified",
                "review_reason": "Record was not part of the explicit reviewed allowlist.",
                "eligible_for_controlled_validation": False,
                "readiness": "hold",
            }
        )
    return record


def main() -> int:
    args = parse_args()
    rows = [row for row in read_jsonl(args.audit_jsonl) if not row.get("eligible_for_controlled_validation")]
    expected_ids = APPROVED_ISSUE_REVIEW_IDS | {TOKEN_RECHECK_ID} | CONTEXT_FALSE_HOLD_IDS | PARTIAL_REPAIR_HOLD_IDS
    actual_ids = {int(row["segment_id"]) for row in rows}
    if actual_ids != expected_ids:
        raise SystemExit(
            f"Remaining-set mismatch: missing={sorted(expected_ids-actual_ids)}, extra={sorted(actual_ids-expected_ids)}"
        )
    state_run_id = latest_state_run_id()
    states = current_states(state_run_id, actual_ids)
    records = [review(row, state_run_id, states.get(int(row["segment_id"]))) for row in rows]
    decisions = Counter(record["review_decision"] for record in records)
    ready = [record for record in records if record["eligible_for_controlled_validation"]]
    held = [
        record
        for record in records
        if str(record["review_decision"]).startswith("hold_")
    ]
    already_closed = [
        record
        for record in records
        if record["review_decision"] == "already_closed_prior_cohort"
    ]

    reports = db.project_path(db.load_settings()["reports_dir"])
    reports.mkdir(parents=True, exist_ok=True)
    base = reports / f"{stamp()}_release_promotion_remaining_review_readonly"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "segment_state_run_id": state_run_id,
        "record_count": len(records),
        "ready_count": len(ready),
        "hold_count": len(held),
        "already_closed_count": len(already_closed),
        "decision_counts": dict(decisions),
        "ready_segment_ids": [record["segment_id"] for record in ready],
        "hold_segment_ids": [record["segment_id"] for record in held],
        "already_closed_segment_ids": [record["segment_id"] for record in already_closed],
        "apply_count": 0,
        "source_changed": False,
        "output_changed": False,
    }
    lines = [
        "# Remaining Release Promotion Review",
        "",
        f"- segment-state: `{state_run_id}`",
        f"- reviewed: `{len(records)}`",
        f"- ready for controlled validation: `{len(ready)}`",
        f"- already closed in prior cohorts: `{len(already_closed)}`",
        f"- hold partial repair: `{len(held)}`",
        "- source/output changed: `false`",
        "",
        "| ID | lane | decision | reason |",
        "|---:|---|---|---|",
    ]
    for record in records:
        lines.append(
            f"| {record['segment_id']} | `{record['lane']}` | `{record['review_decision']}` | {record['review_reason']} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"summary": summary, "artifacts": [str(md_path), str(jsonl_path), str(summary_path)]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
