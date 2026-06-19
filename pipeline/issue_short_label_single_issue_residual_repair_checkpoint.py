from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short, structural_tokens
from issue_dynamic_literal_repair_diagnostic import residual_hits


RULE_VERSION = "issue_short_label_single_issue_residual_repair_checkpoint_v1"
POLICY_NAME = "short_label_single_issue_residual_repair_shadow_v1"
POLICY_STATUS = "shadow"
AGENT_KEY = "micro_short_label_residual_repair"
SUBPOLICY_NAME = "short_label_single_issue_residual_repair"
CHECKPOINT_ACTION = "stage_short_label_single_issue_residual_repair_shadow"


BLOCKED_FRAGMENTS = (
    "#bold no#!",
    "#bold No#!",
    "#bold not#!",
    "probabilidad",
    "Reducción",
    "Reducci",
    "Interludio iranio",
    "SONIDO",
    "The beneficiary",
)


def normalize_markup_tokens_for_compare(text: str) -> str:
    return re.sub(r"#bol\b", "#bold", text, flags=re.IGNORECASE)


def latest_decision_run_id(conn, queue_run_id: int | None) -> int:
    params: list[Any] = []
    queue_filter = ""
    if queue_run_id is not None:
        queue_filter = "AND run.queue_run_id = ?"
        params.append(queue_run_id)
    row = conn.execute(
        f"""
        SELECT run.id
        FROM ml_issue_review_decision_runs run
        JOIN ml_issue_review_decisions decision ON decision.run_id = run.id
        WHERE run.finished_at IS NOT NULL
          {queue_filter}
          AND decision.queue_bucket IN (
              'markup_or_no_literal',
              'spanish_lexical_residual',
              'english_visible'
          )
          AND decision.normalized_decision = 'needs_repair'
        ORDER BY run.id DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    if row is None:
        raise RuntimeError("No matching short-label residual repair decision run found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_single_issue_residual_repair_checkpoint_runs (
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
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_single_issue_residual_repair_checkpoint_items (
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
            queue_bucket TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            checkpoint_action TEXT NOT NULL,
            block_reason TEXT,
            token_status TEXT NOT NULL,
            current_text TEXT NOT NULL,
            corrected_text TEXT NOT NULL,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_short_label_single_issue_residual_repair_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], decision_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_single_issue_residual_repair_checkpoint_decision_run_{decision_run_id}"
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
          AND decision.normalized_decision = 'needs_repair'
          AND decision.queue_bucket IN (
              'markup_or_no_literal',
              'spanish_lexical_residual',
              'english_visible'
          )
        ORDER BY decision.id
        """,
        (decision_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def classify(row: dict[str, Any]) -> tuple[int, str, str]:
    current = row.get("confirmed_text") or row.get("evidence_text") or ""
    corrected = row.get("corrected_text") or ""
    if row.get("agent_key") != AGENT_KEY:
        return 0, "agent_not_short_label_residual_repair", "not_applicable"
    if row.get("issue_family") != "short_label_style_microagent":
        return 0, "issue_family_not_short_label_style", "not_applicable"
    if not current.strip():
        return 0, "missing_current_text", "missing_text"
    if not corrected.strip():
        return 0, "missing_corrected_text", "missing_text"
    if current == corrected:
        return 0, "no_text_delta", "no_text_delta"
    current_for_tokens = normalize_markup_tokens_for_compare(current)
    if structural_tokens(current_for_tokens) != structural_tokens(corrected):
        return 0, "structural_tokens_changed", "structural_token_change_review_required"

    lower_corrected = corrected.lower()
    for fragment in BLOCKED_FRAGMENTS:
        if fragment.lower() in lower_corrected:
            return 0, f"blocked_fragment_after_repair:{fragment}", "residual_after_repair"
    if re.search(r"#bol(?!d)\b", corrected, flags=re.IGNORECASE):
        return 0, "blocked_fragment_after_repair:#bol", "residual_after_repair"

    hits = residual_hits(corrected)
    if hits:
        return 0, "residual_after_repair:" + ",".join(hits[:6]), "residual_after_repair"
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
        "queue_bucket",
        "token_status",
        "current_text",
        "corrected_text",
        "notes",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Short label single-issue residual repair checkpoint",
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
        "Allowed by bucket:",
        *[f"- {key.removeprefix('allowed_bucket:')}: {value:,}" for key, value in counts.items() if key.startswith("allowed_bucket:")],
        "",
        "Blocks:",
        *[f"- {key.removeprefix('block:')}: {value:,}" for key, value in counts.items() if key.startswith("block:")],
        "",
        "Samples:",
    ]
    for row in rows[:50]:
        lines.extend(
            [
                (
                    f"- allowed={row['checkpoint_allowed']} block={row['block_reason'] or 'none'} "
                    f"segment={row['segment_id']} bucket={row['queue_bucket']} "
                    f"{row['relative_path']}::{row['source_key']}"
                ),
                f"  current: {short(row['current_text'], 220)}",
                f"  repair:  {short(row['corrected_text'], 220)}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This checkpoint records shadow repair evidence only.",
            "- It does not write source/output, create confirmations, or run production.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, decision_run_id: int | None = None, queue_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_decision_run_id = decision_run_id or latest_decision_run_id(conn, queue_run_id)
        source_rows = fetch_rows(conn, decision_run_id=selected_decision_run_id)
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_decision_run_id)
        cur = conn.execute(
            """
            INSERT INTO ml_issue_short_label_single_issue_residual_repair_checkpoint_runs (
                rule_version,
                policy_name,
                policy_status,
                decision_run_id,
                started_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (RULE_VERSION, POLICY_NAME, POLICY_STATUS, selected_decision_run_id, started_at, started_at),
        )
        run_id = int(cur.lastrowid)
        now = db.utc_now()
        classified: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        for source in source_rows:
            allowed, block_reason, token_status = classify(source)
            counts["allowed" if allowed else "blocked"] += 1
            if allowed:
                counts[f"allowed_bucket:{source.get('queue_bucket') or 'unknown'}"] += 1
            if block_reason:
                counts[f"block:{block_reason}"] += 1
            current = source.get("confirmed_text") or source.get("evidence_text") or ""
            item = {
                "decision_id": int(source["decision_id"]),
                "queue_item_id": int(source["queue_item_id"]),
                "ledger_item_id": int(source["ledger_item_id"]),
                "segment_id": int(source["segment_id"]),
                "relative_path": source["relative_path"],
                "source_key": source["source_key"],
                "source_line_number": source.get("source_line_number"),
                "agent_key": AGENT_KEY,
                "subpolicy_name": SUBPOLICY_NAME,
                "queue_bucket": source["queue_bucket"],
                "checkpoint_allowed": allowed,
                "checkpoint_action": CHECKPOINT_ACTION,
                "block_reason": block_reason,
                "token_status": token_status,
                "current_text": current,
                "corrected_text": source.get("corrected_text") or "",
                "notes": source.get("notes") or "",
            }
            classified.append(item)
            conn.execute(
                """
                INSERT INTO ml_issue_short_label_single_issue_residual_repair_checkpoint_items (
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
                    queue_bucket,
                    checkpoint_allowed,
                    checkpoint_action,
                    block_reason,
                    token_status,
                    current_text,
                    corrected_text,
                    notes,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_decision_run_id,
                    item["decision_id"],
                    item["queue_item_id"],
                    item["ledger_item_id"],
                    item["segment_id"],
                    item["relative_path"],
                    item["source_key"],
                    item["source_line_number"],
                    item["agent_key"],
                    item["subpolicy_name"],
                    item["queue_bucket"],
                    item["checkpoint_allowed"],
                    item["checkpoint_action"],
                    item["block_reason"],
                    item["token_status"],
                    item["current_text"],
                    item["corrected_text"],
                    item["notes"],
                    now,
                ),
            )

        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            decision_run_id=selected_decision_run_id,
            rows=classified,
            counts=counts,
        )
        finished_at = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            UPDATE ml_issue_short_label_single_issue_residual_repair_checkpoint_runs
            SET
                candidate_count = ?,
                allowed_count = ?,
                blocked_count = ?,
                report_path = ?,
                csv_path = ?,
                jsonl_path = ?,
                finished_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                len(classified),
                counts["allowed"],
                counts["blocked"],
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                finished_at,
                finished_at,
                run_id,
            ),
        )
        conn.commit()

    print("[short_label_residual_checkpoint] Checkpoint generated")
    print(f"[short_label_residual_checkpoint] Rule version: {RULE_VERSION}")
    print(f"[short_label_residual_checkpoint] Run id: {run_id}")
    print(f"[short_label_residual_checkpoint] Decision run id: {selected_decision_run_id}")
    print(f"[short_label_residual_checkpoint] Candidates: {len(classified)}")
    print(f"[short_label_residual_checkpoint] Allowed: {counts['allowed']}")
    print(f"[short_label_residual_checkpoint] Blocked: {counts['blocked']}")
    print(f"[short_label_residual_checkpoint] Report: {txt_path}")
    return {
        "run_id": run_id,
        "decision_run_id": selected_decision_run_id,
        "candidates": len(classified),
        "allowed": counts["allowed"],
        "blocked": counts["blocked"],
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a protected checkpoint for short-label residual repairs.")
    parser.add_argument("--decision-run-id", type=int, default=None)
    parser.add_argument("--queue-run-id", type=int, default=None)
    args = parser.parse_args()
    main(decision_run_id=args.decision_run_id, queue_run_id=args.queue_run_id)
