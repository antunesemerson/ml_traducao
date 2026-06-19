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


RULE_VERSION = "issue_event_surface_pair_completion_checkpoint_v1"
POLICY_NAME = "event_surface_pair_completion_shadow_v1"
POLICY_STATUS = "shadow"
CHECKPOINT_ACTION = "stage_event_surface_pair_completion_shadow"
AGENT_KEY = "coordinator_event_surface_pair_completion"
SUBPOLICY_NAME = "event_reward_phrase_short_label_pair_completion"
TARGET_FAMILY = "short_label_style_microagent"
TARGET_KIND = "short_or_compact_label_reopened"


def table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        LIMIT 1
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def latest_run_id(conn, table_name: str) -> int | None:
    row = conn.execute(
        f"""
        SELECT id
        FROM {table_name}
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    return int(row["id"]) if row else None


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_event_surface_pair_completion_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            repair_checkpoint_run_id INTEGER,
            reviewed_item_checkpoint_run_id INTEGER,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            allowed_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
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
        CREATE TABLE IF NOT EXISTS ml_issue_event_surface_pair_completion_checkpoint_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            source_kind TEXT NOT NULL,
            source_checkpoint_run_id INTEGER NOT NULL,
            source_checkpoint_item_id INTEGER NOT NULL,
            source_ledger_item_id INTEGER NOT NULL,
            source_decision_run_id INTEGER NOT NULL,
            source_decision_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            agent_key TEXT NOT NULL,
            issue_family TEXT NOT NULL,
            issue_kind TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            checkpoint_action TEXT NOT NULL,
            block_reason TEXT,
            token_status TEXT NOT NULL,
            current_text TEXT NOT NULL,
            corrected_text TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_event_surface_pair_completion_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def fetch_source_rows(
    conn,
    *,
    repair_checkpoint_run_id: int | None,
    reviewed_item_checkpoint_run_id: int | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if repair_checkpoint_run_id is not None:
        repair_rows = conn.execute(
            """
            SELECT
                'event_surface_repair_checkpoint' AS source_kind,
                item.checkpoint_run_id AS source_checkpoint_run_id,
                item.id AS source_checkpoint_item_id,
                item.ledger_item_id AS source_ledger_item_id,
                item.decision_run_id AS source_decision_run_id,
                item.decision_id AS source_decision_id,
                item.ledger_run_id,
                item.segment_id,
                item.relative_path,
                item.source_key,
                item.source_line_number,
                item.current_text,
                item.corrected_text
            FROM ml_issue_event_surface_repair_checkpoint_items item
            WHERE item.checkpoint_run_id = ?
              AND item.checkpoint_allowed = 1
              AND item.agent_key = 'micro_effect_reward_phrase'
            ORDER BY item.segment_id, item.id
            """,
            (repair_checkpoint_run_id,),
        ).fetchall()
        rows.extend(dict(row) for row in repair_rows)

    if reviewed_item_checkpoint_run_id is not None:
        positive_rows = conn.execute(
            """
            SELECT
                'event_surface_reviewed_item_checkpoint' AS source_kind,
                item.checkpoint_run_id AS source_checkpoint_run_id,
                item.id AS source_checkpoint_item_id,
                item.ledger_item_id AS source_ledger_item_id,
                item.decision_run_id AS source_decision_run_id,
                item.decision_id AS source_decision_id,
                item.ledger_run_id,
                item.segment_id,
                item.relative_path,
                item.source_key,
                item.source_line_number,
                item.current_text,
                '' AS corrected_text
            FROM ml_issue_event_surface_reviewed_item_checkpoint_items item
            WHERE item.checkpoint_run_id = ?
              AND item.checkpoint_allowed = 1
              AND item.agent_key = 'micro_effect_reward_phrase'
            ORDER BY item.segment_id, item.id
            """,
            (reviewed_item_checkpoint_run_id,),
        ).fetchall()
        rows.extend(dict(row) for row in positive_rows)
    return rows


def fetch_target_item(conn, source: dict[str, Any]) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND segment_id = ?
          AND issue_family = ?
          AND issue_kind = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (
            int(source["ledger_run_id"]),
            int(source["segment_id"]),
            TARGET_FAMILY,
            TARGET_KIND,
        ),
    ).fetchone()
    return dict(row) if row else None


def classify_pair(source: dict[str, Any], target: dict[str, Any] | None) -> tuple[int, str, str, str]:
    if target is None:
        return 0, "missing_paired_short_label_issue", "", ""
    target_text = str(target.get("evidence_text") or "")
    source_text = str(source.get("current_text") or "")
    if target.get("token_status") != "ok":
        return 0, "paired_token_status_not_ok", target_text, str(target.get("token_status") or "")
    if target.get("status") != "open":
        return 0, "paired_issue_not_open", target_text, str(target.get("token_status") or "")
    if target_text != source_text:
        return 0, "paired_evidence_text_mismatch", target_text, str(target.get("token_status") or "")
    return 1, "", target_text, str(target.get("token_status") or "")


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_event_surface_pair_completion_checkpoint"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    repair_checkpoint_run_id: int | None,
    reviewed_item_checkpoint_run_id: int | None,
    rows: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    fields = [
        "checkpoint_allowed",
        "block_reason",
        "source_kind",
        "source_checkpoint_item_id",
        "source_ledger_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "agent_key",
        "subpolicy_name",
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

    source_counts = Counter(row["source_kind"] for row in rows if row["checkpoint_allowed"])
    lines = [
        "Issue event surface pair completion checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Policy: {POLICY_NAME}",
        f"Policy status: {POLICY_STATUS}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Repair checkpoint run id: {repair_checkpoint_run_id or 'none'}",
        f"Reviewed item checkpoint run id: {reviewed_item_checkpoint_run_id or 'none'}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Allowed: {counts['allowed']:,}",
        f"- Blocked: {counts['blocked']:,}",
        "",
        "Allowed source mix:",
        *[f"- {key}: {value:,}" for key, value in source_counts.most_common()],
        "",
        "Blocks:",
        *[f"- {key}: {value:,}" for key, value in counts.items() if key.startswith("block:")],
        "",
        "Samples:",
    ]
    for row in rows[:80]:
        lines.extend(
            [
                (
                    f"- allowed={row['checkpoint_allowed']} block={row['block_reason'] or 'none'} "
                    f"segment={row['segment_id']} target_ledger={row['ledger_item_id']} source={row['source_kind']}"
                ),
                f"  current: {short(row['current_text'], 180)}",
                f"  corrected: {short(row.get('corrected_text') or '', 180)}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Shadow checkpoint only: no source/output reads, no confirmations and no production writes.",
            "- This only covers paired short-label issues when a reviewed event reward phrase evidence exists on the same segment and text.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    repair_checkpoint_run_id: int | None = None,
    reviewed_item_checkpoint_run_id: int | None = None,
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        if repair_checkpoint_run_id is None and table_exists(conn, "ml_issue_event_surface_repair_checkpoint_runs"):
            repair_checkpoint_run_id = latest_run_id(conn, "ml_issue_event_surface_repair_checkpoint_runs")
        if reviewed_item_checkpoint_run_id is None and table_exists(
            conn,
            "ml_issue_event_surface_reviewed_item_checkpoint_runs",
        ):
            reviewed_item_checkpoint_run_id = latest_run_id(conn, "ml_issue_event_surface_reviewed_item_checkpoint_runs")

        source_rows = fetch_source_rows(
            conn,
            repair_checkpoint_run_id=repair_checkpoint_run_id,
            reviewed_item_checkpoint_run_id=reviewed_item_checkpoint_run_id,
        )
        classified: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        now = db.utc_now()
        seen_targets: set[int] = set()

        for source in source_rows:
            target = fetch_target_item(conn, source)
            allowed, block_reason, current_text, token_status = classify_pair(source, target)
            if target is not None and int(target["id"]) in seen_targets:
                allowed = 0
                block_reason = "duplicate_paired_target"
            if target is not None:
                seen_targets.add(int(target["id"]))
            counts["allowed" if allowed else "blocked"] += 1
            if block_reason:
                counts[f"block:{block_reason}"] += 1
            classified.append(
                {
                    "source_kind": source["source_kind"],
                    "source_checkpoint_run_id": source["source_checkpoint_run_id"],
                    "source_checkpoint_item_id": source["source_checkpoint_item_id"],
                    "source_ledger_item_id": source["source_ledger_item_id"],
                    "source_decision_run_id": source["source_decision_run_id"],
                    "source_decision_id": source["source_decision_id"],
                    "ledger_run_id": source["ledger_run_id"],
                    "ledger_item_id": int(target["id"]) if target else 0,
                    "segment_id": int(source["segment_id"]),
                    "relative_path": source["relative_path"],
                    "source_key": source["source_key"],
                    "source_line_number": source.get("source_line_number"),
                    "agent_key": AGENT_KEY,
                    "issue_family": TARGET_FAMILY,
                    "issue_kind": TARGET_KIND,
                    "subpolicy_name": SUBPOLICY_NAME,
                    "checkpoint_allowed": allowed,
                    "checkpoint_action": CHECKPOINT_ACTION,
                    "block_reason": block_reason,
                    "token_status": token_status,
                    "current_text": current_text or str(source.get("current_text") or ""),
                    "corrected_text": source.get("corrected_text") or "",
                    "created_at": now,
                }
            )

        txt_path, csv_path, jsonl_path = report_paths(settings)
        finished_at = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_event_surface_pair_completion_checkpoint_runs (
                rule_version,
                policy_name,
                policy_status,
                repair_checkpoint_run_id,
                reviewed_item_checkpoint_run_id,
                candidate_count,
                allowed_count,
                blocked_count,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                POLICY_NAME,
                POLICY_STATUS,
                repair_checkpoint_run_id,
                reviewed_item_checkpoint_run_id,
                len(classified),
                counts["allowed"],
                counts["blocked"],
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                finished_at,
                finished_at,
            ),
        )
        checkpoint_run_id = int(cur.lastrowid)

        for row in classified:
            conn.execute(
                """
                INSERT INTO ml_issue_event_surface_pair_completion_checkpoint_items (
                    run_id,
                    source_kind,
                    source_checkpoint_run_id,
                    source_checkpoint_item_id,
                    source_ledger_item_id,
                    source_decision_run_id,
                    source_decision_id,
                    ledger_run_id,
                    ledger_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    agent_key,
                    issue_family,
                    issue_kind,
                    subpolicy_name,
                    checkpoint_allowed,
                    checkpoint_action,
                    block_reason,
                    token_status,
                    current_text,
                    corrected_text,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_run_id,
                    row["source_kind"],
                    row["source_checkpoint_run_id"],
                    row["source_checkpoint_item_id"],
                    row["source_ledger_item_id"],
                    row["source_decision_run_id"],
                    row["source_decision_id"],
                    row["ledger_run_id"],
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    row["agent_key"],
                    row["issue_family"],
                    row["issue_kind"],
                    row["subpolicy_name"],
                    row["checkpoint_allowed"],
                    row["checkpoint_action"],
                    row["block_reason"],
                    row["token_status"],
                    row["current_text"],
                    row["corrected_text"],
                    row["created_at"],
                ),
            )
        conn.commit()
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            checkpoint_run_id=checkpoint_run_id,
            repair_checkpoint_run_id=repair_checkpoint_run_id,
            reviewed_item_checkpoint_run_id=reviewed_item_checkpoint_run_id,
            rows=classified,
            counts=counts,
        )

    print("[issue_event_surface_pair_completion_checkpoint] Checkpoint generated")
    print(f"[issue_event_surface_pair_completion_checkpoint] Rule version: {RULE_VERSION}")
    print(f"[issue_event_surface_pair_completion_checkpoint] Checkpoint run id: {checkpoint_run_id}")
    print(f"[issue_event_surface_pair_completion_checkpoint] Candidates: {len(classified):,}")
    print(f"[issue_event_surface_pair_completion_checkpoint] Allowed: {counts['allowed']:,}")
    print(f"[issue_event_surface_pair_completion_checkpoint] Blocked: {counts['blocked']:,}")
    print(f"[issue_event_surface_pair_completion_checkpoint] Report: {txt_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "candidates": len(classified),
        "allowed": counts["allowed"],
        "blocked": counts["blocked"],
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Checkpoint paired short-label completion for event surface evidence.")
    parser.add_argument("--repair-checkpoint-run-id", type=int, default=None)
    parser.add_argument("--reviewed-item-checkpoint-run-id", type=int, default=None)
    args = parser.parse_args()
    main(
        repair_checkpoint_run_id=args.repair_checkpoint_run_id,
        reviewed_item_checkpoint_run_id=args.reviewed_item_checkpoint_run_id,
    )
