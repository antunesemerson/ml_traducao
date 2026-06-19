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
from issue_dynamic_literal_repair_diagnostic import classify_item


RULE_VERSION = "issue_spanish_residual_repair_diagnostic_v1"
AGENT_KEY = "micro_spanish_residual"


def latest_decision_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT d.run_id
        FROM ml_issue_review_decisions d
        JOIN ml_issue_review_queue_items q ON q.id = d.queue_item_id
        WHERE q.agent_key = ?
          AND d.normalized_decision = 'needs_repair'
        GROUP BY d.run_id
        ORDER BY MAX(d.created_at) DESC, d.run_id DESC
        LIMIT 1
        """,
        (AGENT_KEY,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No spanish residual repair decision run found for {AGENT_KEY!r}.")
    return int(row["run_id"])


def fetch_rows(conn, decision_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            d.id AS decision_id,
            d.queue_item_id,
            q.segment_id,
            q.relative_path,
            q.source_key,
            q.queue_bucket,
            q.english_text,
            q.spanish_text,
            q.confirmed_text
        FROM ml_issue_review_decisions d
        JOIN ml_issue_review_queue_items q ON q.id = d.queue_item_id
        WHERE d.run_id = ?
          AND d.normalized_decision = 'needs_repair'
          AND q.agent_key = ?
        ORDER BY q.id
        """,
        (decision_run_id, AGENT_KEY),
    ).fetchall()
    return [dict(row) for row in rows]


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_spanish_residual_repair_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            decision_run_id INTEGER NOT NULL,
            agent_key TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            ready_shadow_count INTEGER NOT NULL DEFAULT 0,
            needs_context_count INTEGER NOT NULL DEFAULT 0,
            token_policy_review_count INTEGER NOT NULL DEFAULT 0,
            same_structural_token_count INTEGER NOT NULL DEFAULT 0,
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
        CREATE TABLE IF NOT EXISTS ml_issue_spanish_residual_repair_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            decision_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            queue_bucket TEXT NOT NULL,
            repair_status TEXT NOT NULL,
            token_status TEXT NOT NULL,
            current_text TEXT NOT NULL,
            corrected_text TEXT,
            english_text TEXT,
            spanish_text TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_spanish_residual_repair_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], decision_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_spanish_residual_repair_decision_run_{decision_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    decision_run_id: int,
    rows: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    fields = [
        "repair_status",
        "token_status",
        "decision_id",
        "queue_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "queue_bucket",
        "current_text",
        "corrected_text",
        "reasons",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            payload = dict(row)
            payload["reasons"] = "; ".join(row["reasons"])
            writer.writerow(payload)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Issue spanish residual repair diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Decision run id: {decision_run_id}",
        f"Agent: {AGENT_KEY}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Ready shadow: {counts['ready_shadow']:,}",
        f"- Needs context: {counts['needs_context']:,}",
        f"- Token policy review: {counts['token_policy_review']:,}",
        f"- Same structural tokens: {counts['same_structural_tokens']:,}",
        "",
        "By queue bucket:",
        *[f"- {key}: {value:,}" for key, value in counts.items() if key.startswith("bucket:")],
        "",
        "Samples:",
    ]
    for row in rows[:80]:
        lines.extend(
            [
                (
                    f"- {row['repair_status']} | {row['token_status']} | "
                    f"segment={row['segment_id']} {row['relative_path']}::{row['source_key']}"
                ),
                f"  current: {short(row['current_text'], 220)}",
                f"  corrected: {short(row['corrected_text'], 220) if row['corrected_text'] else '<none>'}",
                f"  reasons: {', '.join(row['reasons'])}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This diagnostic does not write source/output and does not promote confirmations.",
            "- ready_shadow means the proposed text preserves structural token shape and can be checkpointed later.",
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
        source_rows = fetch_rows(conn, selected_decision_run_id)
        classified = [classify_item(row) for row in source_rows]
        counts: Counter[str] = Counter(row["repair_status"] for row in classified)
        counts.update(f"bucket:{row['queue_bucket']}" for row in classified)
        counts["same_structural_tokens"] = sum(
            1 for row in classified if row["token_status"] == "same_structural_tokens"
        )
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_decision_run_id)
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            decision_run_id=selected_decision_run_id,
            rows=classified,
            counts=counts,
        )
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_spanish_residual_repair_runs (
                rule_version,
                decision_run_id,
                agent_key,
                candidate_count,
                ready_shadow_count,
                needs_context_count,
                token_policy_review_count,
                same_structural_token_count,
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
                selected_decision_run_id,
                AGENT_KEY,
                len(classified),
                counts["ready_shadow"],
                counts["needs_context"],
                counts["token_policy_review"],
                counts["same_structural_tokens"],
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
                INSERT INTO ml_issue_spanish_residual_repair_items (
                    run_id,
                    decision_id,
                    queue_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    queue_bucket,
                    repair_status,
                    token_status,
                    current_text,
                    corrected_text,
                    english_text,
                    spanish_text,
                    reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    row["decision_id"],
                    row["queue_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["queue_bucket"],
                    row["repair_status"],
                    row["token_status"],
                    row["current_text"],
                    row["corrected_text"],
                    row["english_text"],
                    row["spanish_text"],
                    json.dumps(row["reasons"], ensure_ascii=False),
                    now,
                ),
            )
        conn.commit()

    print("[issue_spanish_residual_repair_diagnostic] Diagnostic generated")
    print(f"[issue_spanish_residual_repair_diagnostic] Rule version: {RULE_VERSION}")
    print(f"[issue_spanish_residual_repair_diagnostic] Run id: {run_id}")
    print(f"[issue_spanish_residual_repair_diagnostic] Decision run id: {selected_decision_run_id}")
    print(f"[issue_spanish_residual_repair_diagnostic] Candidates: {len(classified):,}")
    print(f"[issue_spanish_residual_repair_diagnostic] Ready shadow: {counts['ready_shadow']:,}")
    print(f"[issue_spanish_residual_repair_diagnostic] Needs context: {counts['needs_context']:,}")
    print(f"[issue_spanish_residual_repair_diagnostic] Token policy review: {counts['token_policy_review']:,}")
    print(f"[issue_spanish_residual_repair_diagnostic] Report: {txt_path}")
    return {
        "run_id": run_id,
        "decision_run_id": selected_decision_run_id,
        "candidate_count": len(classified),
        "ready_shadow": counts["ready_shadow"],
        "needs_context": counts["needs_context"],
        "token_policy_review": counts["token_policy_review"],
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Diagnose reusable repairs for spanish residual issue-review rows.")
    parser.add_argument("--decision-run-id", type=int, default=None)
    args = parser.parse_args()
    main(decision_run_id=args.decision_run_id)
