from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from issue_short_label_pure_no_token_assisted_review import classify
from issue_short_label_pure_no_token_review_queue import DEFAULT_DOMAINS, fetch_candidates, normalize_domains


RULE_VERSION = "issue_short_label_pure_no_token_shadow_policy_v1"
POLICY_NAME = "short_label_pure_no_token_nominal_shadow"
AGENT_KEY = "micro_short_label_style"


def latest_ledger_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_ledger_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished ml_issue_ledger_runs found.")
    return int(row["id"])


def report_base(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    return reports_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_issue_short_label_pure_no_token_shadow_policy"


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_pure_no_token_shadow_policy_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            shadow_ready_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            decision_counts_json TEXT,
            reason_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_short_label_pure_no_token_shadow_policy_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            shadow_status TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT NOT NULL,
            confidence_label TEXT NOT NULL,
            evidence_text TEXT,
            validation_issue TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_short_label_pure_no_token_shadow_policy_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_short_label_pure_shadow_items_run
        ON ml_issue_short_label_pure_no_token_shadow_policy_items(run_id, shadow_status, decision);

        CREATE INDEX IF NOT EXISTS idx_short_label_pure_shadow_items_segment
        ON ml_issue_short_label_pure_no_token_shadow_policy_items(segment_id);
        """
    )


def main(*, ledger_run_id: int | None = None, domains: tuple[str, ...] = DEFAULT_DOMAINS) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = db.utc_now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_ledger = ledger_run_id or latest_ledger_run_id(conn)
        candidates = fetch_candidates(conn, ledger_run_id=selected_ledger, domains=domains)

        items: list[dict[str, Any]] = []
        decision_counts: Counter[str] = Counter()
        reason_counts: Counter[str] = Counter()
        for row in candidates:
            decision, reason, confidence = classify(row)
            shadow_status = "shadow_ready" if decision == "safe_short_label" else "shadow_blocked"
            decision_counts[decision] += 1
            reason_counts[reason] += 1
            items.append(
                {
                    "ledger_item_id": row["ledger_item_id"],
                    "segment_id": row["segment_id"],
                    "relative_path": row["relative_path"],
                    "source_key": row["source_key"],
                    "source_line_number": row["source_line_number"],
                    "shadow_status": shadow_status,
                    "decision": decision,
                    "reason": reason,
                    "confidence_label": confidence,
                    "evidence_text": row["evidence_text"],
                    "validation_issue": "" if shadow_status == "shadow_ready" else reason,
                }
            )

        shadow_ready = sum(1 for item in items if item["shadow_status"] == "shadow_ready")
        blocked = len(items) - shadow_ready
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_short_label_pure_no_token_shadow_policy_runs (
                rule_version, policy_name, agent_key, ledger_run_id,
                candidate_count, shadow_ready_count, blocked_count,
                production_release_allowed, decision_counts_json, reason_counts_json,
                started_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                POLICY_NAME,
                AGENT_KEY,
                selected_ledger,
                len(items),
                shadow_ready,
                blocked,
                json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(reason_counts), ensure_ascii=False, sort_keys=True),
                started_at,
                started_at,
            ),
        )
        run_id = int(cursor.lastrowid)
        created_at = db.utc_now()
        conn.executemany(
            """
            INSERT INTO ml_issue_short_label_pure_no_token_shadow_policy_items (
                run_id, ledger_item_id, segment_id, relative_path, source_key,
                source_line_number, shadow_status, decision, reason, confidence_label,
                evidence_text, validation_issue, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    item["ledger_item_id"],
                    item["segment_id"],
                    item["relative_path"],
                    item["source_key"],
                    item["source_line_number"],
                    item["shadow_status"],
                    item["decision"],
                    item["reason"],
                    item["confidence_label"],
                    item["evidence_text"],
                    item["validation_issue"],
                    created_at,
                )
                for item in items
            ],
        )

        base = report_base(settings)
        txt_path = base.with_suffix(".txt")
        csv_path = base.with_suffix(".csv")
        jsonl_path = base.with_suffix(".jsonl")
        conn.execute(
            """
            UPDATE ml_issue_short_label_pure_no_token_shadow_policy_runs
            SET report_path = ?, csv_path = ?, jsonl_path = ?,
                finished_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (str(txt_path), str(csv_path), str(jsonl_path), db.utc_now(), db.utc_now(), run_id),
        )
        conn.commit()

    fields = [
        "run_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "shadow_status",
        "decision",
        "reason",
        "confidence_label",
        "evidence_text",
        "validation_issue",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in items:
            writer.writerow({"run_id": run_id, **item})
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps({"run_id": run_id, **item}, ensure_ascii=False, sort_keys=True) + "\n")

    ready_rate = shadow_ready / len(items) if items else 0.0
    lines = [
        "Short Label Pure No-Token Shadow Policy",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Ledger run id: {selected_ledger}",
        f"Target domains: {', '.join(domains) if domains else 'all'}",
        f"Candidates: {len(items):,}",
        f"Shadow ready: {shadow_ready:,} ({ready_rate:.2%})",
        f"Blocked: {blocked:,}",
        "- Production release allowed: 0",
        "",
        "Decision counts:",
        *[f"- {key}: {value:,}" for key, value in decision_counts.most_common()],
        "",
        "Reason counts:",
        *[f"- {key}: {value:,}" for key, value in reason_counts.most_common()],
        "",
        "Interpretation:",
        "- This policy is shadow only; it does not write output and does not close lifecycle.",
        "- It estimates the safe nominal-label subset inside pure no-token short labels.",
        "- A checkpoint should require sample precision before segment-state consumption.",
        "",
        f"CSV: {csv_path}",
        f"JSONL: {jsonl_path}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("[issue_short_label_pure_no_token_shadow_policy] Shadow policy generated")
    print(f"[issue_short_label_pure_no_token_shadow_policy] Run id: {run_id}")
    print(f"[issue_short_label_pure_no_token_shadow_policy] Ledger run id: {selected_ledger}")
    print(f"[issue_short_label_pure_no_token_shadow_policy] Candidates: {len(items):,}")
    print(f"[issue_short_label_pure_no_token_shadow_policy] Shadow ready: {shadow_ready:,}")
    print(f"[issue_short_label_pure_no_token_shadow_policy] Blocked: {blocked:,}")
    print(f"[issue_short_label_pure_no_token_shadow_policy] Report: {txt_path}")
    return {
        "run_id": run_id,
        "ledger_run_id": selected_ledger,
        "candidate_count": len(items),
        "shadow_ready_count": shadow_ready,
        "blocked_count": blocked,
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apply conservative shadow policy to pure no-token short-label candidates.")
    parser.add_argument("--ledger-run-id", type=int, default=None)
    parser.add_argument(
        "--domains",
        default=",".join(DEFAULT_DOMAINS),
        help="Comma-separated evidence domains to evaluate; use 'all' for no domain filter.",
    )
    args = parser.parse_args()
    main(ledger_run_id=args.ledger_run_id, domains=normalize_domains(args.domains))
