from __future__ import annotations

from pathlib import Path

import release_decision_post592_top15_protected_apply as base


base.SOURCE = "release_decision_post593_ready8_protected_apply_v1"
base.INPUT_JSONL = Path("reports/20260704_103709_320609_release_decision_post593_diff_preview.jsonl")
base.EXPECTED_READY = 8
base.CONFIRMATION_LABEL = "narrative_plain_light_batch_post593_ready8_corrected"
base.EXCLUDED_IDS = {
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
    79601,
    104908,
    120831,
}


if __name__ == "__main__":
    base.main()
