from __future__ import annotations

from pathlib import Path

import release_readiness_narrative_strict_roi_approve_ok_learning_ingest as base


base.base.RULE_VERSION = "release_blocker_non_dynamic_post591_approve_ok_learning_ingest_v1"
base.base.INPUT_JSONL = Path("reports/20260703_223511_712914_release_blocker_non_dynamic_post588_approve_ok_readiness.jsonl")
base.base.QUEUE_SOURCE = "release-blocker-non-dynamic-post591-approve-ok"
base.base.ORIGIN = "human_confirmed_release_blocker_non_dynamic_post591_approve_ok"
base.base.MATCH_TYPE = "narrative_plain_light_high_issue_human_confirmed_already_ok"
base.base.REVIEWER = "user_human_review_release_blocker_non_dynamic_post591"
base.base.FOCUS_GROUP = "release_blocker_non_dynamic_post591_approve_ok"
base.base.EXPECTED_COUNT = 30
base.base.OUTPUT_SLUG = "release_blocker_non_dynamic_post591_approve_ok_learning_ingest"


if __name__ == "__main__":
    base.base.main()
