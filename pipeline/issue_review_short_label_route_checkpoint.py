from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "issue_review_short_label_route_checkpoint_v2_partial_routes"
CHECKPOINT_NAME = "short_label_route_shadow_checkpoint_v1"
CHECKPOINT_ACTION = "checked_short_label_delegate_route_shadow"
POLICY_NAME = "short_label_route_shadow"
AGENT_KEY = "micro_short_label_style"


def stable_hash(value: str | None) -> str:
    return hashlib.sha1((value or "").encode("utf-8")).hexdigest()


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_route_checkpoint"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_route_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            route_run_id INTEGER NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            decision_run_id INTEGER NOT NULL,
            queue_run_id INTEGER,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_blocked_count INTEGER NOT NULL DEFAULT 0,
            subpolicy_counts_json TEXT,
            action_counts_json TEXT,
            blocker_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_route_checkpoint_items (
            id INTEGER PRIMARY KEY,
            checkpoint_run_id INTEGER NOT NULL,
            route_run_id INTEGER NOT NULL,
            route_item_id INTEGER NOT NULL,
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
            subpolicy_name TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL,
            block_reason TEXT,
            current_confirmed_text_hash TEXT,
            observed_confirmed_text_hash TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_short_label_route_checkpoint_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(route_run_id) REFERENCES ml_issue_short_label_route_shadow_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(route_item_id) REFERENCES ml_issue_short_label_route_shadow_items(id) ON DELETE CASCADE
        )
        """
    )


def latest_route_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_route_shadow_runs
        WHERE finished_at IS NOT NULL
          AND policy_name = ?
          AND policy_status = 'shadow'
          AND route_ready_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (POLICY_NAME,),
    ).fetchone()
    if row is None:
        raise RuntimeError("No completed short-label route shadow run found.")
    return int(row["id"])


def fetch_route_run(conn, *, route_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM ml_issue_short_label_route_shadow_runs WHERE id = ?",
        (route_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Short-label route shadow run not found: {route_run_id}")
    return dict(row)


def fetch_rows(conn, *, route_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.*,
            confirmation.confirmed_text AS current_confirmed_text,
            confirmation.locked AS confirmation_locked
        FROM ml_issue_short_label_route_shadow_items item
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.id = (
              SELECT c2.id
              FROM segment_confirmations c2
              WHERE c2.segment_id = item.segment_id
              ORDER BY c2.updated_at DESC, c2.id DESC
              LIMIT 1
          )
        WHERE item.run_id = ?
        ORDER BY item.relative_path, item.source_line_number, item.source_key
        """,
        (route_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def global_block_reasons(route_run: dict[str, Any], rows: list[dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    if route_run.get("policy_name") != POLICY_NAME:
        reasons.append("wrong_policy_name")
    if route_run.get("policy_status") != "shadow":
        reasons.append("route_run_not_shadow")
    if route_run.get("agent_key") != AGENT_KEY:
        reasons.append("wrong_agent_key")
    if int(route_run.get("candidate_count") or 0) != len(rows):
        reasons.append("route_item_count_mismatch")
    if not rows:
        reasons.append("no_route_items")
    return reasons


def row_block_reason(row: dict[str, Any]) -> tuple[str, str]:
    observed_hash = stable_hash(row.get("current_confirmed_text"))
    if int(row.get("route_allowed") or 0) != 1:
        return row.get("block_reason") or "route_item_not_allowed", observed_hash
    if row.get("block_reason"):
        return "route_item_has_block_reason", observed_hash
    if row.get("route_status") != "shadow_ready_delegate":
        return "route_status_not_ready_delegate", observed_hash
    if row.get("route_action") == "hold_for_manual_route_review":
        return "route_action_not_allowed", observed_hash
    if int(row.get("confirmation_locked") or 0):
        return "current_confirmation_locked", observed_hash
    if not row.get("current_confirmed_text"):
        return "missing_current_confirmation", observed_hash
    if observed_hash != (row.get("current_confirmed_text_hash") or ""):
        return "stale_confirmation_hash_changed", observed_hash
    return "", observed_hash


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    route_run: dict[str, Any],
    rows: list[dict[str, Any]],
    checkpoint_status: str,
    global_reasons: list[str],
) -> None:
    fields = [
        "checkpoint_item_id",
        "route_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "queue_bucket",
        "normalized_decision",
        "subpolicy_name",
        "checkpoint_action",
        "checkpoint_allowed",
        "block_reason",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {field: row.get(field) for field in fields}
            payload["confirmed_preview"] = short(row.get("current_confirmed_text"))
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    counts = Counter("checkpoint_allowed" if row["checkpoint_allowed"] else row["block_reason"] for row in rows)
    subpolicy_counts = Counter(row["subpolicy_name"] for row in rows if row["checkpoint_allowed"])
    lines = [
        "Issue-review short-label route checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint name: {CHECKPOINT_NAME}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Checkpoint status: {checkpoint_status}",
        "Production release allowed: 0",
        f"Route run id: {route_run['id']}",
        f"Decision run id: {route_run['decision_run_id']}",
        f"Queue run id: {route_run.get('queue_run_id')}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Allowed: {sum(1 for row in rows if row['checkpoint_allowed']):,}",
        f"- Blocked: {sum(1 for row in rows if not row['checkpoint_allowed']):,}",
        "",
        "Global gate:",
        *(f"- {reason}" for reason in global_reasons),
        *(["- ok"] if not global_reasons else []),
        "",
        "Checkpoint counts:",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        "",
        "Allowed subpolicies:",
        *[f"- {key}: {value:,}" for key, value in subpolicy_counts.most_common()],
        "",
        "Allowed samples:",
    ]
    for row in [item for item in rows if item["checkpoint_allowed"]][:25]:
        lines.append(f"- {row['subpolicy_name']} | {row['relative_path']}::{row['source_key']}")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Checkpoint only: no source/output writes, no confirmations, no model promotion.",
            "- It verifies that route-shadow evidence still matches current confirmations before coverage counts it.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, route_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    txt_path, csv_path, jsonl_path = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_route_run_id = route_run_id or latest_route_run_id(conn)
        route_run = fetch_route_run(conn, route_run_id=selected_route_run_id)
        rows = fetch_rows(conn, route_run_id=selected_route_run_id)
        global_reasons = global_block_reasons(route_run, rows)
        for row in rows:
            block_reason, observed_hash = row_block_reason(row)
            if global_reasons and not block_reason:
                block_reason = "global_gate:" + ",".join(global_reasons)
            row["checkpoint_action"] = CHECKPOINT_ACTION
            row["checkpoint_allowed"] = 0 if block_reason else 1
            row["block_reason"] = block_reason
            row["observed_confirmed_text_hash"] = observed_hash

        allowed_count = sum(1 for row in rows if row["checkpoint_allowed"])
        blocked_count = len(rows) - allowed_count
        if allowed_count == len(rows) and allowed_count > 0:
            checkpoint_status = "ready_for_shadow_lifecycle_policy"
        elif allowed_count > 0:
            checkpoint_status = "partial_ready_with_preserved_blocks"
        else:
            checkpoint_status = "blocked_by_checkpoint_guard"
        subpolicy_counts = Counter(row["subpolicy_name"] for row in rows if row["checkpoint_allowed"])
        action_counts = Counter(row["checkpoint_action"] for row in rows if row["checkpoint_allowed"])
        blocker_counts = Counter("checkpoint_allowed" if row["checkpoint_allowed"] else row["block_reason"] for row in rows)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_short_label_route_checkpoint_runs (
                rule_version,
                checkpoint_name,
                checkpoint_status,
                route_run_id,
                policy_name,
                policy_status,
                agent_key,
                decision_run_id,
                queue_run_id,
                total_candidates,
                checkpoint_allowed_count,
                checkpoint_blocked_count,
                subpolicy_counts_json,
                action_counts_json,
                blocker_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                CHECKPOINT_NAME,
                checkpoint_status,
                selected_route_run_id,
                POLICY_NAME,
                "shadow",
                AGENT_KEY,
                route_run["decision_run_id"],
                route_run.get("queue_run_id"),
                len(rows),
                allowed_count,
                blocked_count,
                json.dumps(dict(subpolicy_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(action_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(blocker_counts), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        checkpoint_run_id = int(cursor.lastrowid)
        for row in rows:
            item_cursor = conn.execute(
                """
                INSERT INTO ml_issue_short_label_route_checkpoint_items (
                    checkpoint_run_id,
                    route_run_id,
                    route_item_id,
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
                    subpolicy_name,
                    checkpoint_action,
                    checkpoint_allowed,
                    block_reason,
                    current_confirmed_text_hash,
                    observed_confirmed_text_hash,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_run_id,
                    selected_route_run_id,
                    row["id"],
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
                    row["subpolicy_name"],
                    row["checkpoint_action"],
                    int(row["checkpoint_allowed"]),
                    row["block_reason"],
                    row["current_confirmed_text_hash"],
                    row["observed_confirmed_text_hash"],
                    now,
                ),
            )
            row["checkpoint_item_id"] = int(item_cursor.lastrowid)
            row["route_item_id"] = int(row["id"])
        conn.commit()

    write_outputs(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        checkpoint_run_id=checkpoint_run_id,
        route_run=route_run,
        rows=rows,
        checkpoint_status=checkpoint_status,
        global_reasons=global_reasons,
    )
    print("[issue_review_short_label_route_checkpoint] Checkpoint generated")
    print(f"[issue_review_short_label_route_checkpoint] Checkpoint run id: {checkpoint_run_id}")
    print(f"[issue_review_short_label_route_checkpoint] Route run id: {selected_route_run_id}")
    print(f"[issue_review_short_label_route_checkpoint] Status: {checkpoint_status}")
    print(f"[issue_review_short_label_route_checkpoint] Allowed: {allowed_count:,}")
    print(f"[issue_review_short_label_route_checkpoint] Blocked: {blocked_count:,}")
    print(f"[issue_review_short_label_route_checkpoint] Report: {txt_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "route_run_id": selected_route_run_id,
        "allowed": allowed_count,
        "blocked": blocked_count,
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Checkpoint short-label route shadow evidence before coverage consumption.")
    parser.add_argument("--route-run-id", type=int, default=None)
    args = parser.parse_args()
    main(route_run_id=args.route_run_id)
