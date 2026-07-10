from __future__ import annotations

from pathlib import Path

import release_decision_post592_top15_protected_apply as base


base.SOURCE = "release_decision_post595_final_protected_apply_v1"
base.INPUT_JSONL = Path("reports/20260704_114236_416189_release_decision_post595_final_diff_preview.jsonl")
base.EXPECTED_READY = 4
base.CONFIRMATION_LABEL = "narrative_plain_light_batch_post595_final_corrected"
base.EXCLUDED_IDS = {
    99428,
    120831,
    79601,
    104908,
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
