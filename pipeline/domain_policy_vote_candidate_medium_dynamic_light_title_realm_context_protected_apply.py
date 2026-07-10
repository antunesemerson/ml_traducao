from __future__ import annotations

from pathlib import Path

import domain_policy_vote_candidate_medium_dynamic_light_protected_apply as base
import domain_policy_vote_candidate_medium_dynamic_light_protected_apply_dry_run as dry


dry.RULE_VERSION = "domain_policy_vote_candidate_medium_dynamic_light_title_realm_context_protected_apply_dry_run_v1"
dry.DECISIONS_PATH = Path("reports/20260629_150925_724079_domain_policy_vote_candidate_medium_dynamic_light_title_realm_context_decisions.jsonl")
dry.EXPECTED_STATE_RUN_ID = 498
base.RULE_VERSION = "domain_policy_vote_candidate_medium_dynamic_light_title_realm_context_protected_apply_v1"


if __name__ == "__main__":
    base.main()
