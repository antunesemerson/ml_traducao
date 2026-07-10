from __future__ import annotations

from pathlib import Path
from typing import Any

import release_readiness_ui_tooltips_packet3_approve_ok_issue_closure as base


base.SOURCE = "release_decision_post592_top15_issue_closure_v1"
base.DEFAULT_INPUT_JSONL = Path("reports/20260704_005755_892574_release_decision_post592_top15_diff_preview.jsonl")
base.DEFAULT_SEGMENT_STATE_RUN_ID = 592
base.DEFAULT_LEDGER_RUN_ID = 76
base.EXPECTED_SEGMENT_COUNT = 13
base.TARGET_VALIDATION_STATUS = "closed_resolved_by_human_release_decision_post592_top15_corrected_text"


def read_ids(path: Path) -> list[int]:
    ids: list[int] = []
    with base.db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = base.json.loads(line)
            if row.get("validation_status") == "ready_for_protected_apply":
                ids.append(int(row["segment_id"]))
    return sorted(set(ids))


_original_classify = base.classify


def classify_allow_high_issue(row: dict[str, Any]) -> tuple[str, list[str]]:
    _decision, reasons = _original_classify(row)
    reasons = [reason for reason in reasons if reason != "high_issue"]
    return ("released" if not reasons else "blocked"), reasons


base.read_ids = read_ids
base.classify = classify_allow_high_issue


if __name__ == "__main__":
    base.main()
