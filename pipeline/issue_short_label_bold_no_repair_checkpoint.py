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


RULE_VERSION = "issue_short_label_bold_no_repair_checkpoint_v1"
POLICY_NAME = "short_label_bold_no_ptbr_repair_shadow_v1"
CHECKPOINT_NAME = "short_label_bold_no_ptbr_repair_checkpoint_v1"
CHECKPOINT_ACTION = "cover_short_label_bold_no_ptbr_repair"
AGENT_KEY = "micro_short_label_bold_no_repair"
SUBPOLICY_NAME = "short_label_bold_no_ptbr_repair"


def latest_shadow_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_bold_no_repair_shadow_runs
        WHERE finished_at IS NOT NULL
          AND policy_name = ?
          AND ready_shadow_count > 0
        ORDER BY id DESC
        LIMIT 1
        """,
        (POLICY_NAME,),
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished bold-no repair shadow run found.")
    return int(row["id"])


def report_paths(settings: dict[str, Any], shadow_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_bold_no_repair_checkpoint_shadow_run_{shadow_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_bold_no_repair_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            promotion_status TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            shadow_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_blocked_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            reason_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_short_label_bold_no_repair_checkpoint_items (
            id INTEGER PRIMARY KEY,
            checkpoint_run_id INTEGER NOT NULL,
            shadow_run_id INTEGER NOT NULL,
            shadow_item_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            issue_family TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            token_status TEXT NOT NULL,
            current_text TEXT NOT NULL,
            corrected_text TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_short_label_bold_no_repair_checkpoint_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_short_label_bold_no_checkpoint_items_run
        ON ml_issue_short_label_bold_no_repair_checkpoint_items(checkpoint_run_id, checkpoint_allowed, segment_id);

        CREATE INDEX IF NOT EXISTS idx_short_label_bold_no_checkpoint_items_ledger
        ON ml_issue_short_label_bold_no_repair_checkpoint_items(ledger_item_id, checkpoint_allowed);
        """
    )


def fetch_shadow_run(conn, *, shadow_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_bold_no_repair_shadow_runs
        WHERE id = ?
        """,
        (shadow_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Shadow run not found: {shadow_run_id}")
    return dict(row)


def fetch_rows(conn, *, shadow_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_bold_no_repair_shadow_items
        WHERE run_id = ?
        ORDER BY relative_path, source_line_number, source_key, id
        """,
        (shadow_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def row_block_reason(row: dict[str, Any]) -> str:
    if row.get("shadow_status") != "ready_shadow":
        return row.get("block_reason") or "shadow_not_ready"
    if int(row.get("checkpoint_candidate") or 0) != 1:
        return "not_checkpoint_candidate"
    if row.get("token_status") != "same_structural_tokens":
        return "token_status_not_same_structural_tokens"
    if not (row.get("corrected_text") or "").strip():
        return "missing_corrected_text"
    if row.get("current_text") == row.get("corrected_text"):
        return "no_text_delta"
    return ""


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    shadow_run: dict[str, Any],
    rows: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    fields = [
        "checkpoint_allowed",
        "block_reason",
        "shadow_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "checkpoint_action",
        "token_status",
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
            handle.write(json.dumps({field: row.get(field) for field in fields}, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Short Label Bold No Repair Checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Checkpoint name: {CHECKPOINT_NAME}",
        f"Shadow run id: {shadow_run['id']}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Allowed: {counts['allowed']:,}",
        f"- Blocked: {counts['blocked']:,}",
        "",
        "Blockers:",
    ]
    for key, value in counts.most_common():
        if key.startswith("block:"):
            lines.append(f"- {key.removeprefix('block:')}: {value:,}")
    lines.extend(["", "Samples:"])
    for row in rows[:80]:
        lines.extend(
            [
                (
                    f"- allowed={row['checkpoint_allowed']} block={row['block_reason'] or 'none'} "
                    f"segment={row['segment_id']} {row['relative_path']}::{row['source_key']}"
                ),
                f"  current: {short(row['current_text'], 220)}",
                f"  corrected: {short(row['corrected_text'], 220) if row['corrected_text'] else '<none>'}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This checkpoint marks issue-level coverage only.",
            "- It does not write source/output, create confirmations, or promote lifecycle policy.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, shadow_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = db.utc_now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_shadow_run_id = shadow_run_id or latest_shadow_run_id(conn)
        shadow_run = fetch_shadow_run(conn, shadow_run_id=selected_shadow_run_id)
        source_rows = fetch_rows(conn, shadow_run_id=selected_shadow_run_id)

        items: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        global_reasons: list[str] = []
        if shadow_run.get("policy_name") != POLICY_NAME:
            global_reasons.append("wrong_policy_name")
        if shadow_run.get("policy_status") != "shadow":
            global_reasons.append("shadow_run_not_shadow")
        if not source_rows:
            global_reasons.append("no_shadow_items")

        for row in source_rows:
            reason = ";".join(global_reasons) if global_reasons else row_block_reason(row)
            allowed = 0 if reason else 1
            counts["allowed" if allowed else "blocked"] += 1
            if reason:
                for part in reason.split(";"):
                    counts[f"block:{part}"] += 1
            items.append(
                {
                    "shadow_item_id": row["id"],
                    "ledger_item_id": row["ledger_item_id"],
                    "segment_id": row["segment_id"],
                    "relative_path": row["relative_path"],
                    "source_key": row["source_key"],
                    "source_line_number": row["source_line_number"],
                    "issue_family": "short_label_style_microagent",
                    "agent_key": AGENT_KEY,
                    "subpolicy_name": SUBPOLICY_NAME,
                    "checkpoint_action": CHECKPOINT_ACTION,
                    "checkpoint_allowed": allowed,
                    "block_reason": reason,
                    "token_status": row["token_status"],
                    "current_text": row["current_text"],
                    "corrected_text": row["corrected_text"],
                }
            )

        checkpoint_status = "ready_for_lifecycle_policy" if counts["allowed"] > 0 and not global_reasons else "blocked"
        promotion_status = "shadow_checkpoint_candidate" if checkpoint_status == "ready_for_lifecycle_policy" else "blocked"
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_shadow_run_id)
        now = db.utc_now()
        cur = conn.execute(
            """
            INSERT INTO ml_issue_short_label_bold_no_repair_checkpoint_runs (
                rule_version,
                checkpoint_name,
                checkpoint_status,
                promotion_status,
                policy_name,
                agent_key,
                shadow_run_id,
                candidate_count,
                checkpoint_allowed_count,
                checkpoint_blocked_count,
                production_release_allowed,
                reason_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                CHECKPOINT_NAME,
                checkpoint_status,
                promotion_status,
                POLICY_NAME,
                AGENT_KEY,
                selected_shadow_run_id,
                len(items),
                counts["allowed"],
                counts["blocked"],
                json.dumps(dict(counts), ensure_ascii=False, sort_keys=True),
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
            INSERT INTO ml_issue_short_label_bold_no_repair_checkpoint_items (
                checkpoint_run_id,
                shadow_run_id,
                shadow_item_id,
                ledger_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                issue_family,
                agent_key,
                subpolicy_name,
                checkpoint_action,
                checkpoint_allowed,
                block_reason,
                token_status,
                current_text,
                corrected_text,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    checkpoint_run_id,
                    selected_shadow_run_id,
                    row["shadow_item_id"],
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["issue_family"],
                    row["agent_key"],
                    row["subpolicy_name"],
                    row["checkpoint_action"],
                    row["checkpoint_allowed"],
                    row["block_reason"],
                    row["token_status"],
                    row["current_text"],
                    row["corrected_text"],
                    now,
                )
                for row in items
            ],
        )
        conn.commit()

    write_reports(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        checkpoint_run_id=checkpoint_run_id,
        shadow_run=shadow_run,
        rows=items,
        counts=counts,
    )

    print("[issue_short_label_bold_no_repair_checkpoint] Checkpoint generated")
    print(f"[issue_short_label_bold_no_repair_checkpoint] Checkpoint run id: {checkpoint_run_id}")
    print(f"[issue_short_label_bold_no_repair_checkpoint] Shadow run id: {selected_shadow_run_id}")
    print(f"[issue_short_label_bold_no_repair_checkpoint] Candidates: {len(items):,}")
    print(f"[issue_short_label_bold_no_repair_checkpoint] Allowed: {counts['allowed']:,}")
    print(f"[issue_short_label_bold_no_repair_checkpoint] Blocked: {counts['blocked']:,}")
    print(f"[issue_short_label_bold_no_repair_checkpoint] Report: {txt_path}")
    print(f"[issue_short_label_bold_no_repair_checkpoint] CSV: {csv_path}")
    print(f"[issue_short_label_bold_no_repair_checkpoint] JSONL: {jsonl_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "shadow_run_id": selected_shadow_run_id,
        "candidate_count": len(items),
        "allowed_count": counts["allowed"],
        "blocked_count": counts["blocked"],
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Promote bold-no shadow repairs into issue-level checkpoint coverage.")
    parser.add_argument("--shadow-run-id", type=int, default=None)
    args = parser.parse_args()
    main(shadow_run_id=args.shadow_run_id)
