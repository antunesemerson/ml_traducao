from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator
from apply_segment_state_updates import short, structural_tokens


RULE_VERSION = "issue_select_cstring_residual_literal_repair_checkpoint_v1"
CHECKPOINT_NAME = "select_cstring_residual_literal_partial_repair_checkpoint_v1"
CHECKPOINT_STATUS = "shadow_partial_repair"
PRODUCTION_RELEASE_ALLOWED = 0


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def latest_queue_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_select_cstring_residual_literal_queue_runs
        WHERE queue_status = 'learning_read_only'
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No Select_CString residual literal queue run found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_residual_literal_repair_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            source_queue_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            candidate_segment_count INTEGER NOT NULL DEFAULT 0,
            repair_allowed_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            segment_closure_candidate_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            block_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_residual_literal_repair_checkpoint_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            source_queue_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            repair_allowed INTEGER NOT NULL DEFAULT 0,
            segment_closure_candidate INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            ready_observation_count INTEGER NOT NULL DEFAULT 0,
            current_text TEXT NOT NULL,
            corrected_text TEXT,
            validation_issues_json TEXT,
            replacements_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_select_cstring_residual_literal_repair_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def fetch_queue_run(conn, queue_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_select_cstring_residual_literal_queue_runs
        WHERE id = ?
        """,
        (queue_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Queue run not found: {queue_run_id}")
    return dict(row)


def fetch_ready_items(conn, queue_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_select_cstring_residual_literal_queue_items
        WHERE run_id = ?
          AND literal_status = 'ready_literal_map_shadow'
        ORDER BY segment_id, select_index
        """,
        (queue_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def group_by_segment(rows: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(int(row["segment_id"]), []).append(row)
    return grouped


def blocking_validation_issues(text: str) -> list[dict[str, Any]]:
    validation = local_quality_validator.validate_text(text)
    issues = validation.get("issues") or []
    return [
        issue
        for issue in issues
        if issue.get("severity") == "high"
        or issue.get("code")
        in {
            "spanish_residue",
            "spanish_residue_in_literal",
            "spanish_punctuation",
            "mojibake_or_unexpected_script",
            "utf8_mojibake_sequence",
            "replacement_question_mark_mojibake",
            "token_breakage",
            "placeholder_breakage",
        }
    ]


def compose_segment(items: list[dict[str, Any]]) -> dict[str, Any]:
    first = items[0]
    current = as_text(first["current_text"])
    corrected = current
    replacements: list[dict[str, Any]] = []
    block_reasons: list[str] = []

    if first["segment_status"] != "ready_segment_literal_map_shadow":
        block_reasons.append(f"segment_status_not_ready:{first['segment_status']}")

    for item in items:
        old = as_text(item["current_select_text"])
        new = as_text(item["corrected_select_text"])
        if not old or not new:
            block_reasons.append(f"missing_replacement_payload:{item['select_index']}")
            continue
        if old not in corrected:
            block_reasons.append(f"replacement_not_found:{item['select_index']}")
            continue
        corrected = corrected.replace(old, new, 1)
        replacements.append(
            {
                "select_index": item["select_index"],
                "old": old,
                "new": new,
                "left_literal": item["left_literal"],
                "right_literal": item["right_literal"],
                "left_repair": item["left_repair"],
                "right_repair": item["right_repair"],
            }
        )

    if corrected == current:
        block_reasons.append("no_text_delta")
    if structural_tokens(current) != structural_tokens(corrected):
        block_reasons.append("structural_tokens_changed")

    validation_issues = blocking_validation_issues(corrected)
    repair_allowed = 0 if block_reasons else 1
    segment_closure_candidate = 1 if repair_allowed and not validation_issues else 0

    return {
        "segment_id": int(first["segment_id"]),
        "relative_path": first["relative_path"],
        "source_key": first["source_key"],
        "source_line_number": first["source_line_number"],
        "repair_allowed": repair_allowed,
        "segment_closure_candidate": segment_closure_candidate,
        "block_reason": ";".join(block_reasons),
        "ready_observation_count": len(items),
        "current_text": current,
        "corrected_text": corrected if repair_allowed else "",
        "validation_issues": validation_issues,
        "replacements": replacements,
    }


def report_paths(settings: dict[str, Any], queue_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{now_stamp()}_issue_select_cstring_residual_literal_repair_checkpoint_queue_{queue_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    queue_run: dict[str, Any],
    rows: list[dict[str, Any]],
    block_counts: Counter[str],
) -> None:
    fields = [
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "repair_allowed",
        "segment_closure_candidate",
        "block_reason",
        "ready_observation_count",
        "current_text",
        "corrected_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    allowed = [row for row in rows if row["repair_allowed"]]
    closure = [row for row in rows if row["segment_closure_candidate"]]
    lines = [
        "Issue Select_CString residual literal repair checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Source queue run id: {queue_run['id']}",
        f"Segment-state run id: {queue_run['segment_state_run_id']}",
        f"Ledger run id: {queue_run['ledger_run_id']}",
        "",
        "Summary:",
        f"- Candidate segments: {len(rows):,}",
        f"- Repair allowed: {len(allowed):,}",
        f"- Blocked: {len(rows) - len(allowed):,}",
        f"- Segment closure candidates after this repair only: {len(closure):,}",
        "- Apply allowed: 0",
        "- Production release allowed: 0",
        "",
        "Block reasons:",
        *[f"- {key}: {value:,}" for key, value in block_counts.most_common()],
        "",
        "Allowed samples:",
    ]
    for row in allowed[:40]:
        lines.extend(
            [
                f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']} observations={row['ready_observation_count']}",
                f"  current: {short(row['current_text'], 220)}",
                f"  corrected: {short(row['corrected_text'], 220)}",
                f"  closure_candidate: {row['segment_closure_candidate']}",
            ]
        )
    lines.extend(["", "Blocked samples:"])
    for row in [row for row in rows if not row["repair_allowed"]][:30]:
        lines.extend(
            [
                f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']}",
                f"  block: {row['block_reason']}",
                f"  current: {short(row['current_text'], 220)}",
            ]
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, queue_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_queue_run_id = queue_run_id or latest_queue_run_id(conn)
        queue_run = fetch_queue_run(conn, selected_queue_run_id)
        ready_items = fetch_ready_items(conn, selected_queue_run_id)
        rows = [
            compose_segment(items)
            for _segment_id, items in sorted(group_by_segment(ready_items).items())
        ]
        block_counts = Counter(row["block_reason"] or "none" for row in rows if not row["repair_allowed"])
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_queue_run_id)
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            queue_run=queue_run,
            rows=rows,
            block_counts=block_counts,
        )
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_select_cstring_residual_literal_repair_checkpoint_runs (
                rule_version,
                checkpoint_name,
                checkpoint_status,
                source_queue_run_id,
                segment_state_run_id,
                ledger_run_id,
                candidate_segment_count,
                repair_allowed_count,
                blocked_count,
                segment_closure_candidate_count,
                production_release_allowed,
                block_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                CHECKPOINT_NAME,
                CHECKPOINT_STATUS,
                selected_queue_run_id,
                queue_run["segment_state_run_id"],
                queue_run["ledger_run_id"],
                len(rows),
                sum(1 for row in rows if row["repair_allowed"]),
                sum(1 for row in rows if not row["repair_allowed"]),
                sum(1 for row in rows if row["segment_closure_candidate"]),
                PRODUCTION_RELEASE_ALLOWED,
                json.dumps(block_counts, ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        run_id = int(cur.lastrowid)
        for row in rows:
            conn.execute(
                """
                INSERT INTO ml_issue_select_cstring_residual_literal_repair_checkpoint_items (
                    run_id,
                    source_queue_run_id,
                    segment_state_run_id,
                    ledger_run_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    repair_allowed,
                    segment_closure_candidate,
                    block_reason,
                    ready_observation_count,
                    current_text,
                    corrected_text,
                    validation_issues_json,
                    replacements_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_queue_run_id,
                    queue_run["segment_state_run_id"],
                    queue_run["ledger_run_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["repair_allowed"],
                    row["segment_closure_candidate"],
                    row["block_reason"],
                    row["ready_observation_count"],
                    row["current_text"],
                    row["corrected_text"],
                    json.dumps(row["validation_issues"], ensure_ascii=False),
                    json.dumps(row["replacements"], ensure_ascii=False),
                    now,
                ),
            )
        conn.commit()

    print("[issue_select_cstring_residual_literal_repair_checkpoint] Checkpoint generated")
    print(f"[issue_select_cstring_residual_literal_repair_checkpoint] Rule version: {RULE_VERSION}")
    print(f"[issue_select_cstring_residual_literal_repair_checkpoint] Run id: {run_id}")
    print(f"[issue_select_cstring_residual_literal_repair_checkpoint] Source queue run id: {selected_queue_run_id}")
    print(f"[issue_select_cstring_residual_literal_repair_checkpoint] Candidate segments: {len(rows):,}")
    print(f"[issue_select_cstring_residual_literal_repair_checkpoint] Repair allowed: {sum(1 for row in rows if row['repair_allowed']):,}")
    print(f"[issue_select_cstring_residual_literal_repair_checkpoint] Segment closure candidates: {sum(1 for row in rows if row['segment_closure_candidate']):,}")
    print("[issue_select_cstring_residual_literal_repair_checkpoint] Apply allowed: 0")
    print(f"[issue_select_cstring_residual_literal_repair_checkpoint] Report: {txt_path}")
    print(f"[issue_select_cstring_residual_literal_repair_checkpoint] CSV: {csv_path}")
    print(f"[issue_select_cstring_residual_literal_repair_checkpoint] JSONL: {jsonl_path}")
    return {
        "run_id": run_id,
        "source_queue_run_id": selected_queue_run_id,
        "candidate_segments": len(rows),
        "repair_allowed": sum(1 for row in rows if row["repair_allowed"]),
        "segment_closure_candidates": sum(1 for row in rows if row["segment_closure_candidate"]),
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a read-only partial repair checkpoint for Select_CString residual literal mappings.")
    parser.add_argument("--queue-run-id", type=int, default=None)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id)
