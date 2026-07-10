from __future__ import annotations

from pathlib import Path

import release_readiness_ui_tooltips_packet3_approve_ok_issue_closure as base


base.SOURCE = "release_readiness_narrative_strict_roi_approve_ok_issue_closure_v1"
base.DEFAULT_INPUT_JSONL = Path("reports/20260703_145719_101020_release_readiness_narrative_strict_roi_approve_ok_readiness.jsonl")
base.DEFAULT_SEGMENT_STATE_RUN_ID = 578
base.DEFAULT_LEDGER_RUN_ID = 76
base.EXPECTED_SEGMENT_COUNT = 29
base.TARGET_VALIDATION_STATUS = "closed_resolved_by_human_narrative_strict_roi_approve_already_ok"


if __name__ == "__main__":
    base.main()
