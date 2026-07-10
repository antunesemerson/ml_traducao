from __future__ import annotations

from pathlib import Path

import release_decision_post592_top15_protected_apply as base


base.SOURCE = "release_decision_post594_protected_apply_v1"
base.INPUT_JSONL = Path("reports/20260704_111904_991608_release_decision_post594_diff_preview.jsonl")
base.EXPECTED_READY = 7
base.CONFIRMATION_LABEL = "narrative_plain_light_batch_post594_corrected"
base.EXCLUDED_IDS = {
    61067,
    128258,
    112945,
    79601,
    104908,
    120831,
    65282,
    75914,
    30464,
    45089,
    54888,
    68315,
    76377,
    77588,
    103547,
    104983,
    112620,
    114265,
}


if __name__ == "__main__":
    base.main()
