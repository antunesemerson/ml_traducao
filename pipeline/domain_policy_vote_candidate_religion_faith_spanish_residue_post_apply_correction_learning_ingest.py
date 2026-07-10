from __future__ import annotations

import json
from pathlib import Path

import domain_policy_vote_candidate_medium_dynamic_light_post_apply_correction_learning_ingest as base


RULE_VERSION = "domain_policy_vote_candidate_religion_faith_spanish_residue_protected_apply_v1"


def latest_decisions_path() -> Path:
    matches = sorted(Path("reports").glob("*_domain_policy_vote_candidate_religion_faith_spanish_residue_decisions.jsonl"))
    if not matches:
        raise SystemExit("missing religion_faith spanish residue decisions jsonl")
    return matches[-1]


def latest_apply_summary_path() -> Path:
    matches = sorted(Path("reports").glob("*_domain_policy_vote_candidate_medium_dynamic_light_protected_apply_apply_summary.json"))
    for path in reversed(matches):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("rule_version") == RULE_VERSION:
            return path
    raise SystemExit("missing spanish residue protected apply summary")


base.RULE_VERSION = "domain_policy_vote_candidate_religion_faith_spanish_residue_post_apply_correction_learning_ingest_v1"
base.DECISIONS_PATH = latest_decisions_path()
base.APPLY_POST_VALIDATION_PATH = latest_apply_summary_path()
base.QUEUE_SOURCE = "domain-policy-vote-candidate-religion-faith-spanish-residue-post-apply"
base.ORIGIN = "human_confirmed_domain_policy_vote_candidate_religion_faith_spanish_residue_post_apply"
base.MATCH_TYPE_CORRECTION = "domain_policy_vote_candidate_religion_faith_spanish_residue_post_apply_correction"
base.REVIEWER = "user_human_review_domain_policy_vote_candidate_religion_faith_spanish_residue"
base.FOCUS_GROUP = "domain_policy_vote_candidate_religion_faith_spanish_residue"
base.SEGMENT_STATE_RUN_ID = 504


if __name__ == "__main__":
    base.main()
