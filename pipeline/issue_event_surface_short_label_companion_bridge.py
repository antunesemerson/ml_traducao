from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "issue_event_surface_short_label_companion_bridge_v1"
BRIDGE_NAME = "event_surface_short_label_companion_bridge_v1"
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
        CREATE TABLE IF NOT EXISTS ml_issue_event_surface_short_label_companion_bridge_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            bridge_name TEXT NOT NULL,
            source_event_checkpoint_run_id INTEGER NOT NULL,
            source_coverage_run_id INTEGER NOT NULL,
            source_ledger_run_id INTEGER NOT NULL,
            current_segment_state_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            ready_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            estimated_closed_gain INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            status_counts_json TEXT,
            block_counts_json TEXT,
            agent_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_event_surface_short_label_companion_bridge_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            source_event_checkpoint_run_id INTEGER NOT NULL,
            source_event_checkpoint_item_id INTEGER NOT NULL,
            source_coverage_run_id INTEGER NOT NULL,
            source_ledger_run_id INTEGER NOT NULL,
            current_segment_state_run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT,
            source_key TEXT,
            source_line_number INTEGER,
            bridge_status TEXT NOT NULL,
            block_reason TEXT,
            event_agent_key TEXT,
            event_cluster_key TEXT,
            event_current_text TEXT,
            semantic_ledger_item_id INTEGER,
            short_label_ledger_item_id INTEGER,
            semantic_evidence_text TEXT,
            short_label_evidence_text TEXT,
            total_issue_count INTEGER NOT NULL DEFAULT 0,
            covered_issue_count INTEGER NOT NULL DEFAULT 0,
            open_issue_count INTEGER NOT NULL DEFAULT 0,
            blocked_issue_count INTEGER NOT NULL DEFAULT 0,
            current_final_state TEXT,
            current_state_group TEXT,
            current_review_state TEXT,
            current_apply_state TEXT,
            current_needs_output_apply INTEGER NOT NULL DEFAULT 0,
            current_confirmed_matches_output INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_event_surface_short_label_companion_bridge_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_event_surface_short_label_companion_bridge"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl")


def fetch_candidates(
    conn,
    *,
    event_checkpoint_run_id: int,
    coverage_run_id: int,
    ledger_run_id: int,
    state_run_id: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH event_allowed AS (
            SELECT
                segment_id,
                MIN(id) AS source_event_checkpoint_item_id,
                MAX(agent_key) AS event_agent_key,
                MAX(cluster_key) AS event_cluster_key,
                MAX(current_text) AS event_current_text
            FROM ml_issue_event_surface_decision_checkpoint_items
            WHERE run_id = ?
              AND checkpoint_allowed = 1
            GROUP BY segment_id
        ),
        ledger_pivot AS (
            SELECT
                segment_id,
                MAX(CASE WHEN issue_family = 'semantic_review_router' THEN id END) AS semantic_ledger_item_id,
                MAX(CASE WHEN issue_family = 'short_label_style_microagent' THEN id END) AS short_label_ledger_item_id,
                MAX(CASE WHEN issue_family = 'semantic_review_router' THEN evidence_text END) AS semantic_evidence_text,
                MAX(CASE WHEN issue_family = 'short_label_style_microagent' THEN evidence_text END) AS short_label_evidence_text,
                SUM(CASE WHEN COALESCE(token_status, '') <> 'ok' THEN 1 ELSE 0 END) AS token_not_ok_count,
                SUM(CASE WHEN COALESCE(validation_status, '') NOT IN ('not_validated', '') THEN 1 ELSE 0 END) AS unexpected_validation_count,
                SUM(CASE WHEN lower(COALESCE(issue_severity, '')) IN ('high', 'error', 'critical') THEN 1 ELSE 0 END) AS high_issue_count,
                COUNT(*) AS ledger_issue_count
            FROM ml_issue_ledger_items
            WHERE run_id = ?
              AND status = 'open'
              AND issue_family IN ('semantic_review_router', 'short_label_style_microagent')
            GROUP BY segment_id
        )
        SELECT
            event_allowed.source_event_checkpoint_item_id,
            event_allowed.event_agent_key,
            event_allowed.event_cluster_key,
            event_allowed.event_current_text,
            coverage.segment_id,
            coverage.relative_path,
            coverage.source_key,
            coverage.source_line_number,
            coverage.total_issue_count,
            coverage.covered_issue_count,
            coverage.open_issue_count,
            coverage.blocked_issue_count,
            coverage.coverage_state,
            coverage.covered_families_json,
            coverage.open_families_json,
            coverage.coverage_sources_json,
            ledger_pivot.semantic_ledger_item_id,
            ledger_pivot.short_label_ledger_item_id,
            ledger_pivot.semantic_evidence_text,
            ledger_pivot.short_label_evidence_text,
            COALESCE(ledger_pivot.token_not_ok_count, 0) AS token_not_ok_count,
            COALESCE(ledger_pivot.unexpected_validation_count, 0) AS unexpected_validation_count,
            COALESCE(ledger_pivot.high_issue_count, 0) AS high_issue_count,
            COALESCE(ledger_pivot.ledger_issue_count, 0) AS ledger_issue_count,
            state.final_state AS current_final_state,
            state.state_group AS current_state_group,
            state.review_state AS current_review_state,
            state.apply_state AS current_apply_state,
            COALESCE(state.needs_output_apply, 0) AS current_needs_output_apply,
            COALESCE(state.confirmed_matches_output, 0) AS current_confirmed_matches_output,
            COALESCE(state.locked, 0) AS current_locked
        FROM event_allowed
        JOIN ml_issue_partial_coverage_items coverage
          ON coverage.run_id = ?
         AND coverage.segment_id = event_allowed.segment_id
        JOIN segment_state_items state
          ON state.run_id = ?
         AND state.segment_id = event_allowed.segment_id
        LEFT JOIN ledger_pivot
          ON ledger_pivot.segment_id = event_allowed.segment_id
        WHERE state.state_group = 'pending'
        ORDER BY coverage.relative_path, coverage.source_line_number, coverage.segment_id
        """,
        (event_checkpoint_run_id, ledger_run_id, coverage_run_id, state_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def _json_count(text: str | None, key: str) -> int:
    try:
        payload = json.loads(text or "{}")
    except json.JSONDecodeError:
        return 0
    return int(payload.get(key) or 0)


def classify(row: dict[str, Any]) -> tuple[str, str | None]:
    reasons: list[str] = []
    if row.get("current_state_group") != "pending":
        reasons.append("not_pending")
    if row.get("current_review_state") in {"human_locked", "human_confirmed"} or int(row.get("current_locked") or 0):
        reasons.append("human_locked_or_confirmed")
    if int(row.get("current_needs_output_apply") or 0) != 0:
        reasons.append("needs_output_apply")
    if int(row.get("current_confirmed_matches_output") or 0) != 1:
        reasons.append("confirmed_output_mismatch")
    if row.get("coverage_state") != "partial":
        reasons.append("coverage_state_not_partial")
    if int(row.get("total_issue_count") or 0) != 2:
        reasons.append("total_issue_count_not_two")
    if int(row.get("covered_issue_count") or 0) != 1:
        reasons.append("covered_issue_count_not_one")
    if int(row.get("open_issue_count") or 0) != 1:
        reasons.append("open_issue_count_not_one")
    if int(row.get("blocked_issue_count") or 0) != 0:
        reasons.append("blocked_issue_count_not_zero")
    if _json_count(row.get("covered_families_json"), "semantic_review_router") != 1:
        reasons.append("semantic_router_not_covered")
    if _json_count(row.get("open_families_json"), "short_label_style_microagent") != 1:
        reasons.append("short_label_style_not_open")
    if not row.get("semantic_ledger_item_id") or not row.get("short_label_ledger_item_id"):
        reasons.append("missing_current_ledger_pair")
    if (row.get("semantic_evidence_text") or "") != (row.get("short_label_evidence_text") or ""):
        reasons.append("evidence_text_mismatch")
    if (row.get("event_current_text") or "") != (row.get("semantic_evidence_text") or ""):
        reasons.append("event_checkpoint_text_mismatch")
    if int(row.get("token_not_ok_count") or 0) != 0:
        reasons.append("token_status_not_ok")
    if int(row.get("unexpected_validation_count") or 0) != 0:
        reasons.append("unexpected_validation_status")
    if int(row.get("high_issue_count") or 0) != 0:
        reasons.append("high_issue_present")
    if int(row.get("ledger_issue_count") or 0) != 2:
        reasons.append("ledger_issue_count_not_two")
    if "event_surface_decision_checkpoint:carry_forward" not in (row.get("coverage_sources_json") or ""):
        reasons.append("missing_event_surface_coverage_source")

    if reasons:
        return "blocked", ";".join(reasons)
    return READY_STATUS, None


def write_reports(*, txt_path: Path, jsonl_path: Path, run_id: int, rows: list[dict[str, Any]]) -> None:
    status_counts = Counter(row["bridge_status"] for row in rows)
    block_counts = Counter(row["block_reason"] or "none" for row in rows)
    agent_counts = Counter(row["event_agent_key"] or "unknown" for row in rows if row["bridge_status"] == READY_STATUS)
    ready = [row for row in rows if row["bridge_status"] == READY_STATUS]
    blocked = [row for row in rows if row["bridge_status"] != READY_STATUS]

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Event surface short-label companion bridge",
        f"Rule version: {RULE_VERSION}",
        f"Bridge name: {BRIDGE_NAME}",
        f"Bridge run id: {run_id}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Ready for lifecycle bridge: {len(ready):,}",
        f"- Blocked: {len(blocked):,}",
        f"- Estimated closed gain: {len(ready):,}",
        "",
        "Status counts:",
        *[f"- {key}: {value:,}" for key, value in status_counts.most_common()],
        "",
        "Block counts:",
        *[f"- {key}: {value:,}" for key, value in block_counts.most_common()],
        "",
        "Ready by event agent:",
        *[f"- {key}: {value:,}" for key, value in agent_counts.most_common()],
        "",
        "Ready samples:",
    ]
    for row in ready[:30]:
        lines.extend(
            [
                f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']} agent={row.get('event_agent_key')}",
                f"  text: {short(row.get('event_current_text'), 220)}",
            ]
        )
    lines.extend(["", "Blocked samples:"])
    for row in blocked[:30]:
        lines.extend(
            [
                f"- segment={row['segment_id']} reason={row['block_reason']} {row['relative_path']}::{row['source_key']}",
                f"  text: {short(row.get('event_current_text'), 220)}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety:",
            "- Read-only companion bridge.",
            "- No confirmations, source writes, output writes, or production authority.",
            "- It only proposes lifecycle closure when event semantic coverage and short-label mirror issue are the only remaining pair.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-checkpoint-run-id", type=int)
    parser.add_argument("--coverage-run-id", type=int)
    parser.add_argument("--ledger-run-id", type=int)
    parser.add_argument("--state-run-id", type=int)
    args = parser.parse_args()

    settings = db.load_settings()
    conn = db.connect(settings)
    ensure_tables(conn)

    event_checkpoint_run_id = args.event_checkpoint_run_id or latest_finished_run_id(
        conn,
        "ml_issue_event_surface_decision_checkpoint_runs",
        "finished_at IS NOT NULL AND allowed_count > 0",
    )
    coverage_run_id = args.coverage_run_id or latest_finished_run_id(
        conn,
        "ml_issue_partial_coverage_runs",
        "finished_at IS NOT NULL",
    )
    ledger_run_id = args.ledger_run_id or latest_finished_run_id(
        conn,
        "ml_issue_ledger_runs",
        "finished_at IS NOT NULL",
    )
    state_run_id = args.state_run_id or latest_finished_run_id(
        conn,
        "segment_state_runs",
        "finished_at IS NOT NULL",
    )

    started_at = db.utc_now()
    txt_path, jsonl_path = report_paths(settings)
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_event_surface_short_label_companion_bridge_runs (
                rule_version, bridge_name, source_event_checkpoint_run_id,
                source_coverage_run_id, source_ledger_run_id, current_segment_state_run_id,
                started_at, finished_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                BRIDGE_NAME,
                event_checkpoint_run_id,
                coverage_run_id,
                ledger_run_id,
                state_run_id,
                started_at,
                started_at,
                started_at,
            ),
        )
        run_id = int(cursor.lastrowid)

    rows = fetch_candidates(
        conn,
        event_checkpoint_run_id=event_checkpoint_run_id,
        coverage_run_id=coverage_run_id,
        ledger_run_id=ledger_run_id,
        state_run_id=state_run_id,
    )
    created_at = db.utc_now()
    items: list[dict[str, Any]] = []
    for row in rows:
        status, block_reason = classify(row)
        items.append(
            {
                **row,
                "run_id": run_id,
                "source_event_checkpoint_run_id": event_checkpoint_run_id,
                "source_coverage_run_id": coverage_run_id,
                "source_ledger_run_id": ledger_run_id,
                "current_segment_state_run_id": state_run_id,
                "bridge_status": status,
                "block_reason": block_reason,
                "created_at": created_at,
            }
        )

    write_reports(txt_path=txt_path, jsonl_path=jsonl_path, run_id=run_id, rows=items)
    status_counts = Counter(row["bridge_status"] for row in items)
    block_counts = Counter(row["block_reason"] or "none" for row in items)
    agent_counts = Counter(row["event_agent_key"] or "unknown" for row in items if row["bridge_status"] == READY_STATUS)
    ready_count = int(status_counts.get(READY_STATUS, 0))
    blocked_count = len(items) - ready_count
    finished_at = db.utc_now()

    with conn:
        for item in items:
            conn.execute(
                """
                INSERT INTO ml_issue_event_surface_short_label_companion_bridge_items (
                    run_id, source_event_checkpoint_run_id, source_event_checkpoint_item_id,
                    source_coverage_run_id, source_ledger_run_id, current_segment_state_run_id,
                    segment_id, relative_path, source_key, source_line_number,
                    bridge_status, block_reason, event_agent_key, event_cluster_key, event_current_text,
                    semantic_ledger_item_id, short_label_ledger_item_id,
                    semantic_evidence_text, short_label_evidence_text,
                    total_issue_count, covered_issue_count, open_issue_count, blocked_issue_count,
                    current_final_state, current_state_group, current_review_state, current_apply_state,
                    current_needs_output_apply, current_confirmed_matches_output, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    event_checkpoint_run_id,
                    item["source_event_checkpoint_item_id"],
                    coverage_run_id,
                    ledger_run_id,
                    state_run_id,
                    item["segment_id"],
                    item.get("relative_path"),
                    item.get("source_key"),
                    item.get("source_line_number"),
                    item["bridge_status"],
                    item.get("block_reason"),
                    item.get("event_agent_key"),
                    item.get("event_cluster_key"),
                    item.get("event_current_text"),
                    item.get("semantic_ledger_item_id"),
                    item.get("short_label_ledger_item_id"),
                    item.get("semantic_evidence_text"),
                    item.get("short_label_evidence_text"),
                    item.get("total_issue_count") or 0,
                    item.get("covered_issue_count") or 0,
                    item.get("open_issue_count") or 0,
                    item.get("blocked_issue_count") or 0,
                    item.get("current_final_state"),
                    item.get("current_state_group"),
                    item.get("current_review_state"),
                    item.get("current_apply_state"),
                    item.get("current_needs_output_apply") or 0,
                    item.get("current_confirmed_matches_output") or 0,
                    created_at,
                ),
            )
        conn.execute(
            """
            UPDATE ml_issue_event_surface_short_label_companion_bridge_runs
            SET candidate_count = ?,
                ready_count = ?,
                blocked_count = ?,
                estimated_closed_gain = ?,
                status_counts_json = ?,
                block_counts_json = ?,
                agent_counts_json = ?,
                report_path = ?,
                jsonl_path = ?,
                finished_at = ?,
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
                json.dumps(dict(agent_counts), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(jsonl_path),
                finished_at,
                finished_at,
                run_id,
            ),
        )

    result = {
        "run_id": run_id,
        "event_checkpoint_run_id": event_checkpoint_run_id,
        "coverage_run_id": coverage_run_id,
        "ledger_run_id": ledger_run_id,
        "state_run_id": state_run_id,
        "candidates": len(items),
        "ready": ready_count,
        "blocked": blocked_count,
        "estimated_closed_gain": ready_count,
        "production_release_allowed": 0,
        "report_path": str(txt_path),
        "jsonl_path": str(jsonl_path),
    }
    print("[issue_event_surface_short_label_companion_bridge] Proposal generated")
    for key, value in result.items():
        print(f"[issue_event_surface_short_label_companion_bridge] {key}: {value}")
    return result


if __name__ == "__main__":
    main()
