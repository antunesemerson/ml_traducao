from __future__ import annotations

import json
from pathlib import Path

import manual_semantic_triage_name_nickname_dynasty_medium_dynamic_light_batch8_correction_apply as base


DECISIONS_PATH = Path("reports/20260628_173820_063699_manual_semantic_triage_human_packet_decisions.json")
HELD_DRY_RUN_BLOCKED_SEGMENT_IDS = {120831, 100757, 62620}


def correction_segment_ids() -> tuple[int, ...]:
    payload = json.loads(DECISIONS_PATH.read_text(encoding="utf-8"))
    return tuple(
        int(record["segment_id"])
        for record in payload["records"]
        if str(record["decision"]) == "approve_correction"
        and int(record["segment_id"]) not in HELD_DRY_RUN_BLOCKED_SEGMENT_IDS
    )


def main() -> None:
    base.RULE_VERSION = "manual_semantic_triage_human_packet_correction_apply_v1"
    base.QUEUE_SOURCE = "manual-semantic-triage-human-packet-v1"
    base.ORIGIN = "human_confirmed_manual_semantic_triage_human_packet_v1"
    base.MATCH_TYPE_CORRECTION = "manual_semantic_triage_human_packet_correction"
    base.SEGMENT_IDS = correction_segment_ids()
    base.SEGMENT_STATE_RUN_ID = 484
    base.main()


if __name__ == "__main__":
    main()
