from __future__ import annotations

import json
from pathlib import Path

import manual_semantic_triage_name_nickname_dynasty_medium_dynamic_light_batch8_learning_ingest as base


DECISIONS_PATH = Path("reports/20260628_173820_063699_manual_semantic_triage_human_packet_decisions.json")


def load_decisions() -> tuple[list[int], dict[int, str], list[int]]:
    payload = json.loads(DECISIONS_PATH.read_text(encoding="utf-8"))
    already_ok: list[int] = []
    corrections: dict[int, str] = {}
    holds: list[int] = []
    for record in payload["records"]:
        segment_id = int(record["segment_id"])
        decision = str(record["decision"])
        if decision == "approve_already_ok":
            already_ok.append(segment_id)
        elif decision == "approve_correction":
            corrections[segment_id] = str(record["corrected_text"])
        elif decision == "needs_more_context":
            holds.append(segment_id)
        else:
            raise SystemExit(f"unsupported decision for segment {segment_id}: {decision}")
    return already_ok, corrections, holds


def main() -> None:
    already_ok, corrections, holds = load_decisions()
    base.RULE_VERSION = "manual_semantic_triage_human_packet_learning_ingest_v1"
    base.QUEUE_SOURCE = "manual-semantic-triage-human-packet-v1"
    base.ORIGIN = "human_confirmed_manual_semantic_triage_human_packet_v1"
    base.MATCH_TYPE_ALREADY_OK = "manual_semantic_triage_human_packet_already_ok"
    base.MATCH_TYPE_CORRECTION = "manual_semantic_triage_human_packet_correction"
    base.REVIEWER = "user_human_review_manual_semantic_triage_human_packet_v1"
    base.FOCUS_GROUP = "manual_semantic_triage_human_packet_v1"
    base.SEGMENT_STATE_RUN_ID = 484
    base.APPROVED_ALREADY_OK_SEGMENT_IDS = already_ok
    base.APPROVED_CORRECTIONS = corrections
    base.HELD_SEGMENT_IDS = holds
    base.main()


if __name__ == "__main__":
    main()
