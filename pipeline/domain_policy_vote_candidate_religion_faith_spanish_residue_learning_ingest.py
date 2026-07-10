from __future__ import annotations

import json
from pathlib import Path

import domain_policy_vote_candidate_human_packet_learning_ingest as base


def latest_decisions_path() -> Path:
    matches = sorted(Path("reports").glob("*_domain_policy_vote_candidate_religion_faith_spanish_residue_decisions.jsonl"))
    if not matches:
        raise SystemExit("missing religion_faith spanish residue decisions jsonl")
    return matches[-1]


def load_decisions() -> tuple[list[int], list[int]]:
    already_ok: list[int] = []
    held: list[int] = []
    with latest_decisions_path().open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            segment_id = int(record["segment_id"])
            decision = str(record["human_decision"])
            if decision == "approve_already_ok":
                already_ok.append(segment_id)
            elif decision == "needs_more_context":
                held.append(segment_id)
    return sorted(already_ok), sorted(held)


def main() -> None:
    already_ok, held = load_decisions()
    base.RULE_VERSION = "domain_policy_vote_candidate_religion_faith_spanish_residue_learning_ingest_v1"
    base.QUEUE_SOURCE = "domain-policy-vote-candidate-religion-faith-spanish-residue"
    base.ORIGIN = "human_confirmed_domain_policy_vote_candidate_religion_faith_spanish_residue"
    base.MATCH_TYPE_ALREADY_OK = "domain_policy_vote_candidate_religion_faith_spanish_residue_already_ok"
    base.MATCH_TYPE_CORRECTION = "domain_policy_vote_candidate_religion_faith_spanish_residue_correction"
    base.REVIEWER = "user_human_review_domain_policy_vote_candidate_religion_faith_spanish_residue"
    base.FOCUS_GROUP = "domain_policy_vote_candidate_religion_faith_spanish_residue"
    base.SEGMENT_STATE_RUN_ID = 504
    base.APPROVED_ALREADY_OK_SEGMENT_IDS = already_ok
    base.APPROVED_CORRECTIONS = {}
    base.HELD_SEGMENT_IDS = held
    base.main()


if __name__ == "__main__":
    main()
