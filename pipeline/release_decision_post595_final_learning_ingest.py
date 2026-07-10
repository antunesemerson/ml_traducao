from __future__ import annotations

from pathlib import Path
from typing import Any

import release_decision_post592_top15_learning_ingest as top15


base = top15.base
base.RULE_VERSION = "release_decision_post595_final_learning_ingest_v1"
base.INPUT_JSONL = Path("reports/20260704_114149_654116_release_decision_post595_final_human_decision_extract.jsonl")
base.QUEUE_SOURCE = "release-decision-post595-final-corrected"
base.ORIGIN = "human_confirmed_release_decision_post595_final_corrected"
base.MATCH_TYPE = "narrative_plain_light_human_confirmed_post595_final"
base.REVIEWER = "user_human_review_release_decision_post595_final"
base.FOCUS_GROUP = "release_decision_post595_final_corrected"
base.EXPECTED_COUNT = 4
base.OUTPUT_SLUG = "release_decision_post595_final_learning_ingest"


def read_final_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with base.db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = base.json.loads(line)
            if row.get("validation_status") != "ready_for_diff_preview":
                continue
            row["_line_number"] = line_number
            suggested = row.get("corrected_text") or ""
            row["suggested_human_decision"] = "corrected_text"
            row["output_text"] = suggested
            row["confirmed_text"] = suggested
            rows.append(row)
    return rows


def insert_candidate(conn, run_id: int, context: dict[str, Any], record: dict[str, Any], timestamp: str) -> int:
    reasons = [
        "human_confirmed_release_decision_post595_final",
        "review_decision:corrected_text",
        "protected_apply_post_validated",
        "no_source_write",
        "approved_in_chat",
    ]
    cursor = conn.execute(
        """
        INSERT INTO local_learning_candidates (
            run_id, feedback_id, suggestion_id, segment_id, relative_path,
            source_key, source_line_number, english_text, spanish_text, old_text,
            current_output_text, suggested_text, suggested_hash, source_language,
            origin, match_type, match_score, token_status, suggestion_status,
            local_confidence_score, local_status, human_label, corrected_text,
            reason, reviewer, reviewed_at, reasons_json, created_at, updated_at,
            queue_source, focus_group
        )
        VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            int(context["segment_id"]),
            context["relative_path"],
            context["source_key"],
            context["source_line_number"],
            context["english_text"],
            context["spanish_text"],
            context["old_text"],
            context["current_output_text"],
            record["suggested_text"],
            record["suggested_hash"],
            "human_corrected",
            base.ORIGIN,
            base.MATCH_TYPE,
            1.0,
            "ok",
            "safe",
            1.0,
            "high_confidence",
            "correct",
            record["suggested_text"],
            "human-approved final pre-release corrected-text learning signal",
            base.REVIEWER,
            timestamp,
            base.json.dumps(reasons, ensure_ascii=True),
            timestamp,
            timestamp,
            base.QUEUE_SOURCE,
            base.FOCUS_GROUP,
        ),
    )
    return int(cursor.lastrowid)


base.read_focus_rows = read_final_rows
base.insert_candidate = insert_candidate


if __name__ == "__main__":
    base.main()
