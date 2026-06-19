from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "short_label_semantic_load_tips_lifecycle_bridge_v2"
BRIDGE_NAME = "short_label_semantic_load_tips_lifecycle_bridge_v2"
READY_STATUS = "ready_for_lifecycle_bridge"
SAFE_LOAD_TIP_RE = re.compile(
    r"^#T\s+\$game_concept_[A-Za-z0-9_]+\$"
    r"(?:\s+(?:@[A-Za-z0-9_]+!|\[GetLocalPlayerPietyTextIcon\]))?"
    r"#!\s+\$game_concept_[A-Za-z0-9_]+_desc(?:_part_?1)?\$$"
)
DYNAMIC_BLOCK_RE = re.compile(
    r"\b(?:Select_CString|Custom\(|ScriptValue)\b|\[(?!GetLocalPlayerPietyTextIcon\])[A-Za-z0-9_.]+\]"
)
GAME_CONCEPT_TOKEN_RE = re.compile(r"\$game_concept_[A-Za-z0-9_]+(?:_desc(?:_part_?1)?)?\$")
DESC_TOKEN_RE = re.compile(r"^\$game_concept_[A-Za-z0-9_]+_desc(?:_part_?1)?\$$")


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", as_text(value).replace("\\n", "\n")).strip()


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_short_label_semantic_load_tips_lifecycle_bridge_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            bridge_name TEXT NOT NULL,
            source_ledger_run_id INTEGER NOT NULL,
            source_segment_state_run_id INTEGER NOT NULL,
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
        CREATE TABLE IF NOT EXISTS ml_short_label_semantic_load_tips_lifecycle_bridge_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            source_ledger_run_id INTEGER NOT NULL,
            source_segment_state_run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT,
            source_key TEXT,
            bridge_status TEXT NOT NULL,
            block_reason TEXT,
            current_text TEXT,
            normalized_text TEXT,
            title_token TEXT,
            desc_token TEXT,
            open_issue_count INTEGER NOT NULL DEFAULT 0,
            short_label_issue_count INTEGER NOT NULL DEFAULT 0,
            semantic_issue_count INTEGER NOT NULL DEFAULT 0,
            high_issue_count INTEGER NOT NULL DEFAULT 0,
            current_final_state TEXT,
            current_state_group TEXT,
            current_review_state TEXT,
            current_needs_output_apply INTEGER NOT NULL DEFAULT 0,
            current_confirmed_matches_output INTEGER NOT NULL DEFAULT 0,
            current_locked INTEGER NOT NULL DEFAULT 0,
            confirmation_locked INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_short_label_semantic_load_tips_lifecycle_bridge_runs(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_short_label_semantic_load_tips_bridge_items_run_status
        ON ml_short_label_semantic_load_tips_lifecycle_bridge_items(run_id, bridge_status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_short_label_semantic_load_tips_bridge_items_segment
        ON ml_short_label_semantic_load_tips_lifecycle_bridge_items(segment_id, run_id)
        """
    )


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_short_label_semantic_load_tips_lifecycle_bridge"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl")


def classify_text(current_text: str, confirmation_text: str | None) -> tuple[str, str | None, str, str]:
    normalized = normalize_text(current_text)
    if not normalized:
        return "blocked", "load_tip_pattern_mismatch", "", ""
    if DYNAMIC_BLOCK_RE.search(normalized):
        return "blocked", "load_tip_pattern_mismatch", "", ""
    if not normalized.startswith("#T ") or "#!" not in normalized:
        return "blocked", "load_tip_pattern_mismatch", "", ""
    if not SAFE_LOAD_TIP_RE.match(normalized):
        return "blocked", "load_tip_pattern_mismatch", "", ""
    tokens = GAME_CONCEPT_TOKEN_RE.findall(normalized)
    if len(tokens) != 2 or not DESC_TOKEN_RE.match(tokens[1]):
        return "blocked", "load_tip_pattern_mismatch", "", ""
    if confirmation_text is not None and normalize_text(confirmation_text) != normalized:
        return "blocked", "confirmed_output_mismatch", "", ""
    return READY_STATUS, None, tokens[0], tokens[1]


def fetch_candidates(conn, ledger_run_id: int, state_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH issue_segments AS (
            SELECT
                segment_id,
                COUNT(*) AS open_issue_count,
                SUM(CASE WHEN issue_family = 'short_label_style_microagent'
                          AND issue_kind = 'short_or_compact_label_reopened'
                         THEN 1 ELSE 0 END) AS short_label_issue_count,
                SUM(CASE WHEN issue_family = 'semantic_review_router'
                          AND issue_kind = 'needs_human_or_semantic_conflict'
                         THEN 1 ELSE 0 END) AS semantic_issue_count,
                SUM(CASE WHEN lower(COALESCE(issue_severity, '')) IN ('high', 'error', 'critical')
                         THEN 1 ELSE 0 END) AS high_issue_count
            FROM ml_issue_ledger_items
            WHERE run_id = ?
              AND status = 'open'
            GROUP BY segment_id
        )
        SELECT
            state.segment_id,
            state.relative_path,
            state.source_key,
            state.final_state AS current_final_state,
            state.state_group AS current_state_group,
            state.review_state AS current_review_state,
            COALESCE(state.needs_output_apply, 0) AS current_needs_output_apply,
            COALESCE(state.confirmed_matches_output, 0) AS current_confirmed_matches_output,
            COALESCE(state.locked, 0) AS current_locked,
            COALESCE(confirmation.locked, 0) AS confirmation_locked,
            confirmation.confirmed_text,
            output.portuguese_text AS current_text,
            COALESCE(issue_segments.open_issue_count, 0) AS open_issue_count,
            COALESCE(issue_segments.short_label_issue_count, 0) AS short_label_issue_count,
            COALESCE(issue_segments.semantic_issue_count, 0) AS semantic_issue_count,
            COALESCE(issue_segments.high_issue_count, 0) AS high_issue_count
        FROM issue_segments
        JOIN segment_state_items state
          ON state.segment_id = issue_segments.segment_id
         AND state.run_id = ?
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.segment_id = state.segment_id
        LEFT JOIN output_segments output
          ON output.segment_id = state.segment_id
        WHERE state.relative_path LIKE 'load_tips%'
          AND issue_segments.open_issue_count = 2
          AND issue_segments.short_label_issue_count = 1
          AND issue_segments.semantic_issue_count = 1
          AND issue_segments.high_issue_count = 0
          AND state.state_group = 'pending'
          AND state.final_state = 'reopen_auto_confirmed_autofix'
          AND state.review_state = 'auto_confirmed'
          AND COALESCE(state.needs_output_apply, 0) = 0
          AND COALESCE(state.confirmed_matches_output, 0) = 1
        ORDER BY state.segment_id
        """,
        (ledger_run_id, state_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def build_items(rows: list[dict[str, Any]], *, run_id: int, ledger_run_id: int, state_run_id: int) -> list[dict[str, Any]]:
    created_at = db.utc_now()
    items: list[dict[str, Any]] = []
    for row in rows:
        status, block_reason, title_token, desc_token = classify_text(
            as_text(row.get("current_text")),
            as_text(row.get("confirmed_text")) if row.get("confirmed_text") is not None else None,
        )
        items.append(
            {
                "run_id": run_id,
                "source_ledger_run_id": ledger_run_id,
                "source_segment_state_run_id": state_run_id,
                "segment_id": int(row["segment_id"]),
                "relative_path": as_text(row.get("relative_path")),
                "source_key": as_text(row.get("source_key")),
                "bridge_status": status,
                "block_reason": block_reason,
                "current_text": as_text(row.get("current_text")),
                "normalized_text": normalize_text(row.get("current_text")),
                "title_token": title_token,
                "desc_token": desc_token,
                "open_issue_count": int(row.get("open_issue_count") or 0),
                "short_label_issue_count": int(row.get("short_label_issue_count") or 0),
                "semantic_issue_count": int(row.get("semantic_issue_count") or 0),
                "high_issue_count": int(row.get("high_issue_count") or 0),
                "current_final_state": as_text(row.get("current_final_state")),
                "current_state_group": as_text(row.get("current_state_group")),
                "current_review_state": as_text(row.get("current_review_state")),
                "current_needs_output_apply": int(row.get("current_needs_output_apply") or 0),
                "current_confirmed_matches_output": int(row.get("current_confirmed_matches_output") or 0),
                "current_locked": int(row.get("current_locked") or 0),
                "confirmation_locked": int(row.get("confirmation_locked") or 0),
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
        INSERT INTO ml_short_label_semantic_load_tips_lifecycle_bridge_items (
            {", ".join(columns)}
        )
        VALUES ({placeholders})
        """,
        [tuple(item[column] for column in columns) for item in items],
    )


def write_outputs(txt_path: Path, jsonl_path: Path, *, run_id: int, items: list[dict[str, Any]]) -> None:
    status_counts = Counter(item["bridge_status"] for item in items)
    block_counts = Counter(item["block_reason"] for item in items if item["block_reason"])
    ready = [item for item in items if item["bridge_status"] == READY_STATUS]
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    lines = [
        "Short-label semantic load tips lifecycle bridge",
        f"Rule version: {RULE_VERSION}",
        f"Bridge name: {BRIDGE_NAME}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Bridge run id: {run_id}",
        "",
        "Summary:",
        f"- candidates: {len(items):,}",
        f"- ready_for_lifecycle_bridge: {status_counts.get(READY_STATUS, 0):,}",
        f"- blocked: {len(items) - status_counts.get(READY_STATUS, 0):,}",
        f"- estimated_closed_gain: {status_counts.get(READY_STATUS, 0):,}",
        "- production_release_allowed: 0",
        "",
        "Block reasons:",
    ]
    lines.extend(f"- {reason}: {count:,}" for reason, count in block_counts.most_common()) if block_counts else lines.append("- none")
    lines.extend(["", "Ready samples:"])
    for item in ready[:10]:
        lines.append(f"- {item['segment_id']} | {item['source_key']} | {item['normalized_text']}")
    lines.extend(
        [
            "",
            "Safety:",
            "- Bridge only; no output/source writes.",
            "- No confirmations created or promoted.",
            "- Segment-state must re-check live guards before closure.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, ledger_run_id: int, state_run_id: int) -> dict[str, Any]:
    settings = db.load_settings()
    txt_path, jsonl_path = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        now = db.utc_now()
        run_id = int(
            conn.execute(
                """
                INSERT INTO ml_short_label_semantic_load_tips_lifecycle_bridge_runs (
                    rule_version,
                    bridge_name,
                    source_ledger_run_id,
                    source_segment_state_run_id,
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
                VALUES (?, ?, ?, ?, 0, 0, 0, 0, 0, '{}', '{}', ?, ?, ?, ?, ?)
                """,
                (
                    RULE_VERSION,
                    BRIDGE_NAME,
                    ledger_run_id,
                    state_run_id,
                    str(txt_path),
                    str(jsonl_path),
                    now,
                    now,
                    now,
                ),
            ).lastrowid
        )
        rows = fetch_candidates(conn, ledger_run_id, state_run_id)
        items = build_items(rows, run_id=run_id, ledger_run_id=ledger_run_id, state_run_id=state_run_id)
        insert_items(conn, items)
        status_counts = Counter(item["bridge_status"] for item in items)
        block_counts = Counter(item["block_reason"] for item in items if item["block_reason"])
        ready_count = status_counts.get(READY_STATUS, 0)
        blocked_count = len(items) - ready_count
        conn.execute(
            """
            UPDATE ml_short_label_semantic_load_tips_lifecycle_bridge_runs
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
                json.dumps(dict(status_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(block_counts), ensure_ascii=False, sort_keys=True),
                db.utc_now(),
                run_id,
            ),
        )
        conn.commit()
    write_outputs(txt_path, jsonl_path, run_id=run_id, items=items)
    print("[short_label_semantic_load_tips_lifecycle_bridge] Bridge generated")
    print(f"[short_label_semantic_load_tips_lifecycle_bridge] Bridge run id: {run_id}")
    print(f"[short_label_semantic_load_tips_lifecycle_bridge] Candidates: {len(items):,}")
    print(f"[short_label_semantic_load_tips_lifecycle_bridge] Ready: {ready_count:,}")
    print(f"[short_label_semantic_load_tips_lifecycle_bridge] Blocked: {blocked_count:,}")
    print(f"[short_label_semantic_load_tips_lifecycle_bridge] Report: {txt_path}")
    return {"run_id": run_id, "candidates": len(items), "ready": ready_count, "blocked": blocked_count}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build read-only lifecycle bridge for structural load tips.")
    parser.add_argument("--ledger-run-id", type=int, default=75)
    parser.add_argument("--segment-state-run-id", type=int, default=364)
    args = parser.parse_args()
    main(ledger_run_id=args.ledger_run_id, state_run_id=args.segment_state_run_id)
