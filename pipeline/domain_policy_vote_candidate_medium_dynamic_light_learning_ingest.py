from __future__ import annotations

import json
from pathlib import Path

import domain_policy_vote_candidate_human_packet_learning_ingest as base


DECISIONS_PATH = Path("reports/20260629_123549_030368_domain_policy_vote_candidate_medium_dynamic_light_decisions_consolidated.jsonl")


def load_decisions() -> tuple[list[int], dict[int, str]]:
    already_ok: list[int] = []
    corrections: dict[int, str] = {}
    with DECISIONS_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            segment_id = int(record["segment_id"])
            decision = str(record["human_decision"])
            if decision == "approve_already_ok":
                already_ok.append(segment_id)
            elif decision in {"approve_correction", "needs_more_context"}:
                continue
            else:
                raise SystemExit(f"unsupported decision for segment {segment_id}: {decision}")
    return sorted(already_ok), corrections


def main() -> None:
    already_ok, corrections = load_decisions()
    base.RULE_VERSION = "domain_policy_vote_candidate_medium_dynamic_light_learning_ingest_safe_v1"
    base.QUEUE_SOURCE = "domain-policy-vote-candidate-medium-dynamic-light"
    base.ORIGIN = "human_confirmed_domain_policy_vote_candidate_medium_dynamic_light"
    base.MATCH_TYPE_ALREADY_OK = "domain_policy_vote_candidate_medium_dynamic_light_already_ok"
    base.MATCH_TYPE_CORRECTION = "domain_policy_vote_candidate_medium_dynamic_light_correction"
    base.REVIEWER = "user_human_review_domain_policy_vote_candidate_medium_dynamic_light"
    base.FOCUS_GROUP = "domain_policy_vote_candidate_medium_dynamic_light"
    base.SEGMENT_STATE_RUN_ID = 495
    base.APPROVED_ALREADY_OK_SEGMENT_IDS = already_ok
    base.APPROVED_CORRECTIONS = corrections
    base.HELD_SEGMENT_IDS = []
    base.main()


if __name__ == "__main__":
    main()
