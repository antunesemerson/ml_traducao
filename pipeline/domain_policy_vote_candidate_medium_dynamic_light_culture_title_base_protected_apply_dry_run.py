from __future__ import annotations

from pathlib import Path

import domain_policy_vote_candidate_medium_dynamic_light_protected_apply_dry_run as base


base.RULE_VERSION = "domain_policy_vote_candidate_medium_dynamic_light_culture_title_base_protected_apply_dry_run_v1"
base.DECISIONS_PATH = Path("reports/20260629_165005_444200_domain_policy_vote_candidate_medium_dynamic_light_culture_title_base_decisions.jsonl")
base.EXPECTED_STATE_RUN_ID = 501


if __name__ == "__main__":
    base.main()
