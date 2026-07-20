from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "quality_pairwise_monotonic_gate_v1"
DEFAULT_EVIDENCE_TYPE = "deterministic_space_before_punctuation_repair"


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
          evidence.baseline_score_raw,
          evidence.candidate_score_raw,
          evidence.pairwise_score,
          evidence.pairwise_delta,
          evidence.calibration_band,
          evidence.token_integrity_ok,
          evidence.post_validation_clean,
          evidence.auto_safe_eligible,
          output.portuguese_text AS current_output_text,
          confirmation.confirmation_level,
          confirmation.confirmed_text,
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
        WHERE evidence.training_eligible = 1
          AND evidence.evidence_type = ?
        ORDER BY evidence.baseline_score_raw DESC, evidence.segment_id
        """,
        (state_run_id, evidence_type),
    ).fetchall()
    records: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        guards = {
            "baseline_is_current_output": row["current_output_text"] == row["baseline_text"],
            "candidate_changes_output": row["candidate_text"] != row["current_output_text"],
            "segment_baseline_is_closed": row["state_group"] == "closed",
            "no_existing_output_apply": int(row["needs_output_apply"] or 0) == 0,
            "no_locked_confirmation": int(row["confirmation_locked"] or 0) == 0,
            "token_integrity_ok": int(row["token_integrity_ok"] or 0) == 1,
            "post_validation_clean": int(row["post_validation_clean"] or 0) == 1,
            "pairwise_candidate_preferred": float(row["pairwise_score"] or 0)
            > float(row["baseline_score_raw"] or 0),
            "absolute_auto_safe_not_granted": int(row["auto_safe_eligible"] or 0) == 0,
        }
        ready = all(guards.values())
        records.append(
            {
                **row,
                "guards": guards,
                "ready": ready,
                "block_reasons": [name for name, passed in guards.items() if not passed],
                "promotion_kind": "monotonic_local_repair" if ready else "blocked",
                "confirmation_write_count": 0,
                "output_write_count": 0,
            }
        )
    return records


def persist_decisions(
    conn: sqlite3.Connection,
    records: list[dict[str, Any]],
    evidence_type: str,
) -> tuple[int, int]:
    ready_ids = [int(row["evidence_id"]) for row in records if row["ready"]]
    blocked_ids = [int(row["evidence_id"]) for row in records if not row["ready"]]
    if ready_ids:
        placeholders = ",".join("?" for _ in ready_ids)
        conn.execute(
            f"""
            UPDATE ml_pairwise_quality_evidence
            SET promotion_eligible = 1,
                recommended_route = 'monotonic_repair_promotion_ready',
                last_seen_at = ?
            WHERE id IN ({placeholders})
            """,
            (db.utc_now(), *ready_ids),
        )
    if blocked_ids:
        placeholders = ",".join("?" for _ in blocked_ids)
        conn.execute(
            f"""
            UPDATE ml_pairwise_quality_evidence
            SET promotion_eligible = 0,
                last_seen_at = ?
            WHERE id IN ({placeholders})
            """,
            (db.utc_now(), *blocked_ids),
        )
    latest_run = conn.execute(
        """
        SELECT id
        FROM ml_pairwise_quality_runs
        WHERE evidence_type = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (evidence_type,),
    ).fetchone()
    if latest_run:
        conn.execute(
            """
            UPDATE ml_pairwise_quality_runs
            SET promotion_eligible_count = ?, auto_safe_eligible_count = 0, updated_at = ?
            WHERE id = ?
            """,
            (len(ready_ids), db.utc_now(), int(latest_run["id"])),
        )
    conn.commit()
    return len(ready_ids), len(blocked_ids)


def write_reports(
    records: list[dict[str, Any]],
    state_run_id: int,
    evidence_type: str,
    apply: bool,
) -> dict[str, Any]:
    reports_dir = db.project_path(db.load_settings()["reports_dir"])
    base = reports_dir / f"{stamp()}_quality_pairwise_monotonic_gate"
    markdown_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = base.with_name(base.name + "_summary.json")
    ready = [row for row in records if row["ready"]]
    blocked = [row for row in records if not row["ready"]]
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
        "block_reason_counts": dict(block_counts),
        "promotion_metadata_written": len(ready) if apply else 0,
        "auto_safe_eligible_count": 0,
        "confirmation_write_count": 0,
        "segment_state_write_count": 0,
        "output_write_count": 0,
        "database_write": apply,
        "artifacts": {
            "markdown": str(markdown_path),
            "jsonl": str(jsonl_path),
            "summary": str(summary_path),
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Pairwise monotonic promotion gate",
        "",
        f"- Segment-state: `{state_run_id}`",
        f"- Records: `{len(records)}`",
        f"- Ready: `{len(ready)}`",
        f"- Blocked: `{len(blocked)}`",
        f"- Promotion metadata written: `{summary['promotion_metadata_written']}`",
        "- Auto-safe granted: `0`",
        "- Confirmations written: `0`",
        "- Segment-state written: `0`",
        "- Output written: `0`",
        "",
        "## Block reasons",
        "",
    ]
    if block_counts:
        lines.extend(f"- `{name}`: `{count}`" for name, count in block_counts.most_common())
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Contract",
            "",
            "Ready means the accepted closed baseline remains current and the candidate is a strictly local,",
            "token-safe, post-validation-clean repair. It may enter promotion validation, but it does not",
            "grant whole-segment auto-safe status and does not write output.",
        ]
    )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Gate pairwise evidence as monotonic local promotions.")
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
            persist_decisions(conn, records, args.evidence_type)
    else:
        with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=120) as conn:
            conn.row_factory = sqlite3.Row
            state_run_id = latest_state_run_id(conn)
            records = evaluate_rows(conn, state_run_id, args.evidence_type)
    if not records:
        raise RuntimeError("No pairwise evidence matched the monotonic gate scope.")
    summary = write_reports(records, state_run_id, args.evidence_type, args.apply)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
