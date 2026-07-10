from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_accolade_formula_run517_batch2_decisions_v1"
INPUT_JSONL = Path(
    "reports/20260630_164454_238945_domain_policy_vote_candidate_pending_apply_run515_token_safe_diff_preview.jsonl"
)
SEGMENT_STATE_RUN_ID = 517
APPROVED_SEGMENT_IDS = {629, 669}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> None:
    input_rows = read_jsonl(INPUT_JSONL)
    selected = [row for row in input_rows if int(row["segment_id"]) in APPROVED_SEGMENT_IDS]
    if {int(row["segment_id"]) for row in selected} != APPROVED_SEGMENT_IDS:
        raise SystemExit("approved segment id guard failed")

    decisions: list[dict[str, Any]] = []
    for row in selected:
        decisions.append(
            {
                "source": SOURCE,
                "segment_state_run_id": SEGMENT_STATE_RUN_ID,
                "segment_id": int(row["segment_id"]),
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "output_line_number": row.get("output_line_number"),
                "current_output_text": row.get("output_text"),
                "confirmed_text": row.get("confirmed_text"),
                "corrected_text": row.get("confirmed_text"),
                "change_family": "accolade_unlock_can_become_formula_batch2",
                "human_decision": "approve_correction",
                "human_review_status": "approved",
                "decision_source": "chat_human_approval",
                "decision_note": "Approved by user for protected apply dry-run: normalize accolade unlock formula to 'pode se tornar'.",
                "token_integrity_ok": bool(row.get("token_integrity_ok")),
                "requires_protected_apply_dry_run": True,
                "apply_allowed_without_dry_run": False,
                "approved_at": datetime.now().isoformat(timespec="seconds"),
            }
        )

    decision_counts = Counter(row["human_decision"] for row in decisions)
    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "human_approval_record_only",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "input_jsonl": str(INPUT_JSONL),
        "approved_count": len(decisions),
        "decision_counts": dict(decision_counts),
        "approved_segment_ids": [row["segment_id"] for row in decisions],
        "candidate_generation_count": 0,
        "apply_count": 0,
        "lifecycle_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "single_operational_recommendation": "Run protected apply dry-run only for these approved 2 segments.",
        "output_files": {},
    }
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_accolade_formula_run517_batch2_decisions"
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    txt_path = base.with_suffix(".txt")
    write_jsonl(jsonl_path, decisions)
    summary["output_files"] = {"txt": str(txt_path), "jsonl": str(jsonl_path), "summary_json": str(summary_path)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    txt_path.write_text(
        "\n".join(
            [
                "domain_policy_vote_candidate accolade formula run517 batch2 decisions",
                "",
                f"approved_count: {len(decisions)}",
                "approved_segment_ids:",
                *[f"- {row['segment_id']}" for row in decisions],
                "",
                "guards:",
                "- candidate_generation: not_run",
                "- apply: not_run",
                "- lifecycle: not_run",
                "- segment_state: not_run",
                "- reindex: not_run",
                "- full_production: not_run",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
