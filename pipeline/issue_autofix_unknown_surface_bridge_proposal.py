from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from pending_architecture_diagnostic import latest_run


RULE_VERSION = "issue_autofix_unknown_surface_bridge_proposal_v1"
BRIDGE_NAME = "autofix_unknown_surface_bridge_v1"


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_autofix_unknown_surface_bridge_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            bridge_name TEXT NOT NULL,
            source_checkpoint_run_id INTEGER NOT NULL,
            source_segment_state_run_id INTEGER NOT NULL,
            source_ledger_run_id INTEGER NOT NULL,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            ready_count INTEGER NOT NULL DEFAULT 0,
            partial_count INTEGER NOT NULL DEFAULT 0,
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
        CREATE TABLE IF NOT EXISTS ml_issue_autofix_unknown_surface_bridge_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            checkpoint_item_id INTEGER NOT NULL,
            checkpoint_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT,
            source_key TEXT,
            cluster TEXT,
            bridge_status TEXT NOT NULL,
            block_reason TEXT,
            segment_open_issue_count INTEGER NOT NULL DEFAULT 0,
            covered_issue_count INTEGER NOT NULL DEFAULT 0,
            final_state TEXT,
            evidence_text TEXT,
            created_at TEXT NOT NULL
        )
        """
    )


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_autofix_unknown_surface_bridge_proposal"
    return base.with_suffix(".txt"), base.with_suffix(".jsonl")


def latest_checkpoint_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_autofix_unknown_surface_checkpoint_runs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise RuntimeError("No autofix unknown surface checkpoint run found.")
    return int(row["id"])


def fetch_candidates(conn, checkpoint_run_id: int, state_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH issue_counts AS (
            SELECT
                segment_id,
                COUNT(*) AS open_issue_count,
                SUM(CASE WHEN issue_family = 'autofix_unknown_microagent' THEN 1 ELSE 0 END) AS autofix_unknown_count
            FROM ml_issue_ledger_items
            WHERE run_id = ?
              AND status = 'open'
            GROUP BY segment_id
        )
        SELECT
            checkpoint.id AS checkpoint_item_id,
            checkpoint.run_id AS checkpoint_run_id,
            checkpoint.ledger_run_id,
            checkpoint.segment_id,
            checkpoint.relative_path,
            checkpoint.source_key,
            checkpoint.cluster,
            checkpoint.evidence_text,
            state.final_state,
            state.state_group,
            COALESCE(issue_counts.open_issue_count, 0) AS segment_open_issue_count,
            COALESCE(issue_counts.autofix_unknown_count, 0) AS autofix_unknown_count
        FROM ml_issue_autofix_unknown_surface_checkpoint_items checkpoint
        LEFT JOIN segment_state_items state
            ON state.run_id = ?
           AND state.segment_id = checkpoint.segment_id
        LEFT JOIN issue_counts ON issue_counts.segment_id = checkpoint.segment_id
        WHERE checkpoint.run_id = ?
          AND checkpoint.checkpoint_status = 'allowed'
        ORDER BY checkpoint.segment_id
        """,
        (ledger_run_id, state_run_id, checkpoint_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def classify(row: dict[str, Any]) -> tuple[str, str | None]:
    if row.get("state_group") != "pending":
        return "blocked", "not_pending_or_already_closed"
    if int(row.get("segment_open_issue_count") or 0) == 1 and int(row.get("autofix_unknown_count") or 0) == 1:
        return "ready_for_lifecycle_bridge", None
    if int(row.get("autofix_unknown_count") or 0) >= 1:
        return "partial_coverage", "other_open_issues_remain"
    return "blocked", "checkpoint_issue_not_in_current_ledger"


def build_items(rows: list[dict[str, Any]], run_id: int, state_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    created_at = db.utc_now()
    items: list[dict[str, Any]] = []
    for row in rows:
        status, block_reason = classify(row)
        covered = 1 if status in {"ready_for_lifecycle_bridge", "partial_coverage"} else 0
        items.append(
            {
                "run_id": run_id,
                "checkpoint_item_id": row["checkpoint_item_id"],
                "checkpoint_run_id": row["checkpoint_run_id"],
                "ledger_run_id": ledger_run_id,
                "segment_state_run_id": state_run_id,
                "segment_id": row["segment_id"],
                "relative_path": row.get("relative_path"),
                "source_key": row.get("source_key"),
                "cluster": row.get("cluster"),
                "bridge_status": status,
                "block_reason": block_reason,
                "segment_open_issue_count": int(row.get("segment_open_issue_count") or 0),
                "covered_issue_count": covered,
                "final_state": row.get("final_state"),
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
        INSERT INTO ml_issue_autofix_unknown_surface_bridge_items (
            {", ".join(columns)}
        )
        VALUES ({placeholders})
        """,
        [tuple(item[column] for column in columns) for item in items],
    )


def write_outputs(txt_path: Path, jsonl_path: Path, run_id: int, checkpoint_run_id: int, state_run_id: int, ledger_run_id: int, items: list[dict[str, Any]]) -> None:
    status_counts = Counter(item["bridge_status"] for item in items)
    block_counts = Counter(item["block_reason"] for item in items if item["block_reason"])
    cluster_counts = Counter(f"{item['cluster']}|{item['bridge_status']}" for item in items)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Autofix unknown surface bridge proposal",
        f"Rule version: {RULE_VERSION}",
        f"Bridge name: {BRIDGE_NAME}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Bridge run id: {run_id}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Segment-state run id: {state_run_id}",
        f"Ledger run id: {ledger_run_id}",
        "",
        "Summary:",
        f"- candidates: {len(items):,}",
        f"- ready_for_lifecycle_bridge: {status_counts.get('ready_for_lifecycle_bridge', 0):,}",
        f"- partial_coverage: {status_counts.get('partial_coverage', 0):,}",
        f"- blocked: {status_counts.get('blocked', 0):,}",
        f"- estimated_closed_gain: {status_counts.get('ready_for_lifecycle_bridge', 0):,}",
        "- production_release_allowed: 0",
        "",
        "Status by cluster:",
        *[f"- {label}: {count:,}" for label, count in cluster_counts.most_common()],
        "",
        "Block reasons:",
    ]
    if block_counts:
        lines.extend(f"- {reason}: {count:,}" for reason, count in block_counts.most_common())
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Ready sample:",
        ]
    )
    ready = [item for item in items if item["bridge_status"] == "ready_for_lifecycle_bridge"]
    if ready:
        for item in ready[:20]:
            lines.append(
                f"- segment={item['segment_id']} | {item['cluster']} | {item['relative_path']}::{item['source_key']} | {item['evidence_text']}"
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety:",
            "- Proposal only.",
            "- Does not modify segment_state.",
            "- Does not write output.",
            "- Future lifecycle consumer must explicitly validate current state and issue coverage.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build bridge proposal for autofix_unknown surface checkpoint.")
    parser.add_argument("--checkpoint-run-id", type=int)
    args = parser.parse_args()

    settings = db.load_settings()
    txt_path, jsonl_path = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        state_run = latest_run(conn, "segment_state_runs")
        ledger_run = latest_run(conn, "ml_issue_ledger_runs")
        checkpoint_run_id = args.checkpoint_run_id or latest_checkpoint_run_id(conn)
        now = db.utc_now()
        run_id = conn.execute(
            """
            INSERT INTO ml_issue_autofix_unknown_surface_bridge_runs (
                rule_version,
                bridge_name,
                source_checkpoint_run_id,
                source_segment_state_run_id,
                source_ledger_run_id,
                total_candidates,
                ready_count,
                partial_count,
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
            VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0, 0, 0, '{}', '{}', ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                BRIDGE_NAME,
                checkpoint_run_id,
                int(state_run["id"]),
                int(ledger_run["id"]),
                str(txt_path),
                str(jsonl_path),
                now,
                now,
                now,
            ),
        ).lastrowid
        run_id = int(run_id)
        rows = fetch_candidates(conn, checkpoint_run_id, int(state_run["id"]), int(ledger_run["id"]))
        items = build_items(rows, run_id, int(state_run["id"]), int(ledger_run["id"]))
        insert_items(conn, items)
        status_counts = Counter(item["bridge_status"] for item in items)
        block_counts = Counter(item["block_reason"] for item in items if item["block_reason"])
        conn.execute(
            """
            UPDATE ml_issue_autofix_unknown_surface_bridge_runs
            SET total_candidates = ?,
                ready_count = ?,
                partial_count = ?,
                blocked_count = ?,
                estimated_closed_gain = ?,
                status_counts_json = ?,
                block_counts_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                len(items),
                status_counts.get("ready_for_lifecycle_bridge", 0),
                status_counts.get("partial_coverage", 0),
                status_counts.get("blocked", 0),
                status_counts.get("ready_for_lifecycle_bridge", 0),
                json.dumps(dict(status_counts.most_common()), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(block_counts.most_common()), ensure_ascii=False, sort_keys=True),
                db.utc_now(),
                run_id,
            ),
        )
        conn.commit()

    write_outputs(txt_path, jsonl_path, run_id, checkpoint_run_id, int(state_run["id"]), int(ledger_run["id"]), items)
    print("[issue_autofix_unknown_surface_bridge_proposal] Proposal generated")
    print(f"[issue_autofix_unknown_surface_bridge_proposal] Run id: {run_id}")
    print(f"[issue_autofix_unknown_surface_bridge_proposal] Checkpoint run id: {checkpoint_run_id}")
    print(f"[issue_autofix_unknown_surface_bridge_proposal] Candidates: {len(items):,}")
    print(f"[issue_autofix_unknown_surface_bridge_proposal] Ready: {sum(1 for item in items if item['bridge_status'] == 'ready_for_lifecycle_bridge'):,}")
    print(f"[issue_autofix_unknown_surface_bridge_proposal] Partial: {sum(1 for item in items if item['bridge_status'] == 'partial_coverage'):,}")
    print(f"[issue_autofix_unknown_surface_bridge_proposal] Blocked: {sum(1 for item in items if item['bridge_status'] == 'blocked'):,}")
    print(f"[issue_autofix_unknown_surface_bridge_proposal] Report: {txt_path}")


if __name__ == "__main__":
    main()
