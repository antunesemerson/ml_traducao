from __future__ import annotations

import json
from pathlib import Path

import domain_policy_vote_candidate_human_packet_correction_apply as base


DECISIONS_PATH = Path("reports/20260628_191248_776884_domain_policy_vote_candidate_human_packet_v3_decisions.json")
FORCE_ALREADY_OK_SEGMENT_IDS = {21083}


def correction_segment_ids() -> tuple[int, ...]:
    payload = json.loads(DECISIONS_PATH.read_text(encoding="utf-8"))
    ids: list[int] = []
    for record in payload["records"]:
        if record["decision"] == "approve_correction" and int(record["segment_id"]) not in FORCE_ALREADY_OK_SEGMENT_IDS:
            ids.append(int(record["segment_id"]))
    return tuple(ids)


def main() -> None:
    base.RULE_VERSION = "domain_policy_vote_candidate_human_packet_v3_correction_apply_safe_v1"
    base.QUEUE_SOURCE = "domain-policy-vote-candidate-human-packet-v3"
    base.ORIGIN = "human_confirmed_domain_policy_vote_candidate_human_packet_v3"
    base.MATCH_TYPE_CORRECTION = "domain_policy_vote_candidate_human_packet_v3_correction"
    base.SEGMENT_IDS = correction_segment_ids()
    base.SEGMENT_STATE_RUN_ID = 486
    base.main()


if __name__ == "__main__":
    main()
