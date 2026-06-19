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


RULE_VERSION = "issue_short_label_context_lane_checkpoint_v1"
CHECKPOINT_NAME = "short_label_context_lane_safe_fragment_checkpoint_v1"
CHECKPOINT_ACTION = "cover_short_label_context_lane_safe_fragment"
POLICY_NAME = "short_label_context_lane_safe_fragment_shadow"
AGENT_KEY = "micro_custom_localization_fragment"
QUEUE_STRATEGY = "short_label_context_lane_stratified_review"
QUEUE_STRATEGIES = {
    QUEUE_STRATEGY,
    "short_label_context_lane_shadow_ready_validation",
}
SAFE_DECISION = "safe_short_label"


def report_paths(settings: dict[str, Any], queue_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_context_lane_checkpoint_queue_{queue_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_context_lane_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            queue_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            decision_count INTEGER NOT NULL DEFAULT 0,
            safe_decision_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_blocked_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            decision_counts_json TEXT,
            blocker_counts_json TEXT,
            subpolicy_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_short_label_context_lane_checkpoint_items (
            id INTEGER PRIMARY KEY,
            checkpoint_run_id INTEGER NOT NULL,
            queue_run_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            queue_bucket TEXT NOT NULL,
            issue_family TEXT NOT NULL,
            issue_kind TEXT NOT NULL,
            normalized_decision TEXT NOT NULL,
            evidence_label TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            evidence_text TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_short_label_context_lane_checkpoint_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_short_label_context_lane_checkpoint_items_ledger
        ON ml_issue_short_label_context_lane_checkpoint_items(ledger_item_id, checkpoint_allowed);

        CREATE INDEX IF NOT EXISTS idx_short_label_context_lane_checkpoint_items_segment
        ON ml_issue_short_label_context_lane_checkpoint_items(segment_id, issue_family);
        """
    )


def latest_queue_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_review_queue_runs
        WHERE finished_at IS NOT NULL
          AND agent_key = ?
          AND queue_strategy IN ({placeholders})
          AND reviewed_count > 0
        ORDER BY id DESC
        LIMIT 1
        """.format(placeholders=", ".join("?" for _ in QUEUE_STRATEGIES)),
        (AGENT_KEY, *sorted(QUEUE_STRATEGIES)),
    ).fetchone()
    if row is None:
        raise RuntimeError("No reviewed short-label context lane queue found.")
    return int(row["id"])


def fetch_queue_run(conn, *, queue_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_review_queue_runs
        WHERE id = ?
        """,
        (queue_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Queue run not found: {queue_run_id}")
    return dict(row)


def fetch_decisions(conn, *, queue_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            decision.*,
            item.evidence_text,
            item.evidence_json
        FROM ml_issue_review_decisions decision
        JOIN ml_issue_review_queue_items item ON item.id = decision.queue_item_id
        WHERE decision.queue_run_id = ?
          AND decision.valid = 1
          AND decision.validation_status = 'accepted'
        ORDER BY decision.queue_item_id
        """,
        (queue_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def block_reason(decision: dict[str, Any], queue_run: dict[str, Any]) -> str:
    if queue_run.get("agent_key") != AGENT_KEY:
        return "wrong_queue_agent"
    if queue_run.get("queue_strategy") not in QUEUE_STRATEGIES:
        return "wrong_queue_strategy"
    if decision.get("agent_key") != AGENT_KEY:
        return "wrong_decision_agent"
    if str(decision.get("normalized_decision") or "") != SAFE_DECISION:
        return "decision_not_safe_short_label"
    if str(decision.get("corrected_text") or "").strip():
        return "safe_decision_has_corrected_text"
    queue_bucket = str(decision.get("queue_bucket") or "")
    if not (
        queue_bucket.startswith("short_label_context:")
        or queue_bucket.startswith("short_label_context_shadow_ready:")
    ):
        return "wrong_queue_bucket"
    return ""


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    queue_run: dict[str, Any],
    rows: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    fields = [
        "checkpoint_allowed",
        "block_reason",
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "normalized_decision",
        "checkpoint_action",
        "evidence_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps({field: row.get(field) for field in fields}, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Short-label Context Lane Checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint name: {CHECKPOINT_NAME}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Queue run id: {queue_run['id']}",
        f"Ledger run id: {queue_run['ledger_run_id']}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Decisions: {len(rows):,}",
        f"- Allowed issue items: {counts['allowed']:,}",
        f"- Blocked decisions: {counts['blocked']:,}",
        "",
        "Decision counts:",
    ]
    for key, value in counts.most_common():
        if key.startswith("decision:"):
            lines.append(f"- {key.removeprefix('decision:')}: {value:,}")
    lines.extend(["", "Blockers:"])
    for key, value in counts.most_common():
        if key.startswith("block:"):
            lines.append(f"- {key.removeprefix('block:')}: {value:,}")
    lines.extend(["", "Allowed samples:"])
    for row in [item for item in rows if item["checkpoint_allowed"]][:40]:
        lines.append(
            f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']} | {short(row['evidence_text'], 120)}"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This checkpoint marks issue-level coverage only.",
            "- It does not write source/output, create confirmations, or promote lifecycle policy.",
            "- Safe fragments still require segment-level composition before a segment can be closed.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, queue_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = db.utc_now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_queue_run_id = queue_run_id or latest_queue_run_id(conn)
        queue_run = fetch_queue_run(conn, queue_run_id=selected_queue_run_id)
        decisions = fetch_decisions(conn, queue_run_id=selected_queue_run_id)

        rows: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        for decision in decisions:
            reason = block_reason(decision, queue_run)
            allowed = 0 if reason else 1
            counts["allowed" if allowed else "blocked"] += 1
            counts[f"decision:{decision.get('normalized_decision') or 'unknown'}"] += 1
            if reason:
                counts[f"block:{reason}"] += 1
            subpolicy_name = str(decision.get("queue_bucket") or "short_label_context:unknown").split(":", 1)[-1]
            rows.append(
                {
                    "queue_run_id": int(decision["queue_run_id"]),
                    "queue_item_id": int(decision["queue_item_id"]),
                    "ledger_run_id": int(decision["ledger_run_id"]),
                    "ledger_item_id": int(decision["ledger_item_id"]),
                    "segment_id": int(decision["segment_id"]),
                    "relative_path": decision["relative_path"],
                    "source_key": decision["source_key"],
                    "source_line_number": decision.get("source_line_number"),
                    "queue_bucket": str(decision.get("queue_bucket") or ""),
                    "issue_family": str(decision.get("issue_family") or "short_label_style_microagent"),
                    "issue_kind": str(decision.get("issue_kind") or "unknown_issue"),
                    "normalized_decision": str(decision.get("normalized_decision") or "unknown"),
                    "evidence_label": str(decision.get("evidence_label") or ""),
                    "agent_key": AGENT_KEY,
                    "subpolicy_name": subpolicy_name,
                    "checkpoint_action": CHECKPOINT_ACTION,
                    "checkpoint_allowed": allowed,
                    "block_reason": reason,
                    "evidence_text": decision.get("evidence_text") or "",
                }
            )

        checkpoint_status = "ready_for_coverage" if counts["allowed"] > 0 else "blocked"
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_queue_run_id)
        now = db.utc_now()
        cur = conn.execute(
            """
            INSERT INTO ml_issue_short_label_context_lane_checkpoint_runs (
                rule_version,
                checkpoint_name,
                checkpoint_status,
                policy_name,
                policy_status,
                agent_key,
                queue_run_id,
                ledger_run_id,
                decision_count,
                safe_decision_count,
                checkpoint_allowed_count,
                checkpoint_blocked_count,
                production_release_allowed,
                decision_counts_json,
                blocker_counts_json,
                subpolicy_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, 'checkpoint_only', ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                CHECKPOINT_NAME,
                checkpoint_status,
                POLICY_NAME,
                AGENT_KEY,
                selected_queue_run_id,
                int(queue_run["ledger_run_id"]),
                len(rows),
                counts[f"decision:{SAFE_DECISION}"],
                counts["allowed"],
                counts["blocked"],
                json.dumps({key.removeprefix("decision:"): value for key, value in counts.items() if key.startswith("decision:")}, ensure_ascii=False, sort_keys=True),
                json.dumps({key.removeprefix("block:"): value for key, value in counts.items() if key.startswith("block:")}, ensure_ascii=False, sort_keys=True),
                json.dumps(Counter(row["subpolicy_name"] for row in rows if row["checkpoint_allowed"]), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        checkpoint_run_id = int(cur.lastrowid)
        conn.executemany(
            """
            INSERT INTO ml_issue_short_label_context_lane_checkpoint_items (
                checkpoint_run_id,
                queue_run_id,
                queue_item_id,
                ledger_run_id,
                ledger_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                queue_bucket,
                issue_family,
                issue_kind,
                normalized_decision,
                evidence_label,
                agent_key,
                subpolicy_name,
                checkpoint_action,
                checkpoint_allowed,
                block_reason,
                evidence_text,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    checkpoint_run_id,
                    row["queue_run_id"],
                    row["queue_item_id"],
                    row["ledger_run_id"],
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["queue_bucket"],
                    row["issue_family"],
                    row["issue_kind"],
                    row["normalized_decision"],
                    row["evidence_label"],
                    row["agent_key"],
                    row["subpolicy_name"],
                    row["checkpoint_action"],
                    row["checkpoint_allowed"],
                    row["block_reason"],
                    row["evidence_text"],
                    now,
                )
                for row in rows
            ],
        )
        conn.commit()

    write_outputs(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        checkpoint_run_id=checkpoint_run_id,
        queue_run=queue_run,
        rows=rows,
        counts=counts,
    )

    print("[issue_short_label_context_lane_checkpoint] Checkpoint generated")
    print(f"[issue_short_label_context_lane_checkpoint] Checkpoint run id: {checkpoint_run_id}")
    print(f"[issue_short_label_context_lane_checkpoint] Queue run id: {selected_queue_run_id}")
    print(f"[issue_short_label_context_lane_checkpoint] Decisions: {len(rows):,}")
    print(f"[issue_short_label_context_lane_checkpoint] Allowed: {counts['allowed']:,}")
    print(f"[issue_short_label_context_lane_checkpoint] Blocked: {counts['blocked']:,}")
    print(f"[issue_short_label_context_lane_checkpoint] Report: {txt_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "queue_run_id": selected_queue_run_id,
        "decision_count": len(rows),
        "allowed": counts["allowed"],
        "blocked": counts["blocked"],
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create issue-level checkpoint coverage for short-label context lane safe fragments.")
    parser.add_argument("--queue-run-id", type=int, default=None)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id)
