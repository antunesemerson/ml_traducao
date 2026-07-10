from __future__ import annotations

from pathlib import Path

import release_readiness_ui_tooltips_packet4_learning_ingest as base


base.RULE_VERSION = "release_readiness_ui_tooltips_packet5_learning_ingest_v1"
base.PACKET_JSONL = Path("reports/20260703_140007_775717_release_readiness_ui_tooltips_plain_light_human_packet.jsonl")
base.APPLY_JSONL = Path("reports/20260703_140613_847297_release_readiness_ui_tooltips_packet5_corrected_apply_apply.jsonl")
base.QUEUE_SOURCE = "release-readiness-ui-tooltips-packet5"
base.ORIGIN = "human_confirmed_release_readiness_ui_tooltips_packet5"
base.REVIEWER = "user_human_review_release_readiness_ui_tooltips_packet5"
base.FOCUS_GROUP = "release_readiness_ui_tooltips_packet5"
base.CORRECTED_SEGMENT_IDS = {52828, 60150, 62986, 63033, 143987, 155528, 155529, 163030}
base.APPROVE_SEGMENT_IDS = {51833, 51971, 56903, 64651, 155527, 155571}

_ORIGINAL_BUILD_RECORDS = base.build_records


def build_records(args):
    records = _ORIGINAL_BUILD_RECORDS(args)
    return [record for record in records if int(record["segment_id"]) != 155571]


base.build_records = build_records


if __name__ == "__main__":
    base.main()
