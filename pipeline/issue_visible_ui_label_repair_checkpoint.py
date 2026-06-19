from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short, structural_tokens
from issue_dynamic_literal_repair_diagnostic import residual_hits


RULE_VERSION = "issue_visible_ui_label_repair_checkpoint_v1"
POLICY_NAME = "visible_ui_label_ptbr_repair_shadow_v1"
POLICY_STATUS = "shadow"
AGENT_KEY = "micro_semantic_review_router"
SUBPOLICY_NAME = "visible_ui_label_ptbr_repair"
CHECKPOINT_ACTION = "stage_visible_ui_label_repair_shadow"


def latest_decision_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT run.id
        FROM ml_issue_review_decision_runs run
        JOIN ml_issue_review_decisions decision ON decision.run_id = run.id
        WHERE run.finished_at IS NOT NULL
          AND decision.queue_bucket = 'visible_ui_label_residual'
          AND decision.valid = 1
          AND decision.validation_status = 'accepted'
        ORDER BY run.id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No visible UI label decision run found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_visible_ui_label_repair_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            decision_run_id INTEGER NOT NULL,
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
        CREATE TABLE IF NOT EXISTS ml_issue_visible_ui_label_repair_checkpoint_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            decision_run_id INTEGER NOT NULL,
            decision_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            agent_key TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            checkpoint_action TEXT NOT NULL,
            block_reason TEXT,
            token_status TEXT NOT NULL,
            current_text TEXT NOT NULL,
            corrected_text TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_visible_ui_label_repair_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], decision_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_visible_ui_label_repair_checkpoint_decision_run_{decision_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_rows(conn, *, decision_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            decision.id AS decision_id,
            decision.queue_item_id,
            decision.ledger_item_id,
            decision.segment_id,
            decision.relative_path,
            decision.source_key,
            decision.source_line_number,
            decision.agent_key,
            decision.issue_family,
            decision.issue_kind,
            decision.queue_bucket,
            decision.normalized_decision,
            decision.corrected_text,
            decision.notes,
            queue.confirmed_text,
            queue.evidence_text
        FROM ml_issue_review_decisions decision
        JOIN ml_issue_review_queue_items queue ON queue.id = decision.queue_item_id
        WHERE decision.run_id = ?
          AND decision.valid = 1
          AND decision.validation_status = 'accepted'
          AND decision.queue_bucket = 'visible_ui_label_residual'
        ORDER BY decision.id
        """,
        (decision_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def classify(row: dict[str, Any]) -> tuple[int, str, str]:
    current = row.get("confirmed_text") or row.get("evidence_text") or ""
    corrected = row.get("corrected_text") or ""
    if row.get("normalized_decision") != "needs_repair":
        return 0, "decision_not_needs_repair", "not_applicable"
    if row.get("agent_key") != AGENT_KEY:
        return 0, "agent_not_semantic_review_router", "not_applicable"
    if row.get("issue_family") != "semantic_review_router":
        return 0, "issue_family_not_semantic_review_router", "not_applicable"
    if not current.strip():
        return 0, "missing_current_text", "missing_text"
    if not corrected.strip():
        return 0, "missing_corrected_text", "missing_text"
    if current == corrected:
        return 0, "no_text_delta", "no_text_delta"
    if structural_tokens(current) != structural_tokens(corrected):
        return 0, "structural_tokens_changed", "structural_token_change_review_required"
    hits = residual_hits(corrected)
    if hits:
        return 0, "residual_after_repair:" + ",".join(hits[:6]), "residual_after_repair"
    if "Súbditos" in corrected:
        return 0, "spanish_visible_label_still_present", "residual_after_repair"
    if "Súditos" not in corrected and row.get("source_key") == "MY_REALM_WINDOW_BOOKMARK_SUBJECTS_TT":
        return 0, "expected_ptbr_subjects_label_missing", "semantic_review_required"
    return 1, "", "same_structural_tokens"


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    decision_run_id: int,
    rows: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    fields = [
        "checkpoint_allowed",
        "block_reason",
        "decision_id",
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "token_status",
        "current_text",
        "corrected_text",
        "notes",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    lines = [
        "Issue visible UI label repair checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Decision run id: {decision_run_id}",
        f"Policy: {POLICY_NAME}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Allowed: {counts['allowed']:,}",
        f"- Blocked: {counts['blocked']:,}",
        "",
        "Blocks:",
        *[f"- {key}: {value:,}" for key, value in counts.items() if key.startswith("block:")],
        "",
        "Samples:",
    ]
    for row in rows[:50]:
        lines.extend(
            [
                (
                    f"- allowed={row['checkpoint_allowed']} block={row['block_reason'] or 'none'} "
                    f"segment={row['segment_id']} {row['relative_path']}::{row['source_key']}"
                ),
                f"  current: {short(row['current_text'], 220)}",
                f"  corrected: {short(row['corrected_text'], 220)}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This checkpoint records shadow repair evidence only.",
            "- It does not write source/output and does not apply the correction.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, decision_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_decision_run_id = decision_run_id or latest_decision_run_id(conn)
        source_rows = fetch_rows(conn, decision_run_id=selected_decision_run_id)
        classified: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        for source in source_rows:
            allowed, block_reason, token_status = classify(source)
            counts["allowed" if allowed else "blocked"] += 1
            if block_reason:
                counts[f"block:{block_reason}"] += 1
            current = source.get("confirmed_text") or source.get("evidence_text") or ""
            classified.append(
                {
                    "decision_id": source["decision_id"],
                    "queue_item_id": source["queue_item_id"],
                    "ledger_item_id": source["ledger_item_id"],
                    "segment_id": source["segment_id"],
                    "relative_path": source["relative_path"],
                    "source_key": source["source_key"],
                    "source_line_number": source.get("source_line_number"),
                    "agent_key": AGENT_KEY,
                    "subpolicy_name": SUBPOLICY_NAME,
                    "checkpoint_allowed": allowed,
                    "checkpoint_action": CHECKPOINT_ACTION,
                    "block_reason": block_reason,
                    "token_status": token_status,
                    "current_text": current,
                    "corrected_text": source.get("corrected_text") or "",
                    "notes": source.get("notes") or "",
                }
            )
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_decision_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_visible_ui_label_repair_checkpoint_runs (
                rule_version,
                policy_name,
                policy_status,
                decision_run_id,
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                POLICY_NAME,
                POLICY_STATUS,
                selected_decision_run_id,
                len(classified),
                counts["allowed"],
                counts["blocked"],
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        run_id = int(cur.lastrowid)
        for row in classified:
            conn.execute(
                """
                INSERT INTO ml_issue_visible_ui_label_repair_checkpoint_items (
                    run_id,
                    decision_run_id,
                    decision_id,
                    queue_item_id,
                    ledger_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    agent_key,
                    subpolicy_name,
                    checkpoint_allowed,
                    checkpoint_action,
                    block_reason,
                    token_status,
                    current_text,
                    corrected_text,
                    notes,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_decision_run_id,
                    row["decision_id"],
                    row["queue_item_id"],
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    row["agent_key"],
                    row["subpolicy_name"],
                    row["checkpoint_allowed"],
                    row["checkpoint_action"],
                    row["block_reason"],
                    row["token_status"],
                    row["current_text"],
                    row["corrected_text"],
                    row["notes"],
                    now,
                ),
            )
        conn.commit()
    write_reports(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        run_id=run_id,
        decision_run_id=selected_decision_run_id,
        rows=classified,
        counts=counts,
    )
    print("[issue_visible_ui_label_repair_checkpoint] Checkpoint generated")
    print(f"[issue_visible_ui_label_repair_checkpoint] Run id: {run_id}")
    print(f"[issue_visible_ui_label_repair_checkpoint] Decision run id: {selected_decision_run_id}")
    print(f"[issue_visible_ui_label_repair_checkpoint] Candidates: {len(classified)}")
    print(f"[issue_visible_ui_label_repair_checkpoint] Allowed: {counts['allowed']}")
    print(f"[issue_visible_ui_label_repair_checkpoint] Blocked: {counts['blocked']}")
    print(f"[issue_visible_ui_label_repair_checkpoint] Report: {txt_path}")
    return {
        "run_id": run_id,
        "decision_run_id": selected_decision_run_id,
        "candidate_count": len(classified),
        "allowed_count": counts["allowed"],
        "blocked_count": counts["blocked"],
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Checkpoint visible UI label repairs reviewed in governed blocker queues.")
    parser.add_argument("--decision-run-id", type=int, default=None)
    args = parser.parse_args()
    main(decision_run_id=args.decision_run_id)
