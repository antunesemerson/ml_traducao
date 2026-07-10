from __future__ import annotations

from pathlib import Path

import domain_policy_vote_candidate_accolade_formula_run515_protected_apply_dry_run as base


base.RULE_VERSION = "domain_policy_vote_candidate_token_safe_run523_batch7_protected_apply_dry_run_v1"
base.EXPECTED_STATE_RUN_ID = 523
base.EXPECTED_APPROVED_COUNT = 6
_base_read_jsonl = base.read_jsonl


def latest_decisions_path() -> Path:
    matches = sorted(base.reports_dir().glob("*_domain_policy_vote_candidate_token_safe_run523_batch7_decisions.jsonl"))
    if not matches:
        raise SystemExit("missing run523 batch7 decisions jsonl")
    return matches[-1]


base.latest_decisions_path = latest_decisions_path


def read_jsonl(path: Path) -> list[dict]:
    rows = _base_read_jsonl(path)
    if path == latest_decisions_path():
        return [row for row in rows if row.get("human_decision") == "approve_correction"]
    return rows


base.read_jsonl = read_jsonl


if __name__ == "__main__":
    base.main()
