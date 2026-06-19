from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_short_label_dynamic_delegate_checkpoint_v1"
CHECKPOINT_NAME = "short_label_dynamic_delegate_checkpoint_v1"
CHECKPOINT_ACTION = "cover_short_label_dynamic_delegate_to_dynamic_ck3"
POLICY_NAME = "short_label_dynamic_delegate_shadow"
AGENT_KEY = "micro_short_label_style"
SUBPOLICY_NAME = "short_label_dynamic_expression_delegate"
TARGET_SUBLANE = "short_label_dynamic_expression_delegate"


def latest_sublane_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_repair_sublane_diagnostic_runs
        WHERE finished_at IS NOT NULL
          AND issue_family = 'short_label_style_microagent'
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished short-label sublane diagnostic run found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any], sublane_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_dynamic_delegate_checkpoint_sublane_{sublane_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_dynamic_delegate_checkpoint_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            sublane_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            allowed_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            dynamic_kind_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_dynamic_delegate_checkpoint_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checkpoint_run_id INTEGER NOT NULL,
            sublane_run_id INTEGER NOT NULL,
            sublane_item_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            dynamic_ledger_item_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            queue_bucket TEXT NOT NULL,
            issue_family TEXT NOT NULL,
            issue_kind TEXT NOT NULL,
            dynamic_issue_kind TEXT,
            subpolicy_name TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            evidence_text TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_short_label_dynamic_delegate_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_short_label_dynamic_delegate_checkpoint_items_ledger
        ON ml_issue_short_label_dynamic_delegate_checkpoint_items(ledger_item_id, checkpoint_allowed)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_short_label_dynamic_delegate_checkpoint_items_segment
        ON ml_issue_short_label_dynamic_delegate_checkpoint_items(segment_id, checkpoint_allowed)
        """
    )


def fetch_sublane_run(conn, sublane_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM ml_issue_short_label_repair_sublane_diagnostic_runs WHERE id = ?",
        (sublane_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Short-label sublane run not found: {sublane_run_id}")
    return dict(row)


def fetch_sublane_items(conn, sublane_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT item.*
        FROM ml_issue_short_label_repair_sublane_diagnostic_items item
        JOIN ml_issue_ledger_items ledger ON ledger.id = item.ledger_item_id
        WHERE item.run_id = ?
          AND item.sublane = ?
          AND ledger.issue_family = 'short_label_style_microagent'
          AND ledger.status = 'open'
        ORDER BY item.relative_path, item.source_line_number, item.source_key, item.id
        """,
        (sublane_run_id, TARGET_SUBLANE),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_dynamic_by_segment(conn, ledger_run_id: int) -> dict[int, list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT id, segment_id, issue_kind
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND issue_family = 'dynamic_ck3_expression_microagent'
          AND status = 'open'
        ORDER BY segment_id, id
        """,
        (ledger_run_id,),
    ).fetchall()
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["segment_id"])].append(dict(row))
    return grouped


def classify_item(row: dict[str, Any], dynamic_rows: list[dict[str, Any]]) -> tuple[int, str, dict[str, Any] | None]:
    if not dynamic_rows:
        return 0, "missing_matching_dynamic_ck3_issue", None
    return 1, "", dynamic_rows[0]


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    sublane_run: dict[str, Any],
    rows: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    fields = [
        "checkpoint_item_id",
        "checkpoint_run_id",
        "sublane_run_id",
        "sublane_item_id",
        "ledger_run_id",
        "ledger_item_id",
        "dynamic_ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "issue_family",
        "issue_kind",
        "dynamic_issue_kind",
        "subpolicy_name",
        "checkpoint_action",
        "checkpoint_allowed",
        "block_reason",
        "evidence_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps({field: row.get(field) for field in fields}, ensure_ascii=False, sort_keys=True) + "\n")

    allowed = [row for row in rows if int(row["checkpoint_allowed"]) == 1]
    blocked = [row for row in rows if int(row["checkpoint_allowed"]) == 0]
    lines = [
        "Short Label Dynamic Delegate Checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint name: {CHECKPOINT_NAME}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Sublane run id: {sublane_run['id']}",
        f"Ledger run id: {sublane_run['ledger_run_id']}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Allowed: {len(allowed):,}",
        f"- Blocked: {len(blocked):,}",
        "",
        "Dynamic issue kinds:",
    ]
    for key, value in sorted(((key, value) for key, value in counts.items() if key.startswith("dynamic:")), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {key.removeprefix('dynamic:')}: {value:,}")
    lines.extend(["", "Blocks:"])
    for key, value in sorted(((key, value) for key, value in counts.items() if key.startswith("block:")), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {key.removeprefix('block:')}: {value:,}")
    lines.extend(["", "Allowed samples:"])
    for row in allowed[:25]:
        lines.append(
            f"- segment={row['segment_id']} | dynamic={row['dynamic_issue_kind']} | "
            f"{row['relative_path']}::{row['source_key']} | {row['evidence_text'][:180] if row.get('evidence_text') else ''}"
        )
    lines.extend(["", "Blocked samples:"])
    for row in blocked[:25]:
        lines.append(
            f"- {row['block_reason']} | segment={row['segment_id']} | "
            f"{row['relative_path']}::{row['source_key']} | {row['evidence_text'][:180] if row.get('evidence_text') else ''}"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Checkpoint only: no source/output writes, no confirmations and no lifecycle/production closure.",
            "- This covers only the short-label issue as delegated; the dynamic issue itself must still be covered by the dynamic CK3 neuron.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Checkpoint short-label issues safely delegated to the dynamic CK3 expression neuron.")
    parser.add_argument("--sublane-run-id", type=int, default=None)
    args = parser.parse_args()

    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        sublane_run_id = args.sublane_run_id or latest_sublane_run_id(conn)
        sublane_run = fetch_sublane_run(conn, sublane_run_id)
        ledger_run_id = int(sublane_run["ledger_run_id"])
        items = fetch_sublane_items(conn, sublane_run_id)
        dynamic_by_segment = fetch_dynamic_by_segment(conn, ledger_run_id)

        counts: Counter[str] = Counter()
        rows: list[dict[str, Any]] = []
        for item in items:
            dynamic_rows = dynamic_by_segment.get(int(item["segment_id"]), [])
            allowed, block_reason, dynamic = classify_item(item, dynamic_rows)
            dynamic_issue_kind = str(dynamic.get("issue_kind") or "") if dynamic else ""
            if dynamic_issue_kind:
                counts[f"dynamic:{dynamic_issue_kind}"] += 1
            if block_reason:
                counts[f"block:{block_reason}"] += 1
            rows.append(
                {
                    "checkpoint_run_id": None,
                    "sublane_run_id": sublane_run_id,
                    "sublane_item_id": int(item["id"]),
                    "ledger_run_id": ledger_run_id,
                    "ledger_item_id": int(item["ledger_item_id"]),
                    "dynamic_ledger_item_id": int(dynamic["id"]) if dynamic else None,
                    "segment_id": int(item["segment_id"]),
                    "relative_path": item["relative_path"],
                    "source_key": item["source_key"],
                    "source_line_number": item.get("source_line_number"),
                    "queue_bucket": TARGET_SUBLANE,
                    "issue_family": "short_label_style_microagent",
                    "issue_kind": item["issue_kind"],
                    "dynamic_issue_kind": dynamic_issue_kind,
                    "subpolicy_name": SUBPOLICY_NAME,
                    "checkpoint_action": CHECKPOINT_ACTION if allowed else "hold_short_label_dynamic_delegate_review",
                    "checkpoint_allowed": allowed,
                    "block_reason": block_reason,
                    "evidence_text": item.get("evidence_text") or "",
                }
            )

        allowed_count = sum(1 for row in rows if int(row["checkpoint_allowed"]) == 1)
        blocked_count = len(rows) - allowed_count
        checkpoint_status = "checkpoint_ready" if allowed_count else "checkpoint_blocked"
        cur = conn.execute(
            """
            INSERT INTO ml_issue_short_label_dynamic_delegate_checkpoint_runs (
                rule_version,
                checkpoint_name,
                checkpoint_status,
                policy_name,
                policy_status,
                agent_key,
                sublane_run_id,
                ledger_run_id,
                candidate_count,
                allowed_count,
                blocked_count,
                dynamic_kind_counts_json,
                blocker_counts_json,
                started_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, 'shadow', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                CHECKPOINT_NAME,
                checkpoint_status,
                POLICY_NAME,
                AGENT_KEY,
                sublane_run_id,
                ledger_run_id,
                len(rows),
                allowed_count,
                blocked_count,
                json.dumps({k: v for k, v in counts.items() if k.startswith("dynamic:")}, ensure_ascii=False, sort_keys=True),
                json.dumps({k: v for k, v in counts.items() if k.startswith("block:")}, ensure_ascii=False, sort_keys=True),
                started_at,
                started_at,
            ),
        )
        checkpoint_run_id = int(cur.lastrowid)
        now = db.utc_now()
        for row in rows:
            row["checkpoint_run_id"] = checkpoint_run_id
            item_cur = conn.execute(
                """
                INSERT INTO ml_issue_short_label_dynamic_delegate_checkpoint_items (
                    checkpoint_run_id,
                    sublane_run_id,
                    sublane_item_id,
                    ledger_run_id,
                    ledger_item_id,
                    dynamic_ledger_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    queue_bucket,
                    issue_family,
                    issue_kind,
                    dynamic_issue_kind,
                    subpolicy_name,
                    checkpoint_action,
                    checkpoint_allowed,
                    block_reason,
                    evidence_text,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_run_id,
                    row["sublane_run_id"],
                    row["sublane_item_id"],
                    row["ledger_run_id"],
                    row["ledger_item_id"],
                    row["dynamic_ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["queue_bucket"],
                    row["issue_family"],
                    row["issue_kind"],
                    row["dynamic_issue_kind"],
                    row["subpolicy_name"],
                    row["checkpoint_action"],
                    row["checkpoint_allowed"],
                    row["block_reason"],
                    row["evidence_text"],
                    now,
                ),
            )
            row["checkpoint_item_id"] = int(item_cur.lastrowid)

        txt_path, csv_path, jsonl_path = report_paths(settings, sublane_run_id)
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            checkpoint_run_id=checkpoint_run_id,
            sublane_run=sublane_run,
            rows=rows,
            counts=counts,
        )
        finished_at = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            UPDATE ml_issue_short_label_dynamic_delegate_checkpoint_runs
            SET report_path = ?,
                csv_path = ?,
                jsonl_path = ?,
                finished_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (str(txt_path), str(csv_path), str(jsonl_path), finished_at, finished_at, checkpoint_run_id),
        )
        conn.commit()

    print("[issue_short_label_dynamic_delegate_checkpoint] Checkpoint generated")
    print(f"[issue_short_label_dynamic_delegate_checkpoint] Checkpoint run id: {checkpoint_run_id}")
    print(f"[issue_short_label_dynamic_delegate_checkpoint] Sublane run id: {sublane_run_id}")
    print(f"[issue_short_label_dynamic_delegate_checkpoint] Candidates: {len(rows):,}")
    print(f"[issue_short_label_dynamic_delegate_checkpoint] Allowed: {allowed_count:,}")
    print(f"[issue_short_label_dynamic_delegate_checkpoint] Blocked: {blocked_count:,}")
    print(f"[issue_short_label_dynamic_delegate_checkpoint] Report: {txt_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "sublane_run_id": sublane_run_id,
        "candidate_count": len(rows),
        "allowed_count": allowed_count,
        "blocked_count": blocked_count,
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    main()
