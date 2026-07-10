from __future__ import annotations

from pathlib import Path
from typing import Any

import release_decision_post592_top15_learning_ingest as top15


base = top15.base
base.RULE_VERSION = "release_decision_post594_learning_ingest_v1"
base.INPUT_JSONL = Path("reports/20260704_111826_654385_release_decision_post594_human_decision_extract.jsonl")
base.QUEUE_SOURCE = "release-decision-post594-human-signals"
base.ORIGIN = "human_confirmed_release_decision_post594_human_signals"
base.MATCH_TYPE = "narrative_plain_light_human_confirmed_post594"
base.REVIEWER = "user_human_review_release_decision_post594"
base.FOCUS_GROUP = "release_decision_post594_human_signals"
base.EXPECTED_COUNT = 10
base.OUTPUT_SLUG = "release_decision_post594_learning_ingest"


def read_post594_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with base.db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = base.json.loads(line)
            status = row.get("validation_status")
            if status not in {"ready_for_diff_preview", "ready_approve_already_ok"}:
                continue
            row["_line_number"] = line_number
            decision = row.get("human_decision")
            if decision == "corrected_text":
                suggested = row.get("corrected_text") or ""
                row["suggested_human_decision"] = "corrected_text"
            elif decision == "approve_already_ok":
                suggested = row.get("output_text") or row.get("confirmed_text") or ""
                row["suggested_human_decision"] = "approve_already_ok"
            else:
                continue
            row["output_text"] = suggested
            row["confirmed_text"] = suggested
            rows.append(row)
    return rows


def insert_candidate(conn, run_id: int, context: dict[str, Any], record: dict[str, Any], timestamp: str) -> int:
    decision = record.get("suggested_human_decision") or "human_confirmed"
    reasons = [
        "human_confirmed_release_decision_post594",
        f"review_decision:{decision}",
        "no_source_write",
        "approved_in_chat",
    ]
    if decision == "corrected_text":
        reasons.append("protected_apply_post_validated")
        source_language = "human_corrected"
        human_label = "correct"
        corrected_text = record["suggested_text"]
        reason = "human-approved release decision post594 corrected-text learning signal"
    else:
        reasons.extend(["output_already_matches_human_approved_text", "no_output_write"])
        source_language = "human_already_ok"
        human_label = "correct"
        corrected_text = None
        reason = "human-approved release decision post594 already-ok learning signal"
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
            source_language,
            base.ORIGIN,
            base.MATCH_TYPE,
            1.0,
            "ok",
            "safe",
            1.0,
            "high_confidence",
            human_label,
            corrected_text,
            reason,
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


base.read_focus_rows = read_post594_rows
base.insert_candidate = insert_candidate


if __name__ == "__main__":
    base.main()
