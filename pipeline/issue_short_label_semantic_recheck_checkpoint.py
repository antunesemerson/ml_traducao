from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_short_label_semantic_recheck_checkpoint_v1"
CHECKPOINT_NAME = "short_label_semantic_whole_segment_recheck_checkpoint_v1"
POLICY_NAME = "short_label_semantic_tier1_composition_shadow"
AGENT_KEY = "composition_coordinator_short_label_semantic"
QUEUE_STRATEGY = "short_label_semantic_whole_segment_recheck"
SAFE_DECISION = "composition_ready"
CHECKPOINT_ACTION = "cover_short_label_semantic_whole_segment_composition"
PRODUCTION_RELEASE_ALLOWED = 0


def report_paths(settings: dict[str, Any], queue_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_semantic_recheck_checkpoint_queue_{queue_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_semantic_recheck_checkpoint_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            queue_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            decision_run_id INTEGER,
            decision_count INTEGER NOT NULL DEFAULT 0,
            composition_ready_count INTEGER NOT NULL DEFAULT 0,
            allowed_segment_count INTEGER NOT NULL DEFAULT 0,
            blocked_decision_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            bucket_counts_json TEXT,
            decision_counts_json TEXT,
            blocker_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(queue_run_id) REFERENCES ml_issue_review_queue_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(decision_run_id) REFERENCES ml_issue_review_decision_runs(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_short_label_semantic_recheck_checkpoint_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checkpoint_run_id INTEGER NOT NULL,
            queue_run_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            decision_id INTEGER NOT NULL,
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
            checkpoint_action TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_short_label_semantic_recheck_checkpoint_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(queue_item_id) REFERENCES ml_issue_review_queue_items(id) ON DELETE CASCADE,
            FOREIGN KEY(decision_id) REFERENCES ml_issue_review_decisions(id) ON DELETE CASCADE,
            FOREIGN KEY(ledger_item_id) REFERENCES ml_issue_ledger_items(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(checkpoint_run_id, queue_item_id)
        );

        CREATE INDEX IF NOT EXISTS idx_short_semantic_recheck_checkpoint_items_allowed
        ON ml_issue_short_label_semantic_recheck_checkpoint_items(checkpoint_run_id, checkpoint_allowed, queue_bucket);

        CREATE INDEX IF NOT EXISTS idx_short_semantic_recheck_checkpoint_items_segment
        ON ml_issue_short_label_semantic_recheck_checkpoint_items(segment_id, checkpoint_allowed);
        """
    )


def latest_queue_run_id(conn, queue_run_id: int | None) -> int:
    if queue_run_id is not None:
        return queue_run_id
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_review_queue_runs
        WHERE finished_at IS NOT NULL
          AND agent_key = ?
          AND queue_strategy = ?
          AND reviewed_count > 0
        ORDER BY id DESC
        LIMIT 1
        """,
        (AGENT_KEY, QUEUE_STRATEGY),
    ).fetchone()
    if row is None:
        raise RuntimeError("No reviewed short-label + semantic recheck queue found.")
    return int(row["id"])


def fetch_queue_run(conn, queue_run_id: int) -> dict[str, Any]:
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
    payload = dict(row)
    if payload.get("agent_key") != AGENT_KEY or payload.get("queue_strategy") != QUEUE_STRATEGY:
        raise RuntimeError(f"Queue {queue_run_id} is not a {QUEUE_STRATEGY} queue.")
    return payload


def fetch_latest_decision_run_id(conn, queue_run_id: int, decision_run_id: int | None) -> int:
    if decision_run_id is not None:
        return decision_run_id
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_review_decision_runs
        WHERE queue_run_id = ?
          AND accepted_count > 0
        ORDER BY id DESC
        LIMIT 1
        """,
        (queue_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No accepted decisions found for queue {queue_run_id}.")
    return int(row["id"])


def fetch_decisions(conn, *, queue_run_id: int, decision_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            decision.*,
            item.relative_path,
            item.source_key,
            item.source_line_number,
            item.issue_family,
            item.issue_kind,
            item.queue_bucket,
            item.evidence_json
        FROM ml_issue_review_decisions decision
        JOIN ml_issue_review_queue_items item ON item.id = decision.queue_item_id
        WHERE decision.queue_run_id = ?
          AND decision.run_id = ?
          AND decision.valid = 1
          AND decision.validation_status = 'accepted'
        ORDER BY item.queue_bucket, item.relative_path, item.source_line_number, item.source_key
        """,
        (queue_run_id, decision_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def block_reason(decision: dict[str, Any]) -> str:
    if decision.get("agent_key") != AGENT_KEY:
        return "wrong_decision_agent"
    if decision.get("normalized_decision") != SAFE_DECISION:
        return "decision_not_composition_ready"
    if decision.get("evidence_label") != "composition_ready":
        return "evidence_not_composition_ready"
    if str(decision.get("corrected_text") or "").strip():
        return "composition_ready_has_corrected_text"
    return ""


def build_rows(decisions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Counter[str]]:
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for decision in decisions:
        reason = block_reason(decision)
        allowed = 0 if reason else 1
        counts[f"decision:{decision.get('normalized_decision') or 'unknown'}"] += 1
        counts[f"bucket:{decision.get('queue_bucket') or 'unknown'}"] += 1
        if allowed:
            counts["allowed"] += 1
        else:
            counts["blocked"] += 1
            counts[f"block:{reason}"] += 1
        rows.append(
            {
                "queue_run_id": int(decision["queue_run_id"]),
                "queue_item_id": int(decision["queue_item_id"]),
                "decision_id": int(decision["id"]),
                "ledger_run_id": int(decision["ledger_run_id"]),
                "ledger_item_id": int(decision["ledger_item_id"]),
                "segment_id": int(decision["segment_id"]),
                "relative_path": decision.get("relative_path") or "",
                "source_key": decision.get("source_key") or "",
                "source_line_number": decision.get("source_line_number"),
                "queue_bucket": decision.get("queue_bucket") or "",
                "issue_family": decision.get("issue_family") or "",
                "issue_kind": decision.get("issue_kind") or "",
                "normalized_decision": decision.get("normalized_decision") or "",
                "evidence_label": decision.get("evidence_label") or "",
                "checkpoint_action": CHECKPOINT_ACTION if allowed else "blocked",
                "checkpoint_allowed": allowed,
                "block_reason": reason,
                "reasons_json": json.dumps(
                    {
                        "rule_version": RULE_VERSION,
                        "decision_notes": decision.get("notes") or "",
                        "source_evidence": "queue_116_tier1_recheck",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "created_at": db.utc_now(),
            }
        )
    return rows, counts


def write_outputs(
    *,
    paths: tuple[Path, Path, Path],
    checkpoint_run_id: int,
    queue_run: dict[str, Any],
    decision_run_id: int,
    rows: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    txt_path, csv_path, jsonl_path = paths
    fieldnames = [
        "checkpoint_run_id",
        "queue_run_id",
        "queue_item_id",
        "decision_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "queue_bucket",
        "normalized_decision",
        "evidence_label",
        "checkpoint_allowed",
        "block_reason",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({"checkpoint_run_id": checkpoint_run_id, **{field: row.get(field) for field in fieldnames if field != "checkpoint_run_id"}})
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps({"checkpoint_run_id": checkpoint_run_id, **row}, ensure_ascii=False, sort_keys=True) + "\n")

    allowed = counts["allowed"]
    total = len(rows)
    precision = allowed / total if total else 0.0
    bucket_counts = Counter(row["queue_bucket"] for row in rows)
    allowed_by_bucket = Counter(row["queue_bucket"] for row in rows if row["checkpoint_allowed"])
    lines = [
        "Short-label + semantic whole-segment recheck checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Queue run id: {queue_run['id']}",
        f"Decision run id: {decision_run_id}",
        f"Ledger run id: {queue_run['ledger_run_id']}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Decisions: {total:,}",
        f"- Composition ready / allowed: {allowed:,}",
        f"- Blocked: {counts['blocked']:,}",
        f"- Assisted precision proxy: {precision:.2%}",
        "",
        "Decisions:",
        *[f"- {key}: {value:,}" for key, value in counts.most_common() if key.startswith("decision:")],
        "",
        "Allowed by bucket:",
        *[
            f"- {bucket}: {allowed_by_bucket[bucket]:,}/{count:,} ({(allowed_by_bucket[bucket] / count if count else 0):.2%})"
            for bucket, count in bucket_counts.most_common()
        ],
        "",
        "Blockers:",
        *[f"- {key}: {value:,}" for key, value in counts.most_common() if key.startswith("block:")],
        "",
        "Safety note:",
        "- This checkpoint is shadow evidence only.",
        "- It does not write source/output and does not promote lifecycle closure.",
        "- A production lifecycle bridge should require repeated precision evidence or human-reviewed confirmation.",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, queue_run_id: int | None = None, decision_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    print("[issue_short_label_semantic_recheck_checkpoint] Starting checkpoint")
    print(f"[issue_short_label_semantic_recheck_checkpoint] Rule version: {RULE_VERSION}")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_queue_run_id = latest_queue_run_id(conn, queue_run_id)
        queue_run = fetch_queue_run(conn, selected_queue_run_id)
        selected_decision_run_id = fetch_latest_decision_run_id(conn, selected_queue_run_id, decision_run_id)
        decisions = fetch_decisions(
            conn,
            queue_run_id=selected_queue_run_id,
            decision_run_id=selected_decision_run_id,
        )
        rows, counts = build_rows(decisions)
        paths = report_paths(settings, selected_queue_run_id)
        txt_path, csv_path, jsonl_path = paths
        now = db.utc_now()
        cur = conn.execute(
            """
            INSERT INTO ml_issue_short_label_semantic_recheck_checkpoint_runs (
                rule_version,
                checkpoint_name,
                checkpoint_status,
                policy_name,
                policy_status,
                agent_key,
                queue_run_id,
                ledger_run_id,
                decision_run_id,
                decision_count,
                composition_ready_count,
                allowed_segment_count,
                blocked_decision_count,
                production_release_allowed,
                bucket_counts_json,
                decision_counts_json,
                blocker_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                CHECKPOINT_NAME,
                "shadow",
                POLICY_NAME,
                "shadow",
                AGENT_KEY,
                selected_queue_run_id,
                int(queue_run["ledger_run_id"]),
                selected_decision_run_id,
                len(rows),
                counts["allowed"],
                counts["allowed"],
                counts["blocked"],
                PRODUCTION_RELEASE_ALLOWED,
                json.dumps(dict(Counter(row["queue_bucket"] for row in rows)), ensure_ascii=False, sort_keys=True),
                json.dumps({key: value for key, value in counts.items() if key.startswith("decision:")}, ensure_ascii=False, sort_keys=True),
                json.dumps({key: value for key, value in counts.items() if key.startswith("block:")}, ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                now,
                now,
                now,
            ),
        )
        checkpoint_run_id = int(cur.lastrowid)
        if rows:
            columns = [
                "checkpoint_run_id",
                "queue_run_id",
                "queue_item_id",
                "decision_id",
                "ledger_run_id",
                "ledger_item_id",
                "segment_id",
                "relative_path",
                "source_key",
                "source_line_number",
                "queue_bucket",
                "issue_family",
                "issue_kind",
                "normalized_decision",
                "evidence_label",
                "checkpoint_action",
                "checkpoint_allowed",
                "block_reason",
                "reasons_json",
                "created_at",
            ]
            conn.executemany(
                f"""
                INSERT INTO ml_issue_short_label_semantic_recheck_checkpoint_items
                ({", ".join(columns)})
                VALUES ({", ".join("?" for _ in columns)})
                """,
                [
                    tuple(
                        checkpoint_run_id if column == "checkpoint_run_id" else row.get(column)
                        for column in columns
                    )
                    for row in rows
                ],
            )
        write_outputs(
            paths=paths,
            checkpoint_run_id=checkpoint_run_id,
            queue_run=queue_run,
            decision_run_id=selected_decision_run_id,
            rows=rows,
            counts=counts,
        )
        conn.commit()

    print("[issue_short_label_semantic_recheck_checkpoint] Checkpoint generated")
    print(f"[issue_short_label_semantic_recheck_checkpoint] Checkpoint run id: {checkpoint_run_id}")
    print(f"[issue_short_label_semantic_recheck_checkpoint] Queue run id: {selected_queue_run_id}")
    print(f"[issue_short_label_semantic_recheck_checkpoint] Decision run id: {selected_decision_run_id}")
    print(f"[issue_short_label_semantic_recheck_checkpoint] Decisions: {len(rows):,}")
    print(f"[issue_short_label_semantic_recheck_checkpoint] Allowed: {counts['allowed']:,}")
    print(f"[issue_short_label_semantic_recheck_checkpoint] Blocked: {counts['blocked']:,}")
    print(f"[issue_short_label_semantic_recheck_checkpoint] Report: {txt_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "queue_run_id": selected_queue_run_id,
        "decision_run_id": selected_decision_run_id,
        "decisions": len(rows),
        "allowed": counts["allowed"],
        "blocked": counts["blocked"],
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a shadow checkpoint from short-label + semantic recheck decisions.")
    parser.add_argument("--queue-run-id", type=int, default=None)
    parser.add_argument("--decision-run-id", type=int, default=None)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id, decision_run_id=args.decision_run_id)
