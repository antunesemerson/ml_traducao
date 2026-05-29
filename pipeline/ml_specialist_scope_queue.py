from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from ml_specialist_models import SPECIALISTS


RULE_VERSION = "ml_specialist_scope_queue_v1"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def next_lote_number(conn) -> int:
    rows = conn.execute(
        """
        SELECT mode
        FROM local_learning_runs
        WHERE mode LIKE 'human_review_%_lote%'
        """
    ).fetchall()
    lote_numbers = []
    for row in rows:
        match = re.search(r"lote(\d+)", str(row["mode"] or ""))
        if match:
            lote_numbers.append(int(match.group(1)))
    return (max(lote_numbers) + 1) if lote_numbers else 1


def load_scope_rows(
    conn,
    scope_sql: str,
    limit: int,
    include_reviewed: bool,
) -> list[dict[str, Any]]:
    reviewed_clause = ""
    if not include_reviewed:
        reviewed_clause = """
          AND NOT EXISTS (
            SELECT 1
            FROM local_learning_candidates c
            WHERE c.segment_id = s.id
              AND c.local_status = 'reviewed_human'
          )
        """
    rows = conn.execute(
        f"""
        WITH scoped AS (
            SELECT *
            FROM source_segments
            WHERE is_active = 1
              AND {scope_sql}
        )
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS current_output_text,
            c.confirmation_level,
            c.confirmation_source,
            c.confirmation_label,
            c.locked
        FROM scoped s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        LEFT JOIN segment_confirmations c ON c.segment_id = s.id
        WHERE 1 = 1
        {reviewed_clause}
        ORDER BY
            CASE WHEN c.locked = 1 THEN 1 ELSE 0 END,
            s.source_key,
            s.id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def candidate_payload(row: dict[str, Any], focus_group: str) -> dict[str, Any]:
    candidate_text = row.get("current_output_text") or row.get("old_text") or row.get("spanish_text") or ""
    return {
        "segment_id": row["segment_id"],
        "relative_path": row.get("relative_path"),
        "source_key": row.get("source_key"),
        "source_line_number": row.get("source_line_number"),
        "source_section": "ML specialist scope review",
        "focus_group": focus_group,
        "auditor_action": "specialist_scope_review",
        "general_action": "not_scored",
        "specialist_action": "scope_candidate",
        "general_safe_probability": 0.0,
        "specialist_safe_probability": 0.0,
        "final_action": "scope_candidate",
        "risk_class": "specialist_scope_review",
        "model_safe_probability": 0.0,
        "issue_count": 0,
        "token_status": "unknown",
        "english_text": row.get("english_text"),
        "spanish_text": row.get("spanish_text"),
        "old_text": row.get("old_text"),
        "current_output_text": row.get("current_output_text"),
        "suggested_text": candidate_text,
        "candidate_text": candidate_text,
        "confirmation_level": row.get("confirmation_level"),
        "confirmation_source": row.get("confirmation_source"),
        "confirmation_label": row.get("confirmation_label"),
        "locked": row.get("locked"),
        "human_label": "pending",
        "corrected_text": None,
        "reason": "",
    }


def build_batches(
    rows: list[dict[str, Any]],
    specialist_name: str,
    first_lote: int,
    batch_size: int,
    batch_count: int,
) -> list[dict[str, Any]]:
    batches = []
    max_rows = batch_size * batch_count
    selected = rows[:max_rows]
    for offset in range(0, len(selected), batch_size):
        batch_rows = selected[offset : offset + batch_size]
        if not batch_rows:
            continue
        batches.append(
            {
                "lote_number": first_lote + len(batches),
                "source_section": "ML specialist scope review",
                "focus_group": specialist_name,
                "queue_source": "ml_specialist_scope_review",
                "candidates": [candidate_payload(row, specialist_name) for row in batch_rows],
            }
        )
    return batches


def main(
    specialist: str,
    batches: int,
    batch_size: int,
    start_lote: int | None,
    output: str | None,
    include_reviewed: bool,
) -> None:
    if specialist not in SPECIALISTS:
        raise RuntimeError(f"Unknown specialist: {specialist}")
    config = SPECIALISTS[specialist]
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        first_lote = start_lote or next_lote_number(conn)
        rows = load_scope_rows(
            conn,
            config.scope_sql,
            limit=batches * batch_size,
            include_reviewed=include_reviewed,
        )
    prepared_batches = build_batches(rows, config.name, first_lote, batch_size, batches)
    payload = {
        "rule_version": RULE_VERSION,
        "prepared_at": now(),
        "source_type": "ml_specialist_scope_review",
        "score_run_id": None,
        "source_report": f"specialist_scope:{config.name}",
        "specialist": config.name,
        "description": config.description,
        "scope_sql": config.scope_sql,
        "batch_size": batch_size,
        "batches": prepared_batches,
        "instructions": {
            "valid_labels": [
                "correct",
                "contextual_exception",
                "minor_fix",
                "major_fix",
                "semantic_error",
                "residual_spanish",
                "structure_error",
                "token_mismatch",
                "rejected",
                "rejected_suggestion",
            ],
            "recommended_labels": [
                "correct",
                "minor_fix",
                "semantic_error",
                "residual_spanish",
                "contextual_exception",
            ],
            "label_guidance": {
                "correct": "Use when current output is safe and natural for this specialist scope.",
                "minor_fix": "Use when the current output is almost safe but needs a small textual correction.",
                "semantic_error": "Use when the current output has the wrong meaning for the English/context.",
                "residual_spanish": "Use when Spanish remains in the current output and should be corrected.",
                "contextual_exception": "Use for intentional non-mirror localization choices that are safe in game context.",
            },
            "fill": ["human_label", "corrected_text when useful", "reason"],
            "do_not_run": ["ml-train-risk", "ml-score", "apply output"],
        },
    }
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    output_path = Path(output) if output else reports_dir / f"{timestamp()}_specialist_scope_review_decisions_template.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[ml_specialist_scope_queue] Specialist: {config.name}")
    print(f"[ml_specialist_scope_queue] Candidates: {sum(len(batch['candidates']) for batch in prepared_batches)}")
    print(f"[ml_specialist_scope_queue] Batches: {len(prepared_batches)}")
    print(f"[ml_specialist_scope_queue] Decision template: {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prepare review batches from a specialist scope.")
    parser.add_argument("--specialist", choices=sorted(SPECIALISTS), required=True)
    parser.add_argument("--batches", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=30)
    parser.add_argument("--start-lote", type=int)
    parser.add_argument("--output")
    parser.add_argument("--include-reviewed", action="store_true")
    args = parser.parse_args()
    main(
        specialist=args.specialist,
        batches=args.batches,
        batch_size=args.batch_size,
        start_lote=args.start_lote,
        output=args.output,
        include_reviewed=args.include_reviewed,
    )
