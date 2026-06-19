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


RULE_VERSION = "issue_short_label_compact_ui_semantic_companion_queue_v1"
QUEUE_STRATEGY = "short_label_compact_ui_semantic_companion_issue_queue"
AGENT_KEY = "short_label_companion_coordinator"
ISSUE_FAMILY = "short_label_companion_composition"


def report_paths(settings: dict[str, Any], checkpoint_run_id: int) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_compact_ui_semantic_companion_queue_checkpoint_{checkpoint_run_id}"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        reports_dir / f"{base.name}_decisions_template.jsonl",
    )


def latest_checkpoint_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_compact_ui_semantic_reviewed_checkpoint_runs
        WHERE finished_at IS NOT NULL
          AND checkpoint_allowed_count > 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished reviewed compact UI semantic checkpoint found.")
    return int(row["id"])


def fetch_checkpoint_run(conn, checkpoint_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_compact_ui_semantic_reviewed_checkpoint_runs
        WHERE id = ?
        """,
        (checkpoint_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Checkpoint run not found: {checkpoint_run_id}")
    return dict(row)


def fetch_companion_rows(conn, *, checkpoint_run_id: int, ledger_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH allowed AS (
            SELECT DISTINCT segment_id
            FROM ml_issue_short_label_compact_ui_semantic_reviewed_checkpoint_items
            WHERE checkpoint_run_id = ?
              AND checkpoint_allowed = 1
        )
        SELECT
            item.id AS ledger_item_id,
            item.run_id AS ledger_run_id,
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.source_line_number,
            item.issue_family,
            item.issue_kind,
            item.evidence_text,
            item.evidence_json,
            source.english_text,
            source.spanish_text,
            confirmation.confirmed_text
        FROM allowed
        JOIN ml_issue_ledger_items item
          ON item.segment_id = allowed.segment_id
         AND item.run_id = ?
         AND item.status = 'open'
         AND item.issue_family <> 'short_label_style_microagent'
        JOIN source_segments source ON source.id = item.segment_id
        LEFT JOIN segment_confirmations confirmation ON confirmation.segment_id = item.segment_id
        ORDER BY item.issue_family, item.relative_path, item.source_key
        """,
        (checkpoint_run_id, ledger_run_id),
    ).fetchall()
    return [dict(row) for row in rows]


def suggested_decision(row: dict[str, Any]) -> str:
    family = str(row.get("issue_family") or "")
    key = str(row.get("source_key") or "")
    path = str(row.get("relative_path") or "")
    text = str(row.get("confirmed_text") or row.get("evidence_text") or "")
    if family in {"religion_semantic_microagent", "title_policy_microagent", "culture_semantic_microagent"}:
        if path.startswith(("triggers/", "religion/", "succession_laws_l_spanish.yml", "interactions_l_spanish.yml")):
            return "composition_ready"
        if key.endswith("_modifier_desc") and len(text) <= 140:
            return "composition_ready"
    if family == "dynamic_ck3_expression_microagent" and "GetTrait(" in text:
        return "composition_ready"
    return "needs_domain_context"


def queue_bucket(row: dict[str, Any]) -> str:
    family = str(row.get("issue_family") or "unknown_family")
    kind = str(row.get("issue_kind") or "unknown_kind")
    return f"{family}|{kind}"


def priority_score(row: dict[str, Any]) -> float:
    text = str(row.get("confirmed_text") or row.get("evidence_text") or "")
    score = 100.0
    score -= min(len(text), 220) * 0.08
    if suggested_decision(row) == "composition_ready":
        score += 15.0
    return round(score, 4)


def insert_queue_run(
    conn,
    *,
    checkpoint_run: dict[str, Any],
    rows: list[dict[str, Any]],
    paths: tuple[Path, Path, Path, Path],
) -> int:
    now = db.utc_now()
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    bucket_counts = Counter(queue_bucket(row) for row in rows)
    run_id = conn.execute(
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
            report_path,
            csv_path,
            jsonl_path,
            decisions_template_path,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            int(checkpoint_run["ledger_run_id"]),
            AGENT_KEY,
            ISSUE_FAMILY,
            QUEUE_STRATEGY,
            len(rows),
            len(rows),
            len(rows),
            json.dumps(dict(bucket_counts), ensure_ascii=False, sort_keys=True),
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            str(decisions_template_path),
            now,
            now,
            now,
        ),
    ).lastrowid
    return int(run_id)


def insert_queue_items(conn, *, queue_run_id: int, rows: list[dict[str, Any]]) -> None:
    now = db.utc_now()
    conn.executemany(
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
        [
            (
                queue_run_id,
                int(row["ledger_run_id"]),
                int(row["ledger_item_id"]),
                int(row["segment_id"]),
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                row["issue_family"],
                row["issue_kind"],
                AGENT_KEY,
                queue_bucket(row),
                priority_score(row),
                suggested_decision(row),
                row.get("evidence_text"),
                row.get("evidence_json"),
                row.get("english_text"),
                row.get("spanish_text"),
                row.get("confirmed_text"),
                now,
            )
            for row in rows
        ],
    )


def write_reports(
    *,
    paths: tuple[Path, Path, Path, Path],
    queue_run_id: int,
    checkpoint_run_id: int,
    rows: list[dict[str, Any]],
) -> None:
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    bucket_counts = Counter(queue_bucket(row) for row in rows)
    decision_counts = Counter(suggested_decision(row) for row in rows)

    fieldnames = [
        "queue_run_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "issue_family",
        "issue_kind",
        "queue_bucket",
        "suggested_decision",
        "confirmed_preview",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "queue_run_id": queue_run_id,
                    "ledger_item_id": row["ledger_item_id"],
                    "segment_id": row["segment_id"],
                    "relative_path": row["relative_path"],
                    "source_key": row["source_key"],
                    "issue_family": row["issue_family"],
                    "issue_kind": row["issue_kind"],
                    "queue_bucket": queue_bucket(row),
                    "suggested_decision": suggested_decision(row),
                    "confirmed_preview": short(row.get("confirmed_text") or row.get("evidence_text"), 220),
                }
            )

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                "queue_run_id": queue_run_id,
                "ledger_item_id": row["ledger_item_id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "issue_family": row["issue_family"],
                "issue_kind": row["issue_kind"],
                "queue_bucket": queue_bucket(row),
                "suggested_decision": suggested_decision(row),
                "evidence_text": row.get("evidence_text"),
                "english_text": row.get("english_text"),
                "spanish_text": row.get("spanish_text"),
                "confirmed_text": row.get("confirmed_text"),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                "queue_run_id": queue_run_id,
                "ledger_item_id": row["ledger_item_id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "issue_family": row["issue_family"],
                "issue_kind": row["issue_kind"],
                "decision": "pending",
                "decision_options": [
                    "composition_ready",
                    "needs_domain_context",
                    "needs_repair",
                    "needs_new_microagent",
                ],
                "suggested_decision": suggested_decision(row),
                "notes": "",
                "corrected_text": "",
                "confirmed_text": row.get("confirmed_text"),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Short-label compact UI semantic companion queue",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Queue run id: {queue_run_id}",
        "",
        "Summary:",
        f"- selected: {len(rows):,}",
        "",
        "Suggested decisions:",
        *[f"- {name}: {count:,}" for name, count in decision_counts.most_common()],
        "",
        "Buckets:",
        *[f"- {name}: {count:,}" for name, count in bucket_counts.most_common()],
        "",
        "Safety:",
        "- Queue only; no lifecycle closure.",
        "- No source/output writes.",
        "- Companion issues must be reviewed before composing full segment closure.",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, checkpoint_run_id: int | None) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_checkpoint_run_id = checkpoint_run_id or latest_checkpoint_run_id(conn)
        checkpoint_run = fetch_checkpoint_run(conn, selected_checkpoint_run_id)
        rows = fetch_companion_rows(
            conn,
            checkpoint_run_id=selected_checkpoint_run_id,
            ledger_run_id=int(checkpoint_run["ledger_run_id"]),
        )
        paths = report_paths(settings, selected_checkpoint_run_id)
        queue_run_id = insert_queue_run(conn, checkpoint_run=checkpoint_run, rows=rows, paths=paths)
        insert_queue_items(conn, queue_run_id=queue_run_id, rows=rows)
        write_reports(paths=paths, queue_run_id=queue_run_id, checkpoint_run_id=selected_checkpoint_run_id, rows=rows)
        conn.commit()

    print("[issue_short_label_compact_ui_semantic_companion_queue] Queue generated")
    print(f"[issue_short_label_compact_ui_semantic_companion_queue] Checkpoint run id: {selected_checkpoint_run_id}")
    print(f"[issue_short_label_compact_ui_semantic_companion_queue] Queue run id: {queue_run_id}")
    print(f"[issue_short_label_compact_ui_semantic_companion_queue] Selected: {len(rows):,}")
    print(f"[issue_short_label_compact_ui_semantic_companion_queue] Report: {paths[0]}")
    print(f"[issue_short_label_compact_ui_semantic_companion_queue] Decisions template: {paths[3]}")
    return {
        "checkpoint_run_id": selected_checkpoint_run_id,
        "queue_run_id": queue_run_id,
        "selected": len(rows),
        "report_path": str(paths[0]),
        "decisions_template_path": str(paths[3]),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create companion issue queue for reviewed compact UI semantic short-label coverage.")
    parser.add_argument("--checkpoint-run-id", type=int)
    args = parser.parse_args()
    main(checkpoint_run_id=args.checkpoint_run_id)
