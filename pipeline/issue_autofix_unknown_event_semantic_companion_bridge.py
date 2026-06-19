from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "issue_autofix_unknown_event_semantic_companion_bridge_v1"
BRIDGE_NAME = "autofix_unknown_event_semantic_companion_bridge_v1"
READY_STATUS = "ready_for_lifecycle_bridge"
PARTIAL_STATUS = "partial_coverage"
EXPECTED_EVENT_FAMILY = "autofix_unknown_microagent"
EXPECTED_SEMANTIC_FAMILY = "semantic_review_router"


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


def latest_event_bridge_run_id(conn) -> int:
    return latest_finished_run_id(
        conn,
        "ml_issue_autofix_unknown_event_composition_bridge_runs",
        "finished_at IS NOT NULL AND partial_count > 0",
    )


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_autofix_unknown_event_semantic_companion_bridge_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            bridge_name TEXT NOT NULL,
            source_event_bridge_run_id INTEGER NOT NULL,
            source_event_checkpoint_run_id INTEGER NOT NULL,
            source_segment_state_run_id INTEGER NOT NULL,
            current_segment_state_run_id INTEGER NOT NULL,
            source_ledger_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            ready_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            estimated_closed_gain INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            status_counts_json TEXT,
            block_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_autofix_unknown_event_semantic_companion_bridge_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            source_event_bridge_item_id INTEGER NOT NULL,
            source_event_checkpoint_item_id INTEGER NOT NULL,
            source_event_bridge_run_id INTEGER NOT NULL,
            source_event_checkpoint_run_id INTEGER NOT NULL,
            source_ledger_run_id INTEGER NOT NULL,
            current_segment_state_run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT,
            source_key TEXT,
            bridge_status TEXT NOT NULL,
            block_reason TEXT,
            event_validation_status TEXT,
            semantic_validation_status TEXT,
            segment_open_issue_count INTEGER NOT NULL DEFAULT 0,
            autofix_unknown_count INTEGER NOT NULL DEFAULT 0,
            semantic_review_count INTEGER NOT NULL DEFAULT 0,
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
            FOREIGN KEY(run_id) REFERENCES ml_issue_autofix_unknown_event_semantic_companion_bridge_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_autofix_unknown_event_semantic_companion_bridge"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl")


def fetch_event_bridge(conn, event_bridge_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_autofix_unknown_event_composition_bridge_runs
        WHERE id = ?
        """,
        (event_bridge_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Event composition bridge run not found: {event_bridge_run_id}")
    return dict(row)


def fetch_candidates(conn, *, event_bridge_run_id: int, current_state_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH issue_counts AS (
            SELECT
                segment_id,
                COUNT(*) AS open_issue_count,
                SUM(CASE WHEN issue_family = 'autofix_unknown_microagent' THEN 1 ELSE 0 END) AS autofix_unknown_count,
                SUM(CASE WHEN issue_family = 'semantic_review_router' THEN 1 ELSE 0 END) AS semantic_review_count,
                SUM(CASE WHEN lower(COALESCE(issue_severity, '')) IN ('high', 'error', 'critical') THEN 1 ELSE 0 END) AS high_issue_count,
                SUM(CASE WHEN COALESCE(token_status, '') <> 'ok' THEN 1 ELSE 0 END) AS token_not_ok_count,
                MAX(CASE WHEN issue_family = 'autofix_unknown_microagent' THEN validation_status ELSE NULL END) AS event_validation_status,
                MAX(CASE WHEN issue_family = 'semantic_review_router' THEN validation_status ELSE NULL END) AS semantic_validation_status
            FROM ml_issue_ledger_items
            WHERE run_id = ?
              AND status = 'open'
            GROUP BY segment_id
        )
        SELECT
            event_item.id AS source_event_bridge_item_id,
            event_item.checkpoint_item_id AS source_event_checkpoint_item_id,
            event_item.run_id AS source_event_bridge_run_id,
            event_item.checkpoint_run_id AS source_event_checkpoint_run_id,
            event_item.ledger_run_id AS source_ledger_run_id,
            event_item.segment_id,
            event_item.relative_path,
            event_item.source_key,
            event_item.bridge_status AS event_bridge_status,
            event_item.block_reason AS event_bridge_block_reason,
            event_item.evidence_text,
            checkpoint.normalized_decision,
            checkpoint.evidence_label,
            checkpoint.checkpoint_status,
            current_state.final_state AS current_final_state,
            current_state.state_group AS current_state_group,
            current_state.review_state AS current_review_state,
            current_state.apply_state AS current_apply_state,
            COALESCE(current_state.needs_output_apply, 0) AS current_needs_output_apply,
            COALESCE(current_state.confirmed_matches_output, 0) AS current_confirmed_matches_output,
            COALESCE(current_state.locked, 0) AS current_locked,
            COALESCE(issue_counts.open_issue_count, 0) AS segment_open_issue_count,
            COALESCE(issue_counts.autofix_unknown_count, 0) AS autofix_unknown_count,
            COALESCE(issue_counts.semantic_review_count, 0) AS semantic_review_count,
            COALESCE(issue_counts.high_issue_count, 0) AS high_issue_count,
            COALESCE(issue_counts.token_not_ok_count, 0) AS token_not_ok_count,
            CASE
                WHEN checkpoint.normalized_decision = 'composition_ready'
                 AND checkpoint.evidence_label = 'composition_ready'
                    THEN 'composition_ready'
                ELSE COALESCE(issue_counts.event_validation_status, '')
            END AS event_validation_status,
            COALESCE(issue_counts.semantic_validation_status, '') AS semantic_validation_status
        FROM ml_issue_autofix_unknown_event_composition_bridge_items event_item
        JOIN ml_issue_autofix_unknown_event_composition_checkpoint_items checkpoint
          ON checkpoint.id = event_item.checkpoint_item_id
         AND checkpoint.run_id = event_item.checkpoint_run_id
        LEFT JOIN segment_state_items current_state
          ON current_state.run_id = ?
         AND current_state.segment_id = event_item.segment_id
        LEFT JOIN issue_counts
          ON issue_counts.segment_id = event_item.segment_id
        WHERE event_item.run_id = ?
          AND event_item.bridge_status = 'partial_coverage'
        ORDER BY event_item.segment_id
        """,
        (ledger_run_id, current_state_run_id, event_bridge_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def classify(row: dict[str, Any]) -> tuple[str, str | None]:
    if row.get("event_bridge_status") != PARTIAL_STATUS:
        return "blocked", "event_bridge_not_partial"
    if row.get("checkpoint_status") != "allowed":
        return "blocked", "event_checkpoint_not_allowed"
    if row.get("normalized_decision") != "composition_ready" or row.get("evidence_label") != "composition_ready":
        return "blocked", "event_checkpoint_not_composition_ready"
    if row.get("current_state_group") != "pending" and not str(row.get("current_final_state") or "").startswith("reopen_"):
        return "blocked", "not_pending_or_reopen"
    if row.get("current_review_state") in {"human_locked", "human_confirmed"} or int(row.get("current_locked") or 0):
        return "blocked", "human_locked_or_confirmed"
    if int(row.get("current_needs_output_apply") or 0) != 0:
        return "blocked", "needs_output_apply"
    if int(row.get("current_confirmed_matches_output") or 0) != 1:
        return "blocked", "confirmed_output_mismatch"
    if int(row.get("segment_open_issue_count") or 0) != 2:
        return "blocked", "open_issue_count_not_two"
    if int(row.get("autofix_unknown_count") or 0) != 1:
        return "blocked", "missing_autofix_unknown_issue"
    if int(row.get("semantic_review_count") or 0) != 1:
        return "blocked", "missing_semantic_review_issue"
    if int(row.get("high_issue_count") or 0) != 0:
        return "blocked", "high_issue_present"
    if int(row.get("token_not_ok_count") or 0) != 0:
        return "blocked", "token_status_not_ok"
    if row.get("event_validation_status") != "composition_ready":
        return "blocked", "event_ledger_not_composition_ready"
    if row.get("semantic_validation_status") not in {"not_validated", ""}:
        return "blocked", "semantic_status_not_plain_router"
    return READY_STATUS, None


def build_items(
    *,
    rows: list[dict[str, Any]],
    run_id: int,
    current_state_run_id: int,
    ledger_run_id: int,
) -> list[dict[str, Any]]:
    created_at = db.utc_now()
    items: list[dict[str, Any]] = []
    for row in rows:
        status, block_reason = classify(row)
        items.append(
            {
                "run_id": run_id,
                "source_event_bridge_item_id": row["source_event_bridge_item_id"],
                "source_event_checkpoint_item_id": row["source_event_checkpoint_item_id"],
                "source_event_bridge_run_id": row["source_event_bridge_run_id"],
                "source_event_checkpoint_run_id": row["source_event_checkpoint_run_id"],
                "source_ledger_run_id": ledger_run_id,
                "current_segment_state_run_id": current_state_run_id,
                "segment_id": row["segment_id"],
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "bridge_status": status,
                "block_reason": block_reason,
                "event_validation_status": row.get("event_validation_status"),
                "semantic_validation_status": row.get("semantic_validation_status"),
                "segment_open_issue_count": int(row.get("segment_open_issue_count") or 0),
                "autofix_unknown_count": int(row.get("autofix_unknown_count") or 0),
                "semantic_review_count": int(row.get("semantic_review_count") or 0),
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


def insert_items(conn, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    columns = list(items[0].keys())
    placeholders = ", ".join("?" for _ in columns)
    conn.executemany(
        f"""
        INSERT INTO ml_issue_autofix_unknown_event_semantic_companion_bridge_items (
            {", ".join(columns)}
        )
        VALUES ({placeholders})
        """,
        [tuple(item[column] for column in columns) for item in items],
    )


def write_outputs(
    *,
    txt_path: Path,
    jsonl_path: Path,
    run_id: int,
    event_bridge_run: dict[str, Any],
    current_state_run_id: int,
    ledger_run_id: int,
    items: list[dict[str, Any]],
) -> None:
    status_counts = Counter(item["bridge_status"] for item in items)
    block_counts = Counter(item["block_reason"] for item in items if item["block_reason"])
    path_counts = Counter(item["relative_path"] for item in items if item["bridge_status"] == READY_STATUS)

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")

    ready = [item for item in items if item["bridge_status"] == READY_STATUS]
    blocked = [item for item in items if item["bridge_status"] != READY_STATUS]
    lines = [
        "Autofix unknown event semantic companion bridge",
        f"Rule version: {RULE_VERSION}",
        f"Bridge name: {BRIDGE_NAME}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Run id: {run_id}",
        f"Source event bridge run id: {event_bridge_run['id']}",
        f"Source event checkpoint run id: {event_bridge_run['source_checkpoint_run_id']}",
        f"Source event segment-state run id: {event_bridge_run['source_segment_state_run_id']}",
        f"Current segment-state run id: {current_state_run_id}",
        f"Ledger run id: {ledger_run_id}",
        "",
        "Summary:",
        f"- candidates: {len(items):,}",
        f"- ready_for_lifecycle_bridge: {status_counts.get(READY_STATUS, 0):,}",
        f"- blocked: {sum(count for status, count in status_counts.items() if status != READY_STATUS):,}",
        f"- estimated_closed_gain: {status_counts.get(READY_STATUS, 0):,}",
        "- production_release_allowed: 0",
        "",
        "Top ready paths:",
    ]
    lines.extend(f"- {path}: {count:,}" for path, count in path_counts.most_common(20)) if path_counts else lines.append("- none")
    lines.extend(["", "Block reasons:"])
    lines.extend(f"- {reason}: {count:,}" for reason, count in block_counts.most_common()) if block_counts else lines.append("- none")
    lines.extend(["", "Ready samples:"])
    if ready:
        for item in ready[:30]:
            lines.append(
                f"- segment={item['segment_id']} | {item['relative_path']}::{item['source_key']} | "
                f"open={item['segment_open_issue_count']} event={item['event_validation_status']} "
                f"semantic={item['semantic_validation_status']} | {short(item.get('evidence_text'), 220)}"
            )
    else:
        lines.append("- none")
    if blocked:
        lines.extend(["", "Blocked samples:"])
        for item in blocked[:30]:
            lines.append(
                f"- {item['block_reason']} | segment={item['segment_id']} | "
                f"{item['relative_path']}::{item['source_key']}"
            )
    lines.extend(
        [
            "",
            "Safety:",
            "- Proposal only; this script does not write segment-state lifecycle, output, source, or confirmations.",
            "- The bridge is ready only when the event composer marked the text as composition_ready and the only companion is the generic semantic router.",
            "- A future lifecycle consumer must still validate current confirmation/output hash, token signature, local issue summary, and non-human lock status.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, event_bridge_run_id: int | None = None, current_state_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    txt_path, jsonl_path = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_event_bridge_run_id = event_bridge_run_id or latest_event_bridge_run_id(conn)
        event_bridge_run = fetch_event_bridge(conn, selected_event_bridge_run_id)
        selected_state_run_id = current_state_run_id or latest_finished_run_id(conn, "segment_state_runs", "finished_at IS NOT NULL")
        ledger_run_id = int(event_bridge_run["source_ledger_run_id"])
        now = db.utc_now()
        run_id = int(
            conn.execute(
                """
                INSERT INTO ml_issue_autofix_unknown_event_semantic_companion_bridge_runs (
                    rule_version,
                    bridge_name,
                    source_event_bridge_run_id,
                    source_event_checkpoint_run_id,
                    source_segment_state_run_id,
                    current_segment_state_run_id,
                    source_ledger_run_id,
                    candidate_count,
                    ready_count,
                    blocked_count,
                    estimated_closed_gain,
                    production_release_allowed,
                    status_counts_json,
                    block_counts_json,
                    report_path,
                    jsonl_path,
                    started_at,
                    finished_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, '{}', '{}', ?, ?, ?, ?, ?)
                """,
                (
                    RULE_VERSION,
                    BRIDGE_NAME,
                    selected_event_bridge_run_id,
                    int(event_bridge_run["source_checkpoint_run_id"]),
                    int(event_bridge_run["source_segment_state_run_id"]),
                    selected_state_run_id,
                    ledger_run_id,
                    str(txt_path),
                    str(jsonl_path),
                    now,
                    now,
                    now,
                ),
            ).lastrowid
        )
        source_rows = fetch_candidates(
            conn,
            event_bridge_run_id=selected_event_bridge_run_id,
            current_state_run_id=selected_state_run_id,
            ledger_run_id=ledger_run_id,
        )
        items = build_items(
            rows=source_rows,
            run_id=run_id,
            current_state_run_id=selected_state_run_id,
            ledger_run_id=ledger_run_id,
        )
        insert_items(conn, items)
        status_counts = Counter(item["bridge_status"] for item in items)
        block_counts = Counter(item["block_reason"] for item in items if item["block_reason"])
        ready_count = int(status_counts.get(READY_STATUS, 0))
        blocked_count = len(items) - ready_count
        conn.execute(
            """
            UPDATE ml_issue_autofix_unknown_event_semantic_companion_bridge_runs
            SET candidate_count = ?,
                ready_count = ?,
                blocked_count = ?,
                estimated_closed_gain = ?,
                status_counts_json = ?,
                block_counts_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                len(items),
                ready_count,
                blocked_count,
                ready_count,
                json.dumps(dict(status_counts.most_common()), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(block_counts.most_common()), ensure_ascii=False, sort_keys=True),
                db.utc_now(),
                run_id,
            ),
        )
        conn.commit()

    write_outputs(
        txt_path=txt_path,
        jsonl_path=jsonl_path,
        run_id=run_id,
        event_bridge_run=event_bridge_run,
        current_state_run_id=selected_state_run_id,
        ledger_run_id=ledger_run_id,
        items=items,
    )
    print("[issue_autofix_unknown_event_semantic_companion_bridge] Proposal generated")
    print(f"[issue_autofix_unknown_event_semantic_companion_bridge] Run id: {run_id}")
    print(f"[issue_autofix_unknown_event_semantic_companion_bridge] Event bridge run id: {selected_event_bridge_run_id}")
    print(f"[issue_autofix_unknown_event_semantic_companion_bridge] Current segment-state run id: {selected_state_run_id}")
    print(f"[issue_autofix_unknown_event_semantic_companion_bridge] Candidates: {len(items):,}")
    print(f"[issue_autofix_unknown_event_semantic_companion_bridge] Ready: {ready_count:,}")
    print(f"[issue_autofix_unknown_event_semantic_companion_bridge] Blocked: {blocked_count:,}")
    print(f"[issue_autofix_unknown_event_semantic_companion_bridge] Report: {txt_path}")
    return {
        "run_id": run_id,
        "event_bridge_run_id": selected_event_bridge_run_id,
        "current_segment_state_run_id": selected_state_run_id,
        "candidates": len(items),
        "ready": ready_count,
        "blocked": blocked_count,
        "report_path": str(txt_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build event semantic companion bridge for autofix_unknown event composition partials.")
    parser.add_argument("--event-bridge-run-id", type=int)
    parser.add_argument("--current-segment-state-run-id", type=int)
    args = parser.parse_args()
    main(event_bridge_run_id=args.event_bridge_run_id, current_state_run_id=args.current_segment_state_run_id)
