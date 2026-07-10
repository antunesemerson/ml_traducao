from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import db
import release_readiness_ui_tooltips_packet2_approve_ok_learning_ingest as base


base.RULE_VERSION = "release_readiness_narrative_strict_roi_approve_ok_learning_ingest_v1"
base.INPUT_JSONL = Path("reports/20260703_145719_101020_release_readiness_narrative_strict_roi_approve_ok_readiness.jsonl")
base.QUEUE_SOURCE = "release-readiness-narrative-strict-roi-approve-ok"
base.ORIGIN = "human_confirmed_release_readiness_narrative_strict_roi_approve_ok"
base.MATCH_TYPE = "narrative_plain_light_human_confirmed_already_ok"
base.REVIEWER = "user_human_review_release_readiness_narrative_strict_roi"
base.FOCUS_GROUP = "release_readiness_narrative_strict_roi_approve_ok"
base.EXPECTED_COUNT = 29
base.OUTPUT_SLUG = "release_readiness_narrative_strict_roi_approve_ok_learning_ingest"


def read_ready_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("status") != "ready":
                continue
            row["_line_number"] = line_number
            row["suggested_human_decision"] = "approve_already_ok"
            row["output_text"] = row.get("output_text") or row.get("effective_confirmed_text") or ""
            row["confirmed_text"] = row.get("effective_confirmed_text") or row.get("output_text") or ""
            rows.append(row)
    return rows


base.read_focus_rows = read_ready_rows


if __name__ == "__main__":
    base.main()
