from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "full_coverage_suspicion_review_queue_v1"
QUEUE_STRATEGY = "full_coverage_false_ready_audit"
RUN_AGENT_KEY = "coverage_trust_auditor"


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def short(value: str | None, limit: int = 220) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def load_bridge_run(conn, bridge_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_full_coverage_lifecycle_bridge_runs
        WHERE id = ?
        """,
        (bridge_run_id,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"Bridge run not found: {bridge_run_id}")
    return dict(row)


def pick_ledger_item(conn, ledger_run_id: int, segment_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND segment_id = ?
        ORDER BY
          CASE issue_family
            WHEN 'semantic_review_router' THEN 0
            WHEN 'short_label_style_microagent' THEN 1
            WHEN 'dynamic_ck3_expression_microagent' THEN 2
            WHEN 'gender_token_microagent' THEN 3
            ELSE 9
          END,
          id
        LIMIT 1
        """,
        (ledger_run_id, segment_id),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No ledger item found for segment {segment_id} in ledger run {ledger_run_id}")
    return dict(row)


def suggested_bucket(row: dict[str, Any], source: dict[str, Any]) -> str:
    key = str(source.get("source_key") or "").lower()
    text = str(source.get("confirmed_text") or "")
    sources = json.loads(row.get("coverage_sources_json") or "{}")
    if "coerced" in key and "volunt" in text.lower():
        return "semantic_contradiction_suspect"
    if "dynamic_ck3_expression_microagent" in json.loads(row.get("issue_families_json") or "{}"):
        return "dynamic_or_gender_boundary_review"
    if sources == {
        "semantic_review_explained_checkpoint:carry_forward": 1,
        "short_label_pure_no_token_checkpoint:carry_forward": 1,
    }:
        return "semantic_short_label_pair_recheck"
    return "full_coverage_ready_recheck"


def suggested_decision(bucket: str) -> str:
    if bucket == "semantic_contradiction_suspect":
        return "needs_repair"
    if bucket == "dynamic_or_gender_boundary_review":
        return "needs_domain_context"
    return "safe"


def fetch_ready_rows(conn, bridge_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
          item.*,
          source.english_text,
          source.spanish_text,
          source.old_text,
          confirmation.confirmed_text
        FROM ml_issue_full_coverage_lifecycle_bridge_items item
        JOIN source_segments source ON source.id = item.segment_id
        LEFT JOIN segment_confirmations confirmation ON confirmation.segment_id = item.segment_id
        WHERE item.run_id = ?
          AND item.bridge_ready = 1
        ORDER BY item.segment_id
        """,
        (bridge_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def ensure_queue_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_review_queue_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            agent_key TEXT NOT NULL,
            issue_family TEXT,
            queue_strategy TEXT NOT NULL,
            limit_count INTEGER,
            per_bucket INTEGER NOT NULL DEFAULT 0,
            selected_count INTEGER NOT NULL DEFAULT 0,
            open_count INTEGER NOT NULL DEFAULT 0,
            reviewed_count INTEGER NOT NULL DEFAULT 0,
            bucket_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            decisions_template_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_review_queue_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            issue_family TEXT NOT NULL,
            issue_kind TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            queue_bucket TEXT NOT NULL,
            priority_score REAL NOT NULL DEFAULT 0,
            review_status TEXT NOT NULL DEFAULT 'pending',
            suggested_decision TEXT NOT NULL,
            evidence_text TEXT,
            evidence_json TEXT,
            english_text TEXT,
            spanish_text TEXT,
            confirmed_text TEXT,
            reviewer_decision TEXT,
            reviewer_notes TEXT,
            corrected_text TEXT,
            created_at TEXT NOT NULL,
            reviewed_at TEXT
        )
        """
    )


def write_artifacts(
    settings: dict[str, Any],
    *,
    bridge_run: dict[str, Any],
    queue_run_id: int,
    rows: list[dict[str, Any]],
    bucket_counts: Counter[str],
) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_full_coverage_suspicion_review_queue"
    txt_path = base.with_suffix(".txt")
    csv_path = base.with_suffix(".csv")
    jsonl_path = base.with_suffix(".jsonl")
    decisions_path = reports_dir / f"{base.name}_decisions_template.jsonl"

    lines = [
        "Full Coverage Suspicion Review Queue",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {queue_run_id}",
        f"Bridge run id: {bridge_run['id']}",
        f"Partial coverage run id: {bridge_run['source_partial_coverage_run_id']}",
        f"Ledger run id: {bridge_run['source_ledger_run_id']}",
        f"Segment-state run id: {bridge_run['source_segment_state_run_id']}",
        f"Items: {len(rows)}",
        "",
        "Bucket counts:",
    ]
    for bucket, count in bucket_counts.most_common():
        lines.append(f"- {bucket}: {count}")
    lines.extend(["", "Items:"])
    for row in rows:
        lines.extend(
            [
                f"- queue_item={row['queue_item_id']} segment={row['segment_id']} bucket={row['queue_bucket']}",
                f"  key: {row['source_key']}",
                f"  path: {row['relative_path']}:{row.get('source_line_number') or ''}",
                f"  decision_hint: {row['suggested_decision']}",
                f"  confirmed: {short(row.get('confirmed_text'))}",
                f"  english: {short(row.get('english_text'))}",
                f"  spanish: {short(row.get('spanish_text'))}",
            ]
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    fieldnames = [
        "queue_item_id",
        "queue_run_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "suggested_decision",
        "issue_family",
        "issue_kind",
        "agent_key",
        "confirmed_text",
        "english_text",
        "spanish_text",
        "coverage_sources_json",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})

    with jsonl_path.open("w", encoding="utf-8") as queue_handle, decisions_path.open(
        "w", encoding="utf-8"
    ) as decisions_handle:
        for row in rows:
            payload = {key: row.get(key) for key in fieldnames}
            queue_handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            decisions_handle.write(
                json.dumps(
                    {
                        "queue_item_id": row["queue_item_id"],
                        "queue_run_id": row["queue_run_id"],
                        "ledger_item_id": row["ledger_item_id"],
                        "segment_id": row["segment_id"],
                        "source_key": row["source_key"],
                        "decision": "pending",
                        "suggested_decision": row["suggested_decision"],
                        "corrected_text": "",
                        "reviewer_notes": "",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
    return txt_path, csv_path, jsonl_path, decisions_path


def build_queue(conn, bridge_run_id: int) -> tuple[int, list[dict[str, Any]], dict[str, Any]]:
    bridge_run = load_bridge_run(conn, bridge_run_id)
    ledger_run_id = int(bridge_run["source_ledger_run_id"])
    source_rows = fetch_ready_rows(conn, bridge_run_id)
    if not source_rows:
        raise SystemExit(f"No bridge-ready rows found for bridge run {bridge_run_id}")

    ensure_queue_tables(conn)
    started_at = now_text()
    bucket_counts = Counter()
    queue_run_cur = conn.execute(
        """
        INSERT INTO ml_issue_review_queue_runs (
            rule_version,
            ledger_run_id,
            agent_key,
            issue_family,
            queue_strategy,
            limit_count,
            per_bucket,
            selected_count,
            open_count,
            reviewed_count,
            bucket_counts_json,
            started_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            ledger_run_id,
            RUN_AGENT_KEY,
            "coverage_trust_audit",
            QUEUE_STRATEGY,
            len(source_rows),
            0,
            len(source_rows),
            len(source_rows),
            0,
            "{}",
            started_at,
            started_at,
        ),
    )
    queue_run_id = int(queue_run_cur.lastrowid)

    inserted: list[dict[str, Any]] = []
    for idx, row in enumerate(source_rows, start=1):
        ledger = pick_ledger_item(conn, ledger_run_id, int(row["segment_id"]))
        bucket = suggested_bucket(row, row)
        decision_hint = suggested_decision(bucket)
        bucket_counts[bucket] += 1
        evidence = {
            "bridge_run_id": bridge_run_id,
            "partial_coverage_run_id": row.get("partial_coverage_run_id"),
            "partial_coverage_item_id": row.get("partial_coverage_item_id"),
            "coverage_sources": json.loads(row.get("coverage_sources_json") or "{}"),
            "issue_families": json.loads(row.get("issue_families_json") or "{}"),
            "review_note": "Do not auto-close: full coverage bridge sample found semantic false-safe risk.",
        }
        item_cur = conn.execute(
            """
            INSERT INTO ml_issue_review_queue_items (
                run_id,
                ledger_run_id,
                ledger_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                issue_family,
                issue_kind,
                agent_key,
                queue_bucket,
                priority_score,
                review_status,
                suggested_decision,
                evidence_text,
                evidence_json,
                english_text,
                spanish_text,
                confirmed_text,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                queue_run_id,
                ledger_run_id,
                int(ledger["id"]),
                int(row["segment_id"]),
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                ledger["issue_family"],
                ledger["issue_kind"],
                ledger["agent_key"],
                bucket,
                float(1000 - idx),
                decision_hint,
                row.get("evidence_preview"),
                json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                row.get("english_text"),
                row.get("spanish_text"),
                row.get("confirmed_text"),
                started_at,
            ),
        )
        out = dict(row)
        out.update(
            {
                "queue_item_id": int(item_cur.lastrowid),
                "queue_run_id": queue_run_id,
                "ledger_item_id": int(ledger["id"]),
                "issue_family": ledger["issue_family"],
                "issue_kind": ledger["issue_kind"],
                "agent_key": ledger["agent_key"],
                "queue_bucket": bucket,
                "suggested_decision": decision_hint,
            }
        )
        inserted.append(out)

    conn.execute(
        """
        UPDATE ml_issue_review_queue_runs
        SET bucket_counts_json = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (json.dumps(dict(bucket_counts), ensure_ascii=False, sort_keys=True), now_text(), queue_run_id),
    )
    return queue_run_id, inserted, bridge_run


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Build a review queue from suspicious full-coverage bridge-ready rows.")
    parser.add_argument("--bridge-run-id", type=int, default=None)
    args = parser.parse_args()

    settings = db.load_settings()
    with db.connect(settings) as conn:
        conn.row_factory = db.sqlite3.Row
        bridge_run_id = args.bridge_run_id
        if bridge_run_id is None:
            latest = conn.execute(
                """
                SELECT id
                FROM ml_issue_full_coverage_lifecycle_bridge_runs
                WHERE finished_at IS NOT NULL
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            if latest is None:
                raise SystemExit("No finished full coverage bridge runs found")
            bridge_run_id = int(latest["id"])

        queue_run_id, rows, bridge_run = build_queue(conn, bridge_run_id)
        bucket_counts = Counter(row["queue_bucket"] for row in rows)
        txt_path, csv_path, jsonl_path, decisions_path = write_artifacts(
            settings,
            bridge_run=bridge_run,
            queue_run_id=queue_run_id,
            rows=rows,
            bucket_counts=bucket_counts,
        )
        conn.execute(
            """
            UPDATE ml_issue_review_queue_runs
            SET report_path = ?,
                csv_path = ?,
                jsonl_path = ?,
                decisions_template_path = ?,
                finished_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                str(decisions_path),
                now_text(),
                now_text(),
                queue_run_id,
            ),
        )
        conn.commit()

    print("[full_coverage_suspicion_review_queue] Queue generated")
    print(f"[full_coverage_suspicion_review_queue] Bridge run id: {bridge_run_id}")
    print(f"[full_coverage_suspicion_review_queue] Queue run id: {queue_run_id}")
    print(f"[full_coverage_suspicion_review_queue] Items: {len(rows)}")
    for bucket, count in bucket_counts.most_common():
        print(f"[full_coverage_suspicion_review_queue] {bucket}: {count}")
    print(f"[full_coverage_suspicion_review_queue] Report: {txt_path}")
    print(f"[full_coverage_suspicion_review_queue] Decisions: {decisions_path}")
    return {
        "bridge_run_id": bridge_run_id,
        "queue_run_id": queue_run_id,
        "items": len(rows),
        "report_path": str(txt_path),
        "decisions_template_path": str(decisions_path),
    }


if __name__ == "__main__":
    main()
