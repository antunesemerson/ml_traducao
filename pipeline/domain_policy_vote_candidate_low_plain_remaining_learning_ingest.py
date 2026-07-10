from __future__ import annotations

import json
from pathlib import Path

import domain_policy_vote_candidate_human_packet_learning_ingest as base


DECISIONS_PATH = Path("reports/20260629_111743_502978_domain_policy_vote_candidate_low_plain_remaining_decisions_consolidated.jsonl")


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
            corrected = str(record.get("corrected_text") or "")
            if decision == "approve_already_ok":
                already_ok.append(segment_id)
            elif decision == "approve_correction":
                if not corrected:
                    raise SystemExit(f"missing corrected_text for segment {segment_id}")
                corrections[segment_id] = corrected
            else:
                raise SystemExit(f"unsupported decision for segment {segment_id}: {decision}")
    return sorted(already_ok), dict(sorted(corrections.items()))


def main() -> None:
    already_ok, corrections = load_decisions()
    base.RULE_VERSION = "domain_policy_vote_candidate_low_plain_remaining_learning_ingest_safe_v1"
    base.QUEUE_SOURCE = "domain-policy-vote-candidate-low-plain-remaining"
    base.ORIGIN = "human_confirmed_domain_policy_vote_candidate_low_plain_remaining"
    base.MATCH_TYPE_ALREADY_OK = "domain_policy_vote_candidate_low_plain_remaining_already_ok"
    base.MATCH_TYPE_CORRECTION = "domain_policy_vote_candidate_low_plain_remaining_correction"
    base.REVIEWER = "user_human_review_domain_policy_vote_candidate_low_plain_remaining"
    base.FOCUS_GROUP = "domain_policy_vote_candidate_low_plain_remaining"
    base.SEGMENT_STATE_RUN_ID = 493
    base.APPROVED_ALREADY_OK_SEGMENT_IDS = already_ok
    base.APPROVED_CORRECTIONS = corrections
    base.HELD_SEGMENT_IDS = []
    base.main()


if __name__ == "__main__":
    main()
