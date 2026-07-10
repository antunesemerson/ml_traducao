from __future__ import annotations

import json
from pathlib import Path

import manual_semantic_triage_name_nickname_dynasty_medium_dynamic_light_batch8_correction_apply as base


DECISIONS_PATH = Path("reports/20260628_182042_698476_manual_semantic_triage_human_packet_v2_decisions.json")
BLOCKED_ALREADY_MATCHES_OUTPUT = {283020, 283026, 78683}


def correction_segment_ids() -> tuple[int, ...]:
    payload = json.loads(DECISIONS_PATH.read_text(encoding="utf-8"))
    return tuple(
        int(record["segment_id"])
        for record in payload["records"]
        if str(record["decision"]) == "approve_correction"
        and int(record["segment_id"]) not in BLOCKED_ALREADY_MATCHES_OUTPUT
    )


def main() -> None:
    base.RULE_VERSION = "manual_semantic_triage_human_packet_v2_correction_apply_v1"
    base.QUEUE_SOURCE = "manual-semantic-triage-human-packet-v2"
    base.ORIGIN = "human_confirmed_manual_semantic_triage_human_packet_v2"
    base.MATCH_TYPE_CORRECTION = "manual_semantic_triage_human_packet_v2_correction"
    base.SEGMENT_IDS = correction_segment_ids()
    base.SEGMENT_STATE_RUN_ID = 485
    base.main()


if __name__ == "__main__":
    main()
