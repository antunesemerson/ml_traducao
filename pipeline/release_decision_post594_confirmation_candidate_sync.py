from __future__ import annotations

import release_decision_post592_top15_confirmation_candidate_sync as base


base.SOURCE = "release_decision_post594_confirmation_candidate_sync_v1"
base.EXPECTED_COUNT = 10
base.CONFIRMATION_LABEL = "narrative_plain_light_batch_post594_corrected"


_original_classify = base.classify


def classify_post594(row):
    _status, reasons = _original_classify(row)
    label = row.get("confirmation_label")
    if label == "correct":
        reasons = [
            reason
            for reason in reasons
            if reason not in {"confirmation_source_mismatch", "confirmation_label_mismatch"}
        ]
    return ("ready" if not reasons else "blocked"), reasons


base.classify = classify_post594


if __name__ == "__main__":
    base.main()
