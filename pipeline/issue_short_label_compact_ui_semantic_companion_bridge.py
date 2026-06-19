from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_short_label_compact_ui_semantic_companion_bridge_v1"
BRIDGE_NAME = "short_label_compact_ui_semantic_companion_bridge_v1"
READY_STATUS = "ready_for_lifecycle_bridge"


def latest_finished_run_id(conn, table_name: str, where_sql: str = "1 = 1") -> int:
    row = conn.execute(
        f"""
        SELECT id
        FROM {table_name}
        WHERE {where_sql}
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No finished run found in {table_name}.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_compact_ui_semantic_companion_bridge_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            bridge_name TEXT NOT NULL,
            source_short_label_checkpoint_run_id INTEGER NOT NULL,
            source_companion_decision_run_id INTEGER NOT NULL,
            source_companion_queue_run_id INTEGER NOT NULL,
            source_ledger_run_id INTEGER NOT NULL,
            current_segment_state_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            ready_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            estimated_closed_gain INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            status_counts_json TEXT,
            block_counts_json TEXT,
            companion_family_counts_json TEXT,
            report_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_compact_ui_semantic_companion_bridge_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            source_short_label_checkpoint_run_id INTEGER NOT NULL,
            source_short_label_checkpoint_item_id INTEGER NOT NULL,
            source_companion_decision_run_id INTEGER NOT NULL,
            source_companion_decision_id INTEGER NOT NULL,
            source_companion_queue_run_id INTEGER NOT NULL,
            source_ledger_run_id INTEGER NOT NULL,
            current_segment_state_run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT,
            source_key TEXT,
            bridge_status TEXT NOT NULL,
            block_reason TEXT,
            companion_issue_family TEXT,
            companion_issue_kind TEXT,
            short_label_ledger_item_id INTEGER,
            companion_ledger_item_id INTEGER,
            segment_open_issue_count INTEGER NOT NULL DEFAULT 0,
            short_label_issue_count INTEGER NOT NULL DEFAULT 0,
            companion_issue_count INTEGER NOT NULL DEFAULT 0,
            high_issue_count INTEGER NOT NULL DEFAULT 0,
            token_not_ok_count INTEGER NOT NULL DEFAULT 0,
            current_final_state TEXT,
            current_state_group TEXT,
            current_review_state TEXT,
            current_apply_state TEXT,
            current_needs_output_apply INTEGER NOT NULL DEFAULT 0,
            current_confirmed_matches_output INTEGER NOT NULL DEFAULT 0,
            evidence_text TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_short_label_compact_ui_semantic_companion_bridge_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_compact_ui_semantic_companion_bridge"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl")


def fetch_companion_run(conn, decision_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_review_decision_runs
        WHERE id = ?
        """,
        (decision_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Companion decision run not found: {decision_run_id}")
    return dict(row)


def fetch_candidates(
    conn,
    *,
    short_label_checkpoint_run_id: int,
    companion_decision_run_id: int,
    companion_queue_run_id: int,
    ledger_run_id: int,
    state_run_id: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH issue_counts AS (
            SELECT
                segment_id,
                COUNT(*) AS open_issue_count,
                SUM(CASE WHEN issue_family = 'short_label_style_microagent' THEN 1 ELSE 0 END) AS short_label_issue_count,
                SUM(CASE WHEN issue_family <> 'short_label_style_microagent' THEN 1 ELSE 0 END) AS companion_issue_count,
                SUM(CASE WHEN lower(COALESCE(issue_severity, '')) IN ('high', 'error', 'critical') THEN 1 ELSE 0 END) AS high_issue_count,
                SUM(CASE WHEN COALESCE(token_status, '') <> 'ok' THEN 1 ELSE 0 END) AS token_not_ok_count
            FROM ml_issue_ledger_items
            WHERE run_id = ?
              AND status = 'open'
            GROUP BY segment_id
        )
        SELECT
            short_item.id AS source_short_label_checkpoint_item_id,
            short_item.ledger_item_id AS short_label_ledger_item_id,
            short_item.segment_id,
            short_item.relative_path,
            short_item.source_key,
            companion.id AS source_companion_decision_id,
            companion.ledger_item_id AS companion_ledger_item_id,
            companion.issue_family AS companion_issue_family,
            companion.issue_kind AS companion_issue_kind,
            companion.evidence_label AS companion_evidence_label,
            companion.normalized_decision AS companion_normalized_decision,
            companion.valid AS companion_valid,
            companion.validation_status AS companion_validation_status,
            state.final_state AS current_final_state,
            state.state_group AS current_state_group,
            state.review_state AS current_review_state,
            state.apply_state AS current_apply_state,
            COALESCE(state.needs_output_apply, 0) AS current_needs_output_apply,
            COALESCE(state.confirmed_matches_output, 0) AS current_confirmed_matches_output,
            COALESCE(state.locked, 0) AS current_locked,
            COALESCE(issue_counts.open_issue_count, 0) AS segment_open_issue_count,
            COALESCE(issue_counts.short_label_issue_count, 0) AS short_label_issue_count,
            COALESCE(issue_counts.companion_issue_count, 0) AS companion_issue_count,
            COALESCE(issue_counts.high_issue_count, 0) AS high_issue_count,
            COALESCE(issue_counts.token_not_ok_count, 0) AS token_not_ok_count,
            companion_queue.evidence_text
        FROM ml_issue_short_label_compact_ui_semantic_reviewed_checkpoint_items short_item
        JOIN ml_issue_review_decisions companion
          ON companion.segment_id = short_item.segment_id
         AND companion.run_id = ?
         AND companion.queue_run_id = ?
        JOIN ml_issue_review_queue_items companion_queue
          ON companion_queue.id = companion.queue_item_id
        JOIN segment_state_items state
          ON state.run_id = ?
         AND state.segment_id = short_item.segment_id
        LEFT JOIN issue_counts ON issue_counts.segment_id = short_item.segment_id
        WHERE short_item.checkpoint_run_id = ?
          AND short_item.checkpoint_allowed = 1
        ORDER BY short_item.segment_id
        """,
        (ledger_run_id, companion_decision_run_id, companion_queue_run_id, state_run_id, short_label_checkpoint_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def classify(row: dict[str, Any]) -> tuple[str, str | None]:
    reasons: list[str] = []
    if int(row.get("companion_valid") or 0) != 1 or row.get("companion_validation_status") != "accepted":
        reasons.append("companion_decision_not_accepted")
    if row.get("companion_normalized_decision") != "composition_ready":
        reasons.append("companion_not_composition_ready")
    if row.get("companion_evidence_label") != "composition_ready":
        reasons.append("companion_evidence_not_composition_ready")
    if row.get("current_state_group") != "pending":
        reasons.append("not_pending")
    if row.get("current_final_state") != "reopen_auto_confirmed_autofix":
        reasons.append("state_not_reopen_auto_confirmed_autofix")
    if row.get("current_review_state") in {"human_locked", "human_confirmed"} or int(row.get("current_locked") or 0):
        reasons.append("human_locked_or_confirmed")
    if int(row.get("current_needs_output_apply") or 0) != 0:
        reasons.append("needs_output_apply")
    if int(row.get("current_confirmed_matches_output") or 0) != 1:
        reasons.append("confirmed_output_mismatch")
    if int(row.get("segment_open_issue_count") or 0) != 2:
        reasons.append("open_issue_count_not_two")
    if int(row.get("short_label_issue_count") or 0) != 1:
        reasons.append("short_label_issue_count_not_one")
    if int(row.get("companion_issue_count") or 0) != 1:
        reasons.append("companion_issue_count_not_one")
    if int(row.get("high_issue_count") or 0) != 0:
        reasons.append("high_issue_present")
    if int(row.get("token_not_ok_count") or 0) != 0:
        reasons.append("token_status_not_ok")
    if reasons:
        return "blocked", ";".join(reasons)
    return READY_STATUS, None


def build_items(
    *,
    rows: list[dict[str, Any]],
    run_id: int,
    short_label_checkpoint_run_id: int,
    companion_decision_run_id: int,
    companion_queue_run_id: int,
    ledger_run_id: int,
    state_run_id: int,
) -> list[dict[str, Any]]:
    created_at = db.utc_now()
    items: list[dict[str, Any]] = []
    for row in rows:
        status, block_reason = classify(row)
        items.append(
            {
                "run_id": run_id,
                "source_short_label_checkpoint_run_id": short_label_checkpoint_run_id,
                "source_short_label_checkpoint_item_id": row["source_short_label_checkpoint_item_id"],
                "source_companion_decision_run_id": companion_decision_run_id,
                "source_companion_decision_id": row["source_companion_decision_id"],
                "source_companion_queue_run_id": companion_queue_run_id,
                "source_ledger_run_id": ledger_run_id,
                "current_segment_state_run_id": state_run_id,
                "segment_id": row["segment_id"],
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "bridge_status": status,
                "block_reason": block_reason,
                "companion_issue_family": row.get("companion_issue_family"),
                "companion_issue_kind": row.get("companion_issue_kind"),
                "short_label_ledger_item_id": row.get("short_label_ledger_item_id"),
                "companion_ledger_item_id": row.get("companion_ledger_item_id"),
                "segment_open_issue_count": int(row.get("segment_open_issue_count") or 0),
                "short_label_issue_count": int(row.get("short_label_issue_count") or 0),
                "companion_issue_count": int(row.get("companion_issue_count") or 0),
                "high_issue_count": int(row.get("high_issue_count") or 0),
                "token_not_ok_count": int(row.get("token_not_ok_count") or 0),
                "current_final_state": row.get("current_final_state"),
                "current_state_group": row.get("current_state_group"),
                "current_review_state": row.get("current_review_state"),
                "current_apply_state": row.get("current_apply_state"),
                "current_needs_output_apply": int(row.get("current_needs_output_apply") or 0),
                "current_confirmed_matches_output": int(row.get("current_confirmed_matches_output") or 0),
                "evidence_text": row.get("evidence_text"),
                "created_at": created_at,
            }
        )
    return items


def insert_rows(conn, table_name: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    columns = list(rows[0].keys())
    placeholders = ", ".join("?" for _ in columns)
    conn.executemany(
        f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({placeholders})",
        [tuple(row[column] for column in columns) for row in rows],
    )


def write_reports(txt_path: Path, jsonl_path: Path, *, run_id: int, items: list[dict[str, Any]]) -> None:
    status_counts = Counter(item["bridge_status"] for item in items)
    block_counts = Counter(item["block_reason"] or "none" for item in items)
    family_counts = Counter(item["companion_issue_family"] or "unknown" for item in items if item["bridge_status"] == READY_STATUS)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    lines = [
        "Short-label compact UI semantic companion bridge",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Bridge run id: {run_id}",
        "",
        "Summary:",
        f"- candidates: {len(items):,}",
        f"- ready: {status_counts.get(READY_STATUS, 0):,}",
        f"- blocked: {sum(count for status, count in status_counts.items() if status != READY_STATUS):,}",
        f"- estimated_closed_gain: {status_counts.get(READY_STATUS, 0):,}",
        "- production_release_allowed: 0",
        "",
        "Ready companion families:",
        *[f"- {name}: {count:,}" for name, count in family_counts.most_common()],
        "",
        "Block reasons:",
        *[f"- {name}: {count:,}" for name, count in block_counts.most_common()],
        "",
        "Safety:",
        "- Bridge only; no lifecycle closure.",
        "- No source/output writes.",
        "- Chat 2 must still integrate segment-state read-only before pending count changes.",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    short_label_checkpoint_run_id: int | None,
    companion_decision_run_id: int | None,
    state_run_id: int | None,
) -> dict[str, Any]:
    settings = db.load_settings()
    txt_path, jsonl_path = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_checkpoint_run_id = short_label_checkpoint_run_id or latest_finished_run_id(
            conn,
            "ml_issue_short_label_compact_ui_semantic_reviewed_checkpoint_runs",
            "finished_at IS NOT NULL AND checkpoint_allowed_count > 0",
        )
        selected_decision_run_id = companion_decision_run_id or latest_finished_run_id(
            conn,
            "ml_issue_review_decision_runs",
            "finished_at IS NOT NULL",
        )
        companion_run = fetch_companion_run(conn, selected_decision_run_id)
        companion_queue_run_id = int(companion_run["queue_run_id"])
        state_run = state_run_id or latest_finished_run_id(conn, "segment_state_runs", "finished_at IS NOT NULL")
        checkpoint_run = conn.execute(
            "SELECT * FROM ml_issue_short_label_compact_ui_semantic_reviewed_checkpoint_runs WHERE id = ?",
            (selected_checkpoint_run_id,),
        ).fetchone()
        if checkpoint_run is None:
            raise RuntimeError(f"Short-label checkpoint not found: {selected_checkpoint_run_id}")
        ledger_run_id = int(checkpoint_run["ledger_run_id"])
        now = db.utc_now()
        run_id = int(
            conn.execute(
                """
                INSERT INTO ml_issue_short_label_compact_ui_semantic_companion_bridge_runs (
                    rule_version,
                    bridge_name,
                    source_short_label_checkpoint_run_id,
                    source_companion_decision_run_id,
                    source_companion_queue_run_id,
                    source_ledger_run_id,
                    current_segment_state_run_id,
                    candidate_count,
                    ready_count,
                    blocked_count,
                    estimated_closed_gain,
                    production_release_allowed,
                    status_counts_json,
                    block_counts_json,
                    companion_family_counts_json,
                    report_path,
                    jsonl_path,
                    started_at,
                    finished_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, '{}', '{}', '{}', ?, ?, ?, ?, ?)
                """,
                (
                    RULE_VERSION,
                    BRIDGE_NAME,
                    selected_checkpoint_run_id,
                    selected_decision_run_id,
                    companion_queue_run_id,
                    ledger_run_id,
                    state_run,
                    str(txt_path),
                    str(jsonl_path),
                    now,
                    now,
                    now,
                ),
            ).lastrowid
        )
        rows = fetch_candidates(
            conn,
            short_label_checkpoint_run_id=selected_checkpoint_run_id,
            companion_decision_run_id=selected_decision_run_id,
            companion_queue_run_id=companion_queue_run_id,
            ledger_run_id=ledger_run_id,
            state_run_id=state_run,
        )
        items = build_items(
            rows=rows,
            run_id=run_id,
            short_label_checkpoint_run_id=selected_checkpoint_run_id,
            companion_decision_run_id=selected_decision_run_id,
            companion_queue_run_id=companion_queue_run_id,
            ledger_run_id=ledger_run_id,
            state_run_id=state_run,
        )
        insert_rows(conn, "ml_issue_short_label_compact_ui_semantic_companion_bridge_items", items)
        status_counts = Counter(item["bridge_status"] for item in items)
        block_counts = Counter(item["block_reason"] or "none" for item in items)
        family_counts = Counter(item["companion_issue_family"] or "unknown" for item in items if item["bridge_status"] == READY_STATUS)
        ready_count = status_counts.get(READY_STATUS, 0)
        blocked_count = len(items) - ready_count
        conn.execute(
            """
            UPDATE ml_issue_short_label_compact_ui_semantic_companion_bridge_runs
            SET candidate_count = ?,
                ready_count = ?,
                blocked_count = ?,
                estimated_closed_gain = ?,
                status_counts_json = ?,
                block_counts_json = ?,
                companion_family_counts_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                len(items),
                ready_count,
                blocked_count,
                ready_count,
                json.dumps(dict(status_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(block_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(family_counts), ensure_ascii=False, sort_keys=True),
                db.utc_now(),
                run_id,
            ),
        )
        write_reports(txt_path, jsonl_path, run_id=run_id, items=items)
        conn.commit()

    print("[issue_short_label_compact_ui_semantic_companion_bridge] Bridge generated")
    print(f"[issue_short_label_compact_ui_semantic_companion_bridge] Bridge run id: {run_id}")
    print(f"[issue_short_label_compact_ui_semantic_companion_bridge] Short-label checkpoint run id: {selected_checkpoint_run_id}")
    print(f"[issue_short_label_compact_ui_semantic_companion_bridge] Companion decision run id: {selected_decision_run_id}")
    print(f"[issue_short_label_compact_ui_semantic_companion_bridge] Candidates: {len(items):,}")
    print(f"[issue_short_label_compact_ui_semantic_companion_bridge] Ready: {ready_count:,}")
    print(f"[issue_short_label_compact_ui_semantic_companion_bridge] Blocked: {blocked_count:,}")
    print(f"[issue_short_label_compact_ui_semantic_companion_bridge] Report: {txt_path}")
    return {
        "run_id": run_id,
        "ready": ready_count,
        "blocked": blocked_count,
        "report_path": str(txt_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build companion bridge for compact UI semantic short-label coverage.")
    parser.add_argument("--short-label-checkpoint-run-id", type=int)
    parser.add_argument("--companion-decision-run-id", type=int)
    parser.add_argument("--segment-state-run-id", type=int)
    args = parser.parse_args()
    main(
        short_label_checkpoint_run_id=args.short_label_checkpoint_run_id,
        companion_decision_run_id=args.companion_decision_run_id,
        state_run_id=args.segment_state_run_id,
    )
