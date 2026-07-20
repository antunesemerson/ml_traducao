from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "quality_pairwise_promotion_queue_v1"
DEFAULT_EVIDENCE_TYPE = "deterministic_space_before_punctuation_repair"
CONFIRMATION_SOURCE = "pairwise_monotonic_repair"
CONFIRMATION_LABELS = {
    "deterministic_space_before_punctuation_repair": "space_before_punctuation_v1",
    "deterministic_gender_token_extra_prefix_repair": "gender_token_extra_prefix_v1",
}


def confirmation_label(evidence_type: str) -> str:
    return CONFIRMATION_LABELS.get(evidence_type, f"pairwise_{evidence_type}_v1")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def latest_state_run_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT id FROM segment_state_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise RuntimeError("No completed segment-state run was found.")
    return int(row["id"])


def evaluate_rows(
    conn: sqlite3.Connection,
    state_run_id: int,
    evidence_type: str,
) -> list[dict[str, Any]]:
    label = confirmation_label(evidence_type)
    rows = conn.execute(
        """
        SELECT
          evidence.id AS evidence_id,
          evidence.evidence_key,
          evidence.segment_id,
          evidence.relative_path,
          evidence.source_key,
          evidence.baseline_text,
          evidence.candidate_text,
          evidence.pairwise_score,
          evidence.pairwise_delta,
          evidence.promotion_eligible,
          evidence.auto_safe_eligible,
          evidence.token_integrity_ok,
          evidence.post_validation_clean,
          output.portuguese_text AS current_output_text,
          confirmation.id AS confirmation_id,
          confirmation.confirmation_level,
          confirmation.confirmed_text,
          confirmation.confirmation_source,
          confirmation.confirmation_label,
          COALESCE(confirmation.locked, 0) AS confirmation_locked,
          state.final_state,
          state.state_group,
          COALESCE(state.needs_output_apply, 0) AS needs_output_apply
        FROM ml_pairwise_quality_evidence evidence
        JOIN output_segments output ON output.segment_id = evidence.segment_id
        JOIN segment_state_items state
          ON state.segment_id = evidence.segment_id
         AND state.run_id = ?
        LEFT JOIN segment_confirmations confirmation ON confirmation.segment_id = evidence.segment_id
        WHERE evidence.evidence_type = ?
          AND evidence.promotion_eligible = 1
          AND evidence.id = (
            SELECT MAX(latest.id)
            FROM ml_pairwise_quality_evidence latest
            WHERE latest.segment_id = evidence.segment_id
              AND latest.evidence_type = evidence.evidence_type
          )
          AND output.portuguese_text = evidence.baseline_text
          AND evidence.candidate_text <> output.portuguese_text
        ORDER BY evidence.baseline_score_raw DESC, evidence.segment_id
        """,
        (state_run_id, evidence_type),
    ).fetchall()
    records: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        already_queued = (
            row.get("confirmed_text") == row["candidate_text"]
            and row.get("confirmation_source") == CONFIRMATION_SOURCE
            and row.get("confirmation_label") == label
        )
        guards = {
            "promotion_metadata_present": int(row["promotion_eligible"] or 0) == 1,
            "absolute_auto_safe_not_granted": int(row["auto_safe_eligible"] or 0) == 0,
            "baseline_is_current_output": row["current_output_text"] == row["baseline_text"],
            "candidate_changes_output": row["candidate_text"] != row["current_output_text"],
            "segment_baseline_is_closed": row["state_group"] == "closed",
            "no_existing_output_apply": int(row["needs_output_apply"] or 0) == 0 or already_queued,
            "no_locked_confirmation": int(row["confirmation_locked"] or 0) == 0,
            "token_integrity_ok": int(row["token_integrity_ok"] or 0) == 1,
            "post_validation_clean": int(row["post_validation_clean"] or 0) == 1,
        }
        ready = all(guards.values())
        records.append(
            {
                **row,
                "already_queued": already_queued,
                "guards": guards,
                "ready": ready,
                "block_reasons": [name for name, passed in guards.items() if not passed],
                "confirmation_source_after": CONFIRMATION_SOURCE,
                "confirmation_label_after": label,
                "confirmation_level_after": "auto_confirmed",
                "locked_after": 0,
                "confirmation_confidence_after": 0.98,
            }
        )
    return records


def queue_confirmation(
    conn: sqlite3.Connection,
    record: dict[str, Any],
    now: str,
    evidence_type: str,
) -> None:
    label = confirmation_label(evidence_type)
    if record.get("confirmation_id") is None:
        conn.execute(
            """
            INSERT INTO segment_confirmations (
              confirmed_text, confirmation_level, confirmation_source, confirmation_label,
              locked, confidence_score, reviewer, confirmed_at, updated_at, segment_id
            ) VALUES (?, 'auto_confirmed', ?, ?, 0, 0.98, ?, ?, ?, ?)
            """,
            (
                record["candidate_text"],
                CONFIRMATION_SOURCE,
                label,
                RULE_VERSION,
                now,
                now,
                int(record["segment_id"]),
            ),
        )
        return
    conn.execute(
        """
        UPDATE segment_confirmations
        SET confirmed_text = ?, confirmation_level = 'auto_confirmed',
            confirmation_source = ?, confirmation_label = ?, locked = 0,
            confidence_score = 0.98, reviewer = ?, confirmed_at = ?, updated_at = ?
        WHERE segment_id = ?
        """,
        (
            record["candidate_text"],
            CONFIRMATION_SOURCE,
            label,
            RULE_VERSION,
            now,
            now,
            int(record["segment_id"]),
        ),
    )


def write_reports(
    records: list[dict[str, Any]],
    state_run_id: int,
    evidence_type: str,
    apply: bool,
) -> dict[str, Any]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    base = reports_dir / f"{stamp()}_quality_pairwise_promotion_queue_{'apply' if apply else 'dry_run'}"
    markdown_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = base.with_name(base.name + "_summary.json")
    ready = [row for row in records if row["ready"]]
    blocked = [row for row in records if not row["ready"]]
    newly_queued = [row for row in ready if not row["already_queued"]]
    already_queued = [row for row in ready if row["already_queued"]]
    block_counts = Counter(reason for row in blocked for reason in row["block_reasons"])

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in records:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary = {
        "schema_version": 1,
        "source": RULE_VERSION,
        "evidence_type": evidence_type,
        "source_segment_state_run_id": state_run_id,
        "record_count": len(records),
        "ready_count": len(ready),
        "blocked_count": len(blocked),
        "already_queued_count": len(already_queued),
        "newly_queued_count": len(newly_queued),
        "queued_confirmation_count": len(newly_queued) if apply else 0,
        "block_reason_counts": dict(block_counts),
        "output_write_count": 0,
        "source_changed": False,
        "output_changed": False,
        "next_step": "run_segment_state_and_apply_dry_runs" if apply else "rerun_with_apply_if_all_ready",
        "artifacts": {
            "markdown": str(markdown_path),
            "jsonl": str(jsonl_path),
            "summary": str(summary_path),
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Pairwise monotonic promotion queue",
        "",
        f"- Mode: `{'apply' if apply else 'dry_run'}`",
        f"- State run: `{state_run_id}`",
        f"- Ready: `{len(ready)}`",
        f"- Blocked: `{len(blocked)}`",
        f"- Already queued: `{len(already_queued)}`",
        f"- Newly queued: `{len(newly_queued)}`",
        f"- Confirmation writes: `{len(newly_queued) if apply else 0}`",
        "- Output writes: `0`",
        "",
        "## Block reasons",
        "",
    ]
    if block_counts:
        lines.extend(f"- `{name}`: `{count}`" for name, count in block_counts.most_common())
    else:
        lines.append("- none")
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Queue pairwise monotonic promotions as confirmations without writing output."
    )
    parser.add_argument("--evidence-type", default=DEFAULT_EVIDENCE_TYPE)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    settings = db.load_settings()
    database_path = db.get_database_path(settings)
    if args.apply:
        with db.connect(settings) as conn:
            db.ensure_database(conn)
            state_run_id = latest_state_run_id(conn)
            records = evaluate_rows(conn, state_run_id, args.evidence_type)
            ready = [row for row in records if row["ready"]]
            now = db.utc_now()
            for record in ready:
                if not record["already_queued"]:
                    queue_confirmation(conn, record, now, args.evidence_type)
            conn.commit()
    else:
        with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=120) as conn:
            conn.row_factory = sqlite3.Row
            state_run_id = latest_state_run_id(conn)
            records = evaluate_rows(conn, state_run_id, args.evidence_type)
    if not records:
        raise RuntimeError("No promotion-eligible pairwise evidence was found.")
    summary = write_reports(records, state_run_id, args.evidence_type, args.apply)
    print("[quality_pairwise_promotion_queue] Completed")
    print(f"[quality_pairwise_promotion_queue] Ready: {summary['ready_count']}")
    print(f"[quality_pairwise_promotion_queue] Blocked: {summary['blocked_count']}")
    print(f"[quality_pairwise_promotion_queue] Queued: {summary['queued_confirmation_count']}")
    print(f"[quality_pairwise_promotion_queue] Summary: {summary['artifacts']['summary']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
