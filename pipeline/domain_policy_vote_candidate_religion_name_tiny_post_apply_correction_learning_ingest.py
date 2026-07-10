from __future__ import annotations

from pathlib import Path

import domain_policy_vote_candidate_medium_dynamic_light_post_apply_correction_learning_ingest as base


base.RULE_VERSION = "domain_policy_vote_candidate_religion_name_tiny_post_apply_correction_learning_ingest_v1"
base.DECISIONS_PATH = Path("reports/20260630_084650_784280_domain_policy_vote_candidate_religion_name_tiny_decisions.jsonl")
base.APPLY_POST_VALIDATION_PATH = Path("reports/20260630_084733_076449_domain_policy_vote_candidate_medium_dynamic_light_protected_apply_apply_summary.json")
base.QUEUE_SOURCE = "domain-policy-vote-candidate-religion-name-tiny-post-apply"
base.ORIGIN = "human_confirmed_domain_policy_vote_candidate_religion_name_tiny_post_apply"
base.MATCH_TYPE_CORRECTION = "domain_policy_vote_candidate_religion_name_tiny_post_apply_correction"
base.REVIEWER = "user_human_review_domain_policy_vote_candidate_religion_name_tiny"
base.FOCUS_GROUP = "domain_policy_vote_candidate_religion_name_tiny"
base.SEGMENT_STATE_RUN_ID = 504


if __name__ == "__main__":
    base.main()
