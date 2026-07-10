from __future__ import annotations

import json
from pathlib import Path

import domain_policy_vote_candidate_human_packet_learning_ingest as base


def latest_decisions_path() -> Path:
    matches = sorted(Path("reports").glob("*_domain_policy_vote_candidate_religion_faith_holy_site_effect_name_micro_decisions.jsonl"))
    if not matches:
        raise SystemExit("missing holy-site effect-name micro decisions jsonl")
    return matches[-1]


def load_decisions() -> tuple[list[int], dict[int, str], list[int]]:
    already_ok: list[int] = []
    corrections: dict[int, str] = {}
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
            elif decision == "corrected_text":
                corrected = str(record.get("corrected_text") or "")
                if not corrected:
                    raise SystemExit(f"missing corrected_text for segment {segment_id}")
                corrections[segment_id] = corrected
            elif decision in {"hold_context", "needs_more_context"}:
                held.append(segment_id)
            else:
                raise SystemExit(f"unsupported decision for segment {segment_id}: {decision}")
    return sorted(already_ok), dict(sorted(corrections.items())), sorted(held)


def main() -> None:
    already_ok, corrections, held = load_decisions()
    base.RULE_VERSION = "domain_policy_vote_candidate_religion_faith_holy_site_effect_name_learning_ingest_v1"
    base.QUEUE_SOURCE = "domain-policy-vote-candidate-religion-faith-holy-site-effect-name"
    base.ORIGIN = "human_confirmed_domain_policy_vote_candidate_religion_faith_holy_site_effect_name"
    base.MATCH_TYPE_ALREADY_OK = "domain_policy_vote_candidate_religion_faith_holy_site_effect_name_already_ok"
    base.MATCH_TYPE_CORRECTION = "domain_policy_vote_candidate_religion_faith_holy_site_effect_name_correction"
    base.REVIEWER = "user_human_review_domain_policy_vote_candidate_religion_faith_holy_site_effect_name"
    base.FOCUS_GROUP = "domain_policy_vote_candidate_religion_faith_holy_site_effect_name"
    base.SEGMENT_STATE_RUN_ID = 510
    base.APPROVED_ALREADY_OK_SEGMENT_IDS = already_ok
    base.APPROVED_CORRECTIONS = corrections
    base.HELD_SEGMENT_IDS = held
    base.main()


if __name__ == "__main__":
    main()
