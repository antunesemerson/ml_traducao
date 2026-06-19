from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_select_cstring_preterite_ptbr_shadow_repair_dry_run_v1"
TARGET_AGENT = "select_cstring_local_player_preterite_verb_rewrite"
SHADOW_ACTION = "shadow_replace_select_cstring_preterite_literals_with_ptbr_neutral"
PRODUCTION_RELEASE_ALLOWED = 0


def latest_decision_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_select_cstring_preterite_ptbr_decision_runs
        WHERE target_agent = ?
          AND learning_only = 1
          AND apply_allowed = 0
          AND production_release_allowed = 0
        ORDER BY id DESC
        LIMIT 1
        """,
        (TARGET_AGENT,),
    ).fetchone()
    if row is None:
        raise RuntimeError("No Select_CString preterite PT-BR decision run found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_preterite_ptbr_shadow_repair_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            rule_version TEXT NOT NULL,
            target_agent TEXT NOT NULL,
            source_decision_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL,
            ready_count INTEGER NOT NULL,
            blocked_count INTEGER NOT NULL,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            decision_counts_json TEXT NOT NULL,
            block_counts_json TEXT NOT NULL,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_preterite_ptbr_shadow_repair_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            source_decision_run_id INTEGER NOT NULL,
            source_decision_id INTEGER NOT NULL,
            queue_item_id INTEGER,
            ledger_item_id INTEGER,
            segment_id INTEGER,
            target_agent TEXT NOT NULL,
            left_literal TEXT NOT NULL,
            right_literal TEXT,
            approved_ptbr_neutral_literal TEXT,
            proposed_left_literal TEXT,
            proposed_right_literal TEXT,
            decision TEXT NOT NULL,
            confidence_label TEXT NOT NULL,
            shadow_ready INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            shadow_action TEXT NOT NULL,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_select_cstring_preterite_ptbr_shadow_repair_runs(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_select_cstring_preterite_ptbr_shadow_items_run
        ON ml_issue_select_cstring_preterite_ptbr_shadow_repair_items(run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_select_cstring_preterite_ptbr_shadow_items_segment
        ON ml_issue_select_cstring_preterite_ptbr_shadow_repair_items(segment_id, shadow_ready)
        """
    )


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_select_cstring_preterite_ptbr_shadow_repair_dry_run"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def classify(row: dict[str, Any]) -> dict[str, Any]:
    decision = str(row["decision"] or "")
    approved = str(row["approved_ptbr_neutral_literal"] or "").strip()
    left = str(row["left_literal"] or "").strip()
    right = str(row["right_literal"] or "").strip()
    block_reason = ""
    shadow_ready = 0
    proposed_left = ""
    proposed_right = ""
    if decision not in {"approve_ptbr_neutral_literal", "edit_ptbr_neutral_literal"}:
        block_reason = "decision_not_positive"
    elif not approved:
        block_reason = "missing_ptbr_neutral_literal"
    elif not left or not right:
        block_reason = "missing_select_cstring_literal_pair"
    else:
        shadow_ready = 1
        proposed_left = approved
        proposed_right = approved
    return {
        "source_decision_id": int(row["id"]),
        "queue_item_id": row["queue_item_id"],
        "ledger_item_id": row["ledger_item_id"],
        "segment_id": row["segment_id"],
        "target_agent": str(row["target_agent"] or TARGET_AGENT),
        "left_literal": left,
        "right_literal": right,
        "approved_ptbr_neutral_literal": approved,
        "proposed_left_literal": proposed_left,
        "proposed_right_literal": proposed_right,
        "decision": decision,
        "confidence_label": str(row["confidence_label"] or ""),
        "shadow_ready": shadow_ready,
        "block_reason": block_reason,
        "shadow_action": SHADOW_ACTION,
        "production_release_allowed": PRODUCTION_RELEASE_ALLOWED,
    }


def run_dry_run(*, decision_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    now = datetime.now().isoformat(timespec="seconds")
    report_path, csv_path, jsonl_path = report_paths(settings)
    with db.connect(settings) as conn:
        ensure_tables(conn)
        source_run_id = decision_run_id or latest_decision_run_id(conn)
        source_rows = conn.execute(
            """
            SELECT *
            FROM ml_issue_select_cstring_preterite_ptbr_decisions
            WHERE run_id = ?
              AND target_agent = ?
              AND learning_only = 1
              AND apply_allowed = 0
              AND production_release_allowed = 0
            ORDER BY id
            """,
            (source_run_id, TARGET_AGENT),
        ).fetchall()
        items = [classify(dict(row)) for row in source_rows]
        decision_counts = Counter(item["decision"] for item in items)
        block_counts = Counter(item["block_reason"] or "ready" for item in items)
        ready_count = sum(1 for item in items if item["shadow_ready"])
        blocked_count = len(items) - ready_count
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_select_cstring_preterite_ptbr_shadow_repair_runs (
                started_at, finished_at, rule_version, target_agent, source_decision_run_id,
                candidate_count, ready_count, blocked_count, production_release_allowed,
                decision_counts_json, block_counts_json, report_path, csv_path, jsonl_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
            """,
            (
                now,
                now,
                RULE_VERSION,
                TARGET_AGENT,
                source_run_id,
                len(items),
                ready_count,
                blocked_count,
                json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(block_counts), ensure_ascii=False, sort_keys=True),
                str(report_path),
                str(csv_path),
                str(jsonl_path),
            ),
        )
        run_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO ml_issue_select_cstring_preterite_ptbr_shadow_repair_items (
                run_id, source_decision_run_id, source_decision_id, queue_item_id,
                ledger_item_id, segment_id, target_agent, left_literal, right_literal,
                approved_ptbr_neutral_literal, proposed_left_literal, proposed_right_literal,
                decision, confidence_label, shadow_ready, block_reason, shadow_action,
                production_release_allowed, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            [
                (
                    run_id,
                    source_run_id,
                    item["source_decision_id"],
                    item["queue_item_id"],
                    item["ledger_item_id"],
                    item["segment_id"],
                    item["target_agent"],
                    item["left_literal"],
                    item["right_literal"],
                    item["approved_ptbr_neutral_literal"],
                    item["proposed_left_literal"],
                    item["proposed_right_literal"],
                    item["decision"],
                    item["confidence_label"],
                    item["shadow_ready"],
                    item["block_reason"],
                    item["shadow_action"],
                    now,
                )
                for item in items
            ],
        )
        conn.commit()

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            payload = {"run_id": run_id, "source_decision_run_id": source_run_id, **item}
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = [
            "run_id",
            "source_decision_run_id",
            "segment_id",
            "left_literal",
            "right_literal",
            "approved_ptbr_neutral_literal",
            "proposed_left_literal",
            "proposed_right_literal",
            "decision",
            "shadow_ready",
            "block_reason",
            "production_release_allowed",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in items:
            writer.writerow({key: ({"run_id": run_id, "source_decision_run_id": source_run_id, **item}).get(key) for key in fieldnames})
    lines = [
        "Select_CString preterite PT-BR shadow repair dry-run",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Source decision run id: {source_run_id}",
        "",
        "Summary:",
        f"- Candidates: {len(items):,}",
        f"- Shadow ready: {ready_count:,}",
        f"- Blocked: {blocked_count:,}",
        "- Production release allowed: 0",
        "- Output writes: 0",
        "",
        "Decision counts:",
        *[f"- {key}: {value:,}" for key, value in decision_counts.most_common()],
        "",
        "Block counts:",
        *[f"- {key}: {value:,}" for key, value in block_counts.most_common()],
        "",
        "Boundary examples:",
    ]
    for item in [item for item in items if not item["shadow_ready"]][:20]:
        lines.append(f"- {item['left_literal']!r} -> {item['right_literal']!r}: {item['block_reason']}")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("[issue_select_cstring_preterite_ptbr_shadow_repair_dry_run] Dry-run complete")
    print(f"[issue_select_cstring_preterite_ptbr_shadow_repair_dry_run] Run id: {run_id}")
    print(f"[issue_select_cstring_preterite_ptbr_shadow_repair_dry_run] Ready: {ready_count:,}")
    print(f"[issue_select_cstring_preterite_ptbr_shadow_repair_dry_run] Blocked: {blocked_count:,}")
    print(f"[issue_select_cstring_preterite_ptbr_shadow_repair_dry_run] Report: {report_path}")
    return {
        "run_id": run_id,
        "source_decision_run_id": source_run_id,
        "candidate_count": len(items),
        "ready_count": ready_count,
        "blocked_count": blocked_count,
        "report_path": str(report_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a learning-only shadow repair dry-run for Select_CString preterite PT-BR evidence.")
    parser.add_argument("--decision-run-id", type=int, default=None)
    args = parser.parse_args()
    run_dry_run(decision_run_id=args.decision_run_id)
