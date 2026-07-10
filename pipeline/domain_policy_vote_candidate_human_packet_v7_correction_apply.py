from __future__ import annotations

import json
from pathlib import Path

import domain_policy_vote_candidate_human_packet_correction_apply as base


DECISIONS_PATH = Path("reports/20260628_232857_836072_domain_policy_vote_candidate_human_packet_v7_decisions.json")


def correction_segment_ids() -> tuple[int, ...]:
    payload = json.loads(DECISIONS_PATH.read_text(encoding="utf-8"))
    return tuple(int(record["segment_id"]) for record in payload["records"] if record["decision"] == "approve_correction")


def main() -> None:
    base.RULE_VERSION = "domain_policy_vote_candidate_human_packet_v7_correction_apply_safe_v1"
    base.QUEUE_SOURCE = "domain-policy-vote-candidate-human-packet-v7"
    base.ORIGIN = "human_confirmed_domain_policy_vote_candidate_human_packet_v7"
    base.MATCH_TYPE_CORRECTION = "domain_policy_vote_candidate_human_packet_v7_correction"
    base.SEGMENT_IDS = correction_segment_ids()
    base.SEGMENT_STATE_RUN_ID = 492
    base.main()


if __name__ == "__main__":
    main()
