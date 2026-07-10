from __future__ import annotations

from pathlib import Path

import domain_policy_vote_candidate_medium_dynamic_light_learning_ingest as base


base.DECISIONS_PATH = Path("reports/20260629_161353_805347_domain_policy_vote_candidate_medium_dynamic_light_faith_context_decisions.jsonl")


if __name__ == "__main__":
    base.main()
