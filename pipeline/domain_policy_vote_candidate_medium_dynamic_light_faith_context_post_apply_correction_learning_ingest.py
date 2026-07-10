from __future__ import annotations

from pathlib import Path

import domain_policy_vote_candidate_medium_dynamic_light_post_apply_correction_learning_ingest as base


base.DECISIONS_PATH = Path("reports/20260629_161353_805347_domain_policy_vote_candidate_medium_dynamic_light_faith_context_decisions.jsonl")
base.APPLY_POST_VALIDATION_PATH = Path("reports/20260629_161432_677411_domain_policy_vote_candidate_medium_dynamic_light_protected_apply_apply_summary.json")
base.QUEUE_SOURCE = "domain-policy-vote-candidate-medium-dynamic-light-faith-context-post-apply"
base.ORIGIN = "human_confirmed_domain_policy_vote_candidate_medium_dynamic_light_faith_context_post_apply"
base.MATCH_TYPE_CORRECTION = "domain_policy_vote_candidate_medium_dynamic_light_faith_context_post_apply_correction"


if __name__ == "__main__":
    base.main()
