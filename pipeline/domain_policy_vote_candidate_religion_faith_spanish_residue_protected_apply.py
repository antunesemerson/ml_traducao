from __future__ import annotations

from pathlib import Path

import domain_policy_vote_candidate_medium_dynamic_light_protected_apply as base
import domain_policy_vote_candidate_medium_dynamic_light_protected_apply_dry_run as dry


def latest_decisions_path() -> Path:
    matches = sorted(Path("reports").glob("*_domain_policy_vote_candidate_religion_faith_spanish_residue_decisions.jsonl"))
    if not matches:
        raise SystemExit("missing religion_faith spanish residue decisions jsonl")
    return matches[-1]


dry.RULE_VERSION = "domain_policy_vote_candidate_religion_faith_spanish_residue_protected_apply_dry_run_v1"
dry.DECISIONS_PATH = latest_decisions_path()
dry.EXPECTED_STATE_RUN_ID = 504
base.RULE_VERSION = "domain_policy_vote_candidate_religion_faith_spanish_residue_protected_apply_v1"


if __name__ == "__main__":
    base.main()
