from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "issue_semantic_short_label_pair_checkpoint_v1"
CHECKPOINT_NAME = "semantic_short_label_pair_review_checkpoint_v1"
CHECKPOINT_ACTION = "cover_semantic_short_label_pair_safe_review"
POLICY_NAME = "semantic_short_label_pair_safe_review_shadow"
AGENT_KEY = "coordinator_semantic_short_label_pair"
QUEUE_STRATEGY = "semantic_short_label_pair_stratified_sample"
PRIMARY_FAMILY = "semantic_review_router"
PAIR_FAMILIES = ("semantic_review_router", "short_label_style_microagent")
SAFE_DECISION = "safe_short_label"


def report_paths(settings: dict[str, Any], queue_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_semantic_short_label_pair_checkpoint_queue_{queue_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_semantic_short_label_pair_checkpoint_runs (
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
            allowed_segment_count INTEGER NOT NULL DEFAULT 0,
            allowed_issue_item_count INTEGER NOT NULL DEFAULT 0,
            blocked_decision_count INTEGER NOT NULL DEFAULT 0,
            profile_counts_json TEXT,
            decision_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_semantic_short_label_pair_checkpoint_items (
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
            checkpoint_allowed INTEGER NOT NULL,
            block_reason TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_semantic_short_label_pair_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_semantic_short_label_pair_checkpoint_items_ledger
        ON ml_issue_semantic_short_label_pair_checkpoint_items(ledger_item_id, checkpoint_allowed)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_semantic_short_label_pair_checkpoint_items_segment
        ON ml_issue_semantic_short_label_pair_checkpoint_items(segment_id, issue_family)
        """
    )


def latest_queue_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_review_queue_runs
        WHERE finished_at IS NOT NULL
          AND agent_key = ?
          AND issue_family = ?
          AND queue_strategy = ?
          AND reviewed_count > 0
        ORDER BY id DESC
        LIMIT 1
        """,
        (AGENT_KEY, PRIMARY_FAMILY, QUEUE_STRATEGY),
    ).fetchone()
    if row is None:
        raise RuntimeError("No reviewed semantic short-label pair queue found.")
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
        ORDER BY decision.segment_id, decision.queue_item_id
        """,
        (queue_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_segment_ledger_items(conn, *, ledger_run_id: int, segment_ids: set[int]) -> dict[int, list[dict[str, Any]]]:
    if not segment_ids:
        return {}
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND segment_id IN ({placeholders})
        ORDER BY segment_id, issue_family, issue_kind, id
        """,
        (ledger_run_id, *sorted(segment_ids)),
    ).fetchall()
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["segment_id"])].append(dict(row))
    return grouped


def decision_block_reason(decision: dict[str, Any], ledger_items: list[dict[str, Any]]) -> str:
    if decision.get("agent_key") != AGENT_KEY:
        return "wrong_decision_agent"
    if decision.get("issue_family") != PRIMARY_FAMILY:
        return "wrong_primary_issue_family"
    if str(decision.get("normalized_decision") or "") != SAFE_DECISION:
        return "decision_not_safe_short_label"
    if str(decision.get("corrected_text") or "").strip():
        return "safe_decision_has_corrected_text"
    families = tuple(str(item["issue_family"]) for item in ledger_items)
    if families != PAIR_FAMILIES:
        return "ledger_pair_mismatch:" + "|".join(families)
    if len(ledger_items) != 2:
        return "ledger_pair_item_count_mismatch"
    return ""


def build_rows(
    *,
    decisions: list[dict[str, Any]],
    ledger_by_segment: dict[int, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], Counter[str]]:
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for decision in decisions:
        segment_id = int(decision["segment_id"])
        ledger_items = ledger_by_segment.get(segment_id, [])
        block_reason = decision_block_reason(decision, ledger_items)
        bucket = str(decision.get("queue_bucket") or "unknown_profile")
        counts[f"decision:{decision.get('normalized_decision') or 'unknown'}"] += 1
        counts[f"profile:{bucket}"] += 1
        if block_reason:
            counts["blocked_decision"] += 1
            counts[f"block:{block_reason}"] += 1
            ledger_item_id = int(decision.get("ledger_item_id") or 0)
            rows.append(
                {
                    "queue_run_id": int(decision["queue_run_id"]),
                    "queue_item_id": int(decision["queue_item_id"]),
                    "ledger_run_id": int(decision["ledger_run_id"]),
                    "ledger_item_id": ledger_item_id,
                    "segment_id": segment_id,
                    "relative_path": decision["relative_path"],
                    "source_key": decision["source_key"],
                    "source_line_number": decision.get("source_line_number"),
                    "queue_bucket": bucket,
                    "issue_family": str(decision.get("issue_family") or PRIMARY_FAMILY),
                    "issue_kind": str(decision.get("issue_kind") or "unknown_issue"),
                    "normalized_decision": str(decision.get("normalized_decision") or "unknown_decision"),
                    "evidence_label": str(decision.get("evidence_label") or ""),
                    "agent_key": AGENT_KEY,
                    "subpolicy_name": bucket,
                    "checkpoint_action": CHECKPOINT_ACTION,
                    "checkpoint_allowed": 0,
                    "block_reason": block_reason,
                }
            )
            continue

        counts["safe_decision"] += 1
        counts["allowed_segment"] += 1
        for ledger_item in ledger_items:
            counts["allowed_issue_item"] += 1
            rows.append(
                {
                    "queue_run_id": int(decision["queue_run_id"]),
                    "queue_item_id": int(decision["queue_item_id"]),
                    "ledger_run_id": int(decision["ledger_run_id"]),
                    "ledger_item_id": int(ledger_item["id"]),
                    "segment_id": segment_id,
                    "relative_path": decision["relative_path"],
                    "source_key": decision["source_key"],
                    "source_line_number": decision.get("source_line_number"),
                    "queue_bucket": bucket,
                    "issue_family": str(ledger_item["issue_family"]),
                    "issue_kind": str(ledger_item["issue_kind"] or "unknown_issue"),
                    "normalized_decision": str(decision["normalized_decision"]),
                    "evidence_label": str(decision.get("evidence_label") or ""),
                    "agent_key": AGENT_KEY,
                    "subpolicy_name": bucket,
                    "checkpoint_action": CHECKPOINT_ACTION,
                    "checkpoint_allowed": 1,
                    "block_reason": "",
                }
            )
    return rows, counts


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
    fieldnames = [
        "checkpoint_item_id",
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "issue_family",
        "issue_kind",
        "normalized_decision",
        "checkpoint_action",
        "checkpoint_allowed",
        "block_reason",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    allowed_rows = [row for row in rows if int(row["checkpoint_allowed"]) == 1]
    blocked_rows = [row for row in rows if int(row["checkpoint_allowed"]) == 0]
    by_family = Counter(row["issue_family"] for row in allowed_rows)
    by_profile = Counter(row["queue_bucket"] for row in allowed_rows)
    lines = [
        "Semantic + short-label pair checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint name: {CHECKPOINT_NAME}",
        f"Checkpoint run id: {checkpoint_run_id}",
        "Production release allowed: 0",
        f"Queue run id: {queue_run['id']}",
        f"Ledger run id: {queue_run['ledger_run_id']}",
        f"Agent key: {AGENT_KEY}",
        "",
        "Summary:",
        f"- Reviewed decisions: {sum(value for key, value in counts.items() if key.startswith('decision:')):,}",
        f"- Safe decisions: {counts['safe_decision']:,}",
        f"- Allowed segments: {counts['allowed_segment']:,}",
        f"- Allowed issue items: {counts['allowed_issue_item']:,}",
        f"- Blocked decisions: {counts['blocked_decision']:,}",
        "",
        "Allowed issue families:",
        *[f"- {key}: {value:,}" for key, value in by_family.most_common()],
        "",
        "Allowed profiles:",
        *[f"- {key}: {value:,}" for key, value in by_profile.most_common()],
        "",
        "Decision counts:",
        *[f"- {key.removeprefix('decision:')}: {value:,}" for key, value in counts.items() if key.startswith("decision:")],
        "",
        "Blocks:",
        *[f"- {key.removeprefix('block:')}: {value:,}" for key, value in counts.items() if key.startswith("block:")],
        "",
        "Allowed samples:",
    ]
    for row in allowed_rows[:30]:
        lines.append(
            f"- {row['issue_family']} | segment={row['segment_id']} "
            f"{row['relative_path']}:{row['source_line_number']}::{row['source_key']} | {row['queue_bucket']}"
        )
    if blocked_rows:
        lines.extend(["", "Blocked samples:"])
        for row in blocked_rows[:30]:
            lines.append(
                f"- {row['block_reason']} | {row['normalized_decision']} | segment={row['segment_id']} "
                f"{row['relative_path']}::{row['source_key']}"
            )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Checkpoint only: no source/output writes, no confirmations and no lifecycle/production closure.",
            "- Each safe segment emits two coverage rows, one for semantic_review_router and one for short_label_style_microagent.",
            "- Coverage still requires the current ledger signature to match segment_id + issue_family + issue_kind.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, queue_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_queue_run_id = queue_run_id or latest_queue_run_id(conn)
        queue_run = fetch_queue_run(conn, queue_run_id=selected_queue_run_id)
        decisions = fetch_decisions(conn, queue_run_id=selected_queue_run_id)
        segment_ids = {int(row["segment_id"]) for row in decisions}
        ledger_by_segment = fetch_segment_ledger_items(
            conn,
            ledger_run_id=int(queue_run["ledger_run_id"]),
            segment_ids=segment_ids,
        )
        rows, counts = build_rows(decisions=decisions, ledger_by_segment=ledger_by_segment)
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_queue_run_id)
        allowed_segments = len({int(row["segment_id"]) for row in rows if int(row["checkpoint_allowed"]) == 1})
        allowed_issues = sum(1 for row in rows if int(row["checkpoint_allowed"]) == 1)
        blocked_decisions = counts["blocked_decision"]
        decision_total = sum(value for key, value in counts.items() if key.startswith("decision:"))
        checkpoint_status = (
            "ready_for_shadow_coverage"
            if allowed_segments > 0 and allowed_issues == allowed_segments * len(PAIR_FAMILIES)
            else "blocked_or_empty"
        )
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_semantic_short_label_pair_checkpoint_runs (
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
                allowed_segment_count,
                allowed_issue_item_count,
                blocked_decision_count,
                profile_counts_json,
                decision_counts_json,
                blocker_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                CHECKPOINT_NAME,
                checkpoint_status,
                POLICY_NAME,
                "shadow",
                AGENT_KEY,
                selected_queue_run_id,
                int(queue_run["ledger_run_id"]),
                decision_total,
                counts["safe_decision"],
                allowed_segments,
                allowed_issues,
                blocked_decisions,
                json.dumps(
                    {key.removeprefix("profile:"): value for key, value in counts.items() if key.startswith("profile:")},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                json.dumps(
                    {key.removeprefix("decision:"): value for key, value in counts.items() if key.startswith("decision:")},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                json.dumps(
                    {key.removeprefix("block:"): value for key, value in counts.items() if key.startswith("block:")},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
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
                INSERT INTO ml_issue_semantic_short_label_pair_checkpoint_items (
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
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
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
                    int(row["checkpoint_allowed"]),
                    row["block_reason"],
                    now,
                ),
            )
            row["checkpoint_item_id"] = int(item_cursor.lastrowid)
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
    print("[issue_semantic_short_label_pair_checkpoint] Checkpoint generated")
    print(f"[issue_semantic_short_label_pair_checkpoint] Checkpoint run id: {checkpoint_run_id}")
    print(f"[issue_semantic_short_label_pair_checkpoint] Queue run id: {selected_queue_run_id}")
    print(f"[issue_semantic_short_label_pair_checkpoint] Decisions: {decision_total:,}")
    print(f"[issue_semantic_short_label_pair_checkpoint] Allowed segments: {allowed_segments:,}")
    print(f"[issue_semantic_short_label_pair_checkpoint] Allowed issue items: {allowed_issues:,}")
    print(f"[issue_semantic_short_label_pair_checkpoint] Blocked decisions: {blocked_decisions:,}")
    print(f"[issue_semantic_short_label_pair_checkpoint] Report: {txt_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "queue_run_id": selected_queue_run_id,
        "decision_count": decision_total,
        "allowed_segment_count": allowed_segments,
        "allowed_issue_item_count": allowed_issues,
        "blocked_decision_count": blocked_decisions,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Checkpoint reviewed semantic + short-label pair evidence.")
    parser.add_argument("--queue-run-id", type=int, default=None)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id)
