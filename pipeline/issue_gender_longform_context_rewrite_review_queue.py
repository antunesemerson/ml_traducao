from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "issue_gender_longform_context_rewrite_review_queue_v1"
CHECKPOINT_RULE_VERSION = "issue_gender_longform_blocker_repair_checkpoint_v1"


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def report_paths(settings: dict[str, Any], checkpoint_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{now_stamp()}_issue_gender_longform_context_rewrite_review_queue_checkpoint_{checkpoint_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_checkpoint_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_gender_longform_blocker_repair_checkpoint_runs
        WHERE rule_version = ?
          AND checkpoint_review_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (CHECKPOINT_RULE_VERSION,),
    ).fetchone()
    if row is None:
        raise RuntimeError("No longform blocker repair checkpoint with review items found.")
    return int(row["id"])


def fetch_rows(conn, checkpoint_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.id AS checkpoint_item_id,
            item.checkpoint_run_id,
            item.dry_run_id,
            item.dry_run_item_id,
            item.source_checkpoint_run_id,
            item.source_checkpoint_item_id,
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.source_line_number,
            item.repair_status,
            item.route_key,
            item.subpolicy_name,
            item.old_fragment,
            item.new_fragment,
            item.current_text,
            item.corrected_text,
            item.token_delta_status,
            source.english_text,
            source.spanish_text,
            state.final_state,
            state.needs_output_apply,
            state.reasons_json AS segment_state_reasons_json
        FROM ml_issue_gender_longform_blocker_repair_checkpoint_items item
        JOIN source_segments source
          ON source.id = item.segment_id
        LEFT JOIN segment_state_items state
          ON state.segment_id = item.segment_id
         AND state.run_id = (
             SELECT MAX(id)
             FROM segment_state_runs
         )
        WHERE item.checkpoint_run_id = ?
          AND item.checkpoint_allowed = 0
          AND item.repair_status LIKE 'candidate_%'
        ORDER BY item.route_key, item.segment_id
        """,
        (checkpoint_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_gender_longform_context_rewrite_review_queue_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            checkpoint_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            route_counts_json TEXT,
            subpolicy_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_gender_longform_context_rewrite_review_queue_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            checkpoint_run_id INTEGER NOT NULL,
            checkpoint_item_id INTEGER NOT NULL,
            dry_run_id INTEGER NOT NULL,
            dry_run_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER NOT NULL DEFAULT 0,
            review_action TEXT NOT NULL,
            suggested_decision TEXT NOT NULL,
            route_key TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            repair_status TEXT NOT NULL,
            old_fragment TEXT,
            proposed_fragment TEXT,
            current_text TEXT NOT NULL,
            english_text TEXT,
            spanish_text TEXT,
            final_state TEXT,
            needs_output_apply INTEGER NOT NULL DEFAULT 0,
            reviewer_notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_gender_longform_context_rewrite_review_queue_runs(id) ON DELETE CASCADE
        )
        """
    )


def suggested_decision(row: dict[str, Any]) -> tuple[str, str, str]:
    status = row.get("repair_status") or ""
    subpolicy = row.get("subpolicy_name") or ""
    if subpolicy == "longform_demonstrative_article_context_rewrite":
        return (
            "review_and_accept_if_fluent",
            "Validate demonstrative article plus gender suffix; proposed fragment repairs missing ES_OA and object complement.",
            "accept_or_edit",
        )
    if subpolicy == "laamp_target_dead_select_cstring_rewrite":
        return (
            "review_dynamic_word_order",
            "Validate that the Select_CString noun phrase reads naturally for male/female target.",
            "accept_or_edit",
        )
    if "quote_punctuation" in subpolicy:
        return (
            "review_sentence_rewrite",
            "Validate quote conversion, pronoun dynamics, and PT-BR fluency before any apply.",
            "accept_or_edit",
        )
    return ("review_context_rewrite", f"Review candidate status {status}.", "accept_or_reject")


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    review_action, notes, suggested = suggested_decision(row)
    return {
        "checkpoint_run_id": int(row["checkpoint_run_id"]),
        "checkpoint_item_id": int(row["checkpoint_item_id"]),
        "dry_run_id": int(row["dry_run_id"]),
        "dry_run_item_id": int(row["dry_run_item_id"]),
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path") or "",
        "source_key": row.get("source_key") or "",
        "source_line_number": int(row.get("source_line_number") or 0),
        "review_action": review_action,
        "suggested_decision": suggested,
        "route_key": row.get("route_key") or "",
        "subpolicy_name": row.get("subpolicy_name") or "",
        "repair_status": row.get("repair_status") or "",
        "old_fragment": row.get("old_fragment") or "",
        "proposed_fragment": row.get("new_fragment") or "",
        "current_text": row.get("current_text") or "",
        "english_text": row.get("english_text") or "",
        "spanish_text": row.get("spanish_text") or "",
        "final_state": row.get("final_state") or "",
        "needs_output_apply": int(row.get("needs_output_apply") or 0),
        "reviewer_notes": notes,
    }


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    checkpoint_run_id: int,
    rows: list[dict[str, Any]],
) -> None:
    fields = [
        "review_action",
        "suggested_decision",
        "route_key",
        "subpolicy_name",
        "repair_status",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "old_fragment",
        "proposed_fragment",
        "current_text",
        "english_text",
        "spanish_text",
        "final_state",
        "needs_output_apply",
        "reviewer_notes",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    route_counts = Counter(row["route_key"] for row in rows)
    subpolicy_counts = Counter(row["subpolicy_name"] for row in rows)
    lines = [
        "Issue gender longform context rewrite review queue",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        "",
        "By route:",
        *[f"- {key}: {value:,}" for key, value in route_counts.most_common()],
        "",
        "By subpolicy:",
        *[f"- {key}: {value:,}" for key, value in subpolicy_counts.most_common()],
        "",
        "Review items:",
    ]
    for row in rows:
        lines.extend(
            [
                f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']}",
                f"  action: {row['review_action']} | suggested: {row['suggested_decision']}",
                f"  old: {row['old_fragment']}",
                f"  proposed: {row['proposed_fragment']}",
                f"  english: {short(row['english_text'], 260)}",
                f"  current: {short(row['current_text'], 280)}",
                f"  notes: {row['reviewer_notes']}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Review queue only: no production run, no confirmations, no source/output writes.",
            "- These items require human/assisted approval or editing before any protected apply.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, checkpoint_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_checkpoint_run_id = checkpoint_run_id or latest_checkpoint_run_id(conn)
        rows = [normalize_row(row) for row in fetch_rows(conn, selected_checkpoint_run_id)]
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_checkpoint_run_id)
        route_counts = Counter(row["route_key"] for row in rows)
        subpolicy_counts = Counter(row["subpolicy_name"] for row in rows)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_gender_longform_context_rewrite_review_queue_runs (
                rule_version,
                checkpoint_run_id,
                candidate_count,
                route_counts_json,
                subpolicy_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_checkpoint_run_id,
                len(rows),
                json.dumps(dict(route_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(subpolicy_counts), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
        for row in rows:
            conn.execute(
                """
                INSERT INTO ml_issue_gender_longform_context_rewrite_review_queue_items (
                    run_id,
                    checkpoint_run_id,
                    checkpoint_item_id,
                    dry_run_id,
                    dry_run_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    review_action,
                    suggested_decision,
                    route_key,
                    subpolicy_name,
                    repair_status,
                    old_fragment,
                    proposed_fragment,
                    current_text,
                    english_text,
                    spanish_text,
                    final_state,
                    needs_output_apply,
                    reviewer_notes,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    row["checkpoint_run_id"],
                    row["checkpoint_item_id"],
                    row["dry_run_id"],
                    row["dry_run_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["review_action"],
                    row["suggested_decision"],
                    row["route_key"],
                    row["subpolicy_name"],
                    row["repair_status"],
                    row["old_fragment"],
                    row["proposed_fragment"],
                    row["current_text"],
                    row["english_text"],
                    row["spanish_text"],
                    row["final_state"],
                    row["needs_output_apply"],
                    row["reviewer_notes"],
                    now,
                ),
            )
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            checkpoint_run_id=selected_checkpoint_run_id,
            rows=rows,
        )
        conn.commit()

    print(f"Gender longform context rewrite review queue: {run_id}")
    print(f"Checkpoint run id: {selected_checkpoint_run_id}")
    print(f"Candidates: {len(rows)}")
    print(f"Report: {txt_path}")
    return {
        "run_id": run_id,
        "checkpoint_run_id": selected_checkpoint_run_id,
        "candidate_count": len(rows),
        "report_path": str(txt_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create review queue for longform gender context rewrites.")
    parser.add_argument("--checkpoint-run-id", type=int, default=None)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    main(checkpoint_run_id=args.checkpoint_run_id)
