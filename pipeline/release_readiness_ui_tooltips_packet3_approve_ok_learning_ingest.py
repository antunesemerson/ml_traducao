from __future__ import annotations

from pathlib import Path

import release_readiness_ui_tooltips_packet2_approve_ok_learning_ingest as base


base.RULE_VERSION = "release_readiness_ui_tooltips_packet3_approve_ok_learning_ingest_v1"
base.INPUT_JSONL = Path("reports/20260703_122244_761708_release_readiness_ui_tooltips_plain_light_human_packet.jsonl")
base.QUEUE_SOURCE = "release-readiness-ui-tooltips-packet3-approve-ok"
base.ORIGIN = "human_confirmed_release_readiness_ui_tooltips_packet3_approve_ok"
base.MATCH_TYPE = "ui_tooltips_plain_light_human_confirmed_already_ok"
base.REVIEWER = "user_human_review_release_readiness_ui_tooltips_packet3"
base.FOCUS_GROUP = "release_readiness_ui_tooltips_packet3_approve_ok"
base.EXPECTED_COUNT = 30
base.OUTPUT_SLUG = "release_readiness_ui_tooltips_packet3_approve_ok_learning_ingest"


if __name__ == "__main__":
    base.main()
