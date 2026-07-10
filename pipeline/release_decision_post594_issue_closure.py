from __future__ import annotations

from pathlib import Path
from typing import Any

import release_decision_post592_top15_issue_closure as base


base.base.SOURCE = "release_decision_post594_issue_closure_v1"
base.base.DEFAULT_INPUT_JSONL = Path("reports/20260704_111826_654385_release_decision_post594_human_decision_extract.jsonl")
base.base.DEFAULT_SEGMENT_STATE_RUN_ID = 594
base.base.DEFAULT_LEDGER_RUN_ID = 76
base.base.EXPECTED_SEGMENT_COUNT = 10
base.base.TARGET_VALIDATION_STATUS = "closed_resolved_by_human_release_decision_post594"


def read_ids(path: Path) -> list[int]:
    ids: list[int] = []
    with base.base.db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = base.base.json.loads(line)
            if row.get("validation_status") in {"ready_for_diff_preview", "ready_approve_already_ok"}:
                ids.append(int(row["segment_id"]))
    return sorted(set(ids))


_original_classify = base.base.classify


def classify_allow_human_decision(row: dict[str, Any]) -> tuple[str, list[str]]:
    _decision, reasons = _original_classify(row)
    reasons = [
        reason
        for reason in reasons
        if reason not in {"high_issue", "not_human_confirmed", "not_locked"}
    ]
    return ("released" if not reasons else "blocked"), reasons


base.base.read_ids = read_ids
base.base.classify = classify_allow_human_decision


if __name__ == "__main__":
    base.base.main()
