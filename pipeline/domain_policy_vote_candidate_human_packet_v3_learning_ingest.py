from __future__ import annotations

import json
from pathlib import Path

import domain_policy_vote_candidate_human_packet_learning_ingest as base


DECISIONS_PATH = Path("reports/20260628_191248_776884_domain_policy_vote_candidate_human_packet_v3_decisions.json")
FORCE_ALREADY_OK_SEGMENT_IDS = {21083}


def load_decisions() -> tuple[list[int], dict[int, str]]:
    payload = json.loads(DECISIONS_PATH.read_text(encoding="utf-8"))
    already_ok: list[int] = []
    corrections: dict[int, str] = {}
    for record in payload["records"]:
        segment_id = int(record["segment_id"])
        decision = str(record["decision"])
        corrected = str(record["corrected_text"])
        if segment_id in FORCE_ALREADY_OK_SEGMENT_IDS:
            already_ok.append(segment_id)
        elif decision == "approve_already_ok":
            already_ok.append(segment_id)
        elif decision == "approve_correction":
            corrections[segment_id] = corrected
        else:
            raise SystemExit(f"unsupported decision for segment {segment_id}: {decision}")
    return already_ok, corrections


def main() -> None:
    already_ok, corrections = load_decisions()
    base.RULE_VERSION = "domain_policy_vote_candidate_human_packet_v3_learning_ingest_safe_v1"
    base.QUEUE_SOURCE = "domain-policy-vote-candidate-human-packet-v3"
    base.ORIGIN = "human_confirmed_domain_policy_vote_candidate_human_packet_v3"
    base.MATCH_TYPE_ALREADY_OK = "domain_policy_vote_candidate_human_packet_v3_already_ok"
    base.MATCH_TYPE_CORRECTION = "domain_policy_vote_candidate_human_packet_v3_correction"
    base.REVIEWER = "user_human_review_domain_policy_vote_candidate_human_packet_v3"
    base.FOCUS_GROUP = "domain_policy_vote_candidate_human_packet_v3"
    base.SEGMENT_STATE_RUN_ID = 486
    base.APPROVED_ALREADY_OK_SEGMENT_IDS = already_ok
    base.APPROVED_CORRECTIONS = corrections
    base.HELD_SEGMENT_IDS = []
    base.main()


if __name__ == "__main__":
    main()
