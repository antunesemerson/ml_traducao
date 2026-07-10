from __future__ import annotations

from pathlib import Path
from typing import Any

import release_readiness_ui_tooltips_packet3_approve_ok_issue_closure as base


base.SOURCE = "release_blocker_non_dynamic_post591_approve_ok_issue_closure_v1"
base.DEFAULT_INPUT_JSONL = Path("reports/20260703_223511_712914_release_blocker_non_dynamic_post588_approve_ok_readiness.jsonl")
base.DEFAULT_SEGMENT_STATE_RUN_ID = 591
base.DEFAULT_LEDGER_RUN_ID = 76
base.EXPECTED_SEGMENT_COUNT = 30
base.TARGET_VALIDATION_STATUS = "closed_resolved_by_human_release_blocker_non_dynamic_post591_approve_ok"

_original_classify = base.classify


def classify_allow_high_issue(row: dict[str, Any]) -> tuple[str, list[str]]:
    _decision, reasons = _original_classify(row)
    reasons = [reason for reason in reasons if reason != "high_issue"]
    return ("released" if not reasons else "blocked"), reasons


base.classify = classify_allow_high_issue


if __name__ == "__main__":
    base.main()
