from __future__ import annotations

from pathlib import Path

import domain_policy_vote_candidate_accolade_formula_run515_protected_apply_dry_run as base


base.RULE_VERSION = "domain_policy_vote_candidate_accolade_formula_run517_batch2_protected_apply_dry_run_v1"
base.EXPECTED_STATE_RUN_ID = 517
base.EXPECTED_APPROVED_COUNT = 2


def latest_decisions_path() -> Path:
    matches = sorted(base.reports_dir().glob("*_domain_policy_vote_candidate_accolade_formula_run517_batch2_decisions.jsonl"))
    if not matches:
        raise SystemExit("missing run517 batch2 decisions jsonl")
    return matches[-1]


base.latest_decisions_path = latest_decisions_path


if __name__ == "__main__":
    base.main()
