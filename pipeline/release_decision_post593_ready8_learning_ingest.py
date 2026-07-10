from __future__ import annotations

from pathlib import Path
from typing import Any

import release_decision_post592_top15_learning_ingest as base


base.base.RULE_VERSION = "release_decision_post593_ready8_learning_ingest_v1"
base.base.INPUT_JSONL = Path("reports/20260704_103709_320609_release_decision_post593_diff_preview.jsonl")
base.base.QUEUE_SOURCE = "release-decision-post593-ready8-corrected"
base.base.ORIGIN = "human_confirmed_release_decision_post593_ready8_corrected"
base.base.MATCH_TYPE = "narrative_plain_light_human_confirmed_corrected_text"
base.base.REVIEWER = "user_human_review_release_decision_post593_ready8"
base.base.FOCUS_GROUP = "release_decision_post593_ready8_corrected_text"
base.base.EXPECTED_COUNT = 8
base.base.OUTPUT_SLUG = "release_decision_post593_ready8_learning_ingest"


def read_corrected_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with base.base.db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = base.base.json.loads(line)
            if row.get("validation_status") != "ready_for_protected_apply":
                continue
            row["_line_number"] = line_number
            row["token_surface"] = "light_token" if row.get("token_integrity_ok") else "plain_text"
            row["suggested_human_decision"] = "corrected_text"
            row["output_text"] = row.get("corrected_text") or ""
            row["confirmed_text"] = row.get("corrected_text") or ""
            rows.append(row)
    return rows


base.base.read_focus_rows = read_corrected_rows
base.base.insert_candidate = base.insert_candidate


if __name__ == "__main__":
    base.base.main()
