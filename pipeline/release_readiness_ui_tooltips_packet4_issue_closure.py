from __future__ import annotations

from pathlib import Path

import db
import release_readiness_ui_tooltips_packet3_approve_ok_issue_closure as base


base.SOURCE = "release_readiness_ui_tooltips_packet4_issue_closure_v1"
base.DEFAULT_INPUT_JSONL = Path("reports/20260703_135336_978871_release_readiness_ui_tooltips_packet4_learning_ingest_apply.jsonl")
base.DEFAULT_SEGMENT_STATE_RUN_ID = 575
base.DEFAULT_LEDGER_RUN_ID = 77
base.EXPECTED_SEGMENT_COUNT = 40
base.TARGET_VALIDATION_STATUS = "closed_resolved_by_human_ui_tooltips_packet4_review"


def read_ids(path: Path) -> list[int]:
    ids: list[int] = []
    with db.project_path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = base.json.loads(line)
            if row.get("status") in {"inserted", "ready"}:
                ids.append(int(row["segment_id"]))
    return sorted(set(ids))


base.read_ids = read_ids


if __name__ == "__main__":
    base.main()
