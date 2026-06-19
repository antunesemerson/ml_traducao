from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from issue_short_label_semantic_recheck_assisted_review import classify
from issue_short_label_semantic_recheck_queue import DEFAULT_BUCKET, queue_bucket


RULE_VERSION = "issue_short_label_semantic_guarded_expansion_dry_run_v1"
POLICY_NAME = "short_label_semantic_tier1_guarded_expansion_shadow"
DRY_RUN_ACTION = "would_close_short_label_semantic_tier1_composition"
PRODUCTION_RELEASE_ALLOWED = 0


def latest_diagnostic_run_id(conn, diagnostic_run_id: int | None) -> int:
    if diagnostic_run_id is not None:
        return diagnostic_run_id
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_semantic_composition_diagnostic_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished short-label + semantic diagnostic run found.")
    return int(row["id"])


def latest_checkpoint_run_id(conn, checkpoint_run_id: int | None) -> int:
    if checkpoint_run_id is not None:
        return checkpoint_run_id
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_semantic_recheck_checkpoint_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished short-label + semantic recheck checkpoint run found.")
    return int(row["id"])


def fetch_run(conn, table_name: str, run_id: int) -> dict[str, Any]:
    row = conn.execute(f"SELECT * FROM {table_name} WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"{table_name} run not found: {run_id}")
    return dict(row)


def report_paths(settings: dict[str, Any], diagnostic_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_semantic_guarded_expansion_dry_run_{diagnostic_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_semantic_guarded_expansion_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            diagnostic_run_id INTEGER NOT NULL,
            checkpoint_run_id INTEGER NOT NULL,
            partial_coverage_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            allowed_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            bucket_counts_json TEXT,
            decision_counts_json TEXT,
            block_reason_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(diagnostic_run_id) REFERENCES ml_issue_short_label_semantic_composition_diagnostic_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_short_label_semantic_recheck_checkpoint_runs(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS ml_issue_short_label_semantic_guarded_expansion_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            diagnostic_run_id INTEGER NOT NULL,
            checkpoint_run_id INTEGER NOT NULL,
            diagnostic_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            path_group TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            queue_bucket TEXT NOT NULL,
            surface_bucket TEXT NOT NULL,
            total_issue_count INTEGER NOT NULL DEFAULT 0,
            covered_issue_count INTEGER NOT NULL DEFAULT 0,
            open_issue_count INTEGER NOT NULL DEFAULT 0,
            dry_run_decision TEXT NOT NULL,
            dry_run_allowed INTEGER NOT NULL DEFAULT 0,
            dry_run_action TEXT NOT NULL,
            block_reason TEXT,
            has_ck3_token INTEGER NOT NULL DEFAULT 0,
            word_count INTEGER NOT NULL DEFAULT 0,
            text_length INTEGER NOT NULL DEFAULT 0,
            confirmed_text TEXT,
            english_text TEXT,
            spanish_text TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_short_label_semantic_guarded_expansion_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(diagnostic_item_id) REFERENCES ml_issue_short_label_semantic_composition_diagnostic_items(id) ON DELETE CASCADE,
            FOREIGN KEY(segment_id) REFERENCES source_segments(id) ON DELETE CASCADE,
            UNIQUE(run_id, diagnostic_item_id)
        );

        CREATE INDEX IF NOT EXISTS idx_short_semantic_guarded_expansion_items_allowed
        ON ml_issue_short_label_semantic_guarded_expansion_items(run_id, dry_run_allowed, queue_bucket);

        CREATE INDEX IF NOT EXISTS idx_short_semantic_guarded_expansion_items_segment
        ON ml_issue_short_label_semantic_guarded_expansion_items(segment_id);
        """
    )


def fetch_candidates(conn, diagnostic_run_id: int, composition_bucket: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_semantic_composition_diagnostic_items
        WHERE run_id = ?
          AND composition_bucket = ?
        ORDER BY path_group, surface_bucket, relative_path, source_line_number, source_key
        """,
        (diagnostic_run_id, composition_bucket),
    ).fetchall()
    return [dict(row) for row in rows]


def build_item(row: dict[str, Any], *, run_id: int, diagnostic_run_id: int, checkpoint_run_id: int) -> dict[str, Any]:
    payload = dict(row)
    payload["queue_bucket"] = queue_bucket(payload)
    decision, reason = classify(payload)
    allowed = 1 if decision == "composition_ready" else 0
    return {
        "run_id": run_id,
        "diagnostic_run_id": diagnostic_run_id,
        "checkpoint_run_id": checkpoint_run_id,
        "diagnostic_item_id": int(row["id"]),
        "segment_id": int(row["segment_id"]),
        "relative_path": row.get("relative_path") or "",
        "path_group": row.get("path_group") or "",
        "source_key": row.get("source_key") or "",
        "source_line_number": row.get("source_line_number"),
        "queue_bucket": payload["queue_bucket"],
        "surface_bucket": row.get("surface_bucket") or "",
        "total_issue_count": int(row.get("total_issue_count") or 0),
        "covered_issue_count": int(row.get("covered_issue_count") or 0),
        "open_issue_count": int(row.get("open_issue_count") or 0),
        "dry_run_decision": decision,
        "dry_run_allowed": allowed,
        "dry_run_action": DRY_RUN_ACTION if allowed else "blocked",
        "block_reason": "" if allowed else reason,
        "has_ck3_token": int(row.get("has_ck3_token") or 0),
        "word_count": int(row.get("word_count") or 0),
        "text_length": int(row.get("text_length") or 0),
        "confirmed_text": row.get("confirmed_text"),
        "english_text": row.get("english_text"),
        "spanish_text": row.get("spanish_text"),
        "reasons_json": json.dumps(
            {
                "rule_version": RULE_VERSION,
                "classifier_rule": "issue_short_label_semantic_recheck_assisted_review_v1",
                "decision_reason": reason,
                "source": "tier1_guarded_expansion_shadow",
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        "created_at": db.utc_now(),
    }


def insert_items(conn, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    columns = [
        "run_id",
        "diagnostic_run_id",
        "checkpoint_run_id",
        "diagnostic_item_id",
        "segment_id",
        "relative_path",
        "path_group",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "surface_bucket",
        "total_issue_count",
        "covered_issue_count",
        "open_issue_count",
        "dry_run_decision",
        "dry_run_allowed",
        "dry_run_action",
        "block_reason",
        "has_ck3_token",
        "word_count",
        "text_length",
        "confirmed_text",
        "english_text",
        "spanish_text",
        "reasons_json",
        "created_at",
    ]
    conn.executemany(
        f"""
        INSERT INTO ml_issue_short_label_semantic_guarded_expansion_items
        ({", ".join(columns)})
        VALUES ({", ".join("?" for _ in columns)})
        """,
        [tuple(item.get(column) for column in columns) for item in items],
    )


def write_outputs(
    *,
    paths: tuple[Path, Path, Path],
    run_id: int,
    diagnostic_run: dict[str, Any],
    checkpoint_run: dict[str, Any],
    items: list[dict[str, Any]],
) -> None:
    txt_path, csv_path, jsonl_path = paths
    bucket_counts = Counter(item["queue_bucket"] for item in items)
    decision_counts = Counter(item["dry_run_decision"] for item in items)
    block_counts = Counter(item["block_reason"] for item in items if not item["dry_run_allowed"])
    allowed_by_bucket = Counter(item["queue_bucket"] for item in items if item["dry_run_allowed"])
    path_counts = Counter(item["path_group"] for item in items)

    fieldnames = [
        "run_id",
        "segment_id",
        "relative_path",
        "path_group",
        "source_key",
        "queue_bucket",
        "surface_bucket",
        "dry_run_decision",
        "dry_run_allowed",
        "block_reason",
        "total_issue_count",
        "covered_issue_count",
        "has_ck3_token",
        "word_count",
        "text_length",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in items:
            writer.writerow({field: item.get(field) for field in fieldnames})
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")

    allowed = sum(1 for item in items if item["dry_run_allowed"])
    total = len(items)
    lines = [
        "Short-label + semantic guarded expansion dry-run",
        f"Rule version: {RULE_VERSION}",
        f"Dry-run id: {run_id}",
        f"Diagnostic run id: {diagnostic_run['id']}",
        f"Checkpoint run id: {checkpoint_run['id']}",
        f"Partial coverage run id: {diagnostic_run['partial_coverage_run_id']}",
        f"Ledger run id: {diagnostic_run['ledger_run_id']}",
        f"Segment-state run id: {diagnostic_run['segment_state_run_id']}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Candidates: {total:,}",
        f"- Would allow: {allowed:,}",
        f"- Would block: {total - allowed:,}",
        f"- Allowed rate: {(allowed / total if total else 0):.2%}",
        "",
        "Decisions:",
        *[f"- {key}: {value:,}" for key, value in decision_counts.most_common()],
        "",
        "Allowed by bucket:",
        *[
            f"- {bucket}: {allowed_by_bucket[bucket]:,}/{count:,} ({(allowed_by_bucket[bucket] / count if count else 0):.2%})"
            for bucket, count in bucket_counts.most_common()
        ],
        "",
        "Top path groups:",
        *[f"- {path}: {count:,}" for path, count in path_counts.most_common(20)],
        "",
        "Block reasons:",
        *[f"- {reason}: {count:,}" for reason, count in block_counts.most_common(20)],
        "",
        "Blocked samples:",
    ]
    shown = 0
    for item in items:
        if item["dry_run_allowed"]:
            continue
        lines.append(
            f"- {item['dry_run_decision']} | {item['queue_bucket']} | segment={item['segment_id']} "
            f"{item['relative_path']}::{item['source_key']} | {item['block_reason']}"
        )
        lines.append(f"  text: {(item.get('confirmed_text') or '').replace(chr(10), '\\n')[:180]}")
        if shown >= 40:
            break
        shown += 1
    lines.extend(
        [
            "",
            "Safety note:",
            "- This is a dry-run only.",
            "- It does not write source/output and does not promote lifecycle closure.",
            "- Use this result to decide whether to build a guarded lifecycle bridge with explicit blockers.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    diagnostic_run_id: int | None = None,
    checkpoint_run_id: int | None = None,
    composition_bucket: str = DEFAULT_BUCKET,
) -> dict[str, Any]:
    settings = db.load_settings()
    print("[issue_short_label_semantic_guarded_expansion_dry_run] Starting dry-run")
    print(f"[issue_short_label_semantic_guarded_expansion_dry_run] Rule version: {RULE_VERSION}")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_diagnostic_run_id = latest_diagnostic_run_id(conn, diagnostic_run_id)
        selected_checkpoint_run_id = latest_checkpoint_run_id(conn, checkpoint_run_id)
        diagnostic_run = fetch_run(
            conn,
            "ml_issue_short_label_semantic_composition_diagnostic_runs",
            selected_diagnostic_run_id,
        )
        checkpoint_run = fetch_run(
            conn,
            "ml_issue_short_label_semantic_recheck_checkpoint_runs",
            selected_checkpoint_run_id,
        )
        paths = report_paths(settings, selected_diagnostic_run_id)
        now = db.utc_now()
        cur = conn.execute(
            """
            INSERT INTO ml_issue_short_label_semantic_guarded_expansion_runs (
                rule_version,
                policy_name,
                policy_status,
                diagnostic_run_id,
                checkpoint_run_id,
                partial_coverage_run_id,
                ledger_run_id,
                segment_state_run_id,
                production_release_allowed,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                POLICY_NAME,
                "shadow",
                selected_diagnostic_run_id,
                selected_checkpoint_run_id,
                int(diagnostic_run["partial_coverage_run_id"]),
                int(diagnostic_run["ledger_run_id"]),
                int(diagnostic_run["segment_state_run_id"]),
                PRODUCTION_RELEASE_ALLOWED,
                str(paths[0]),
                str(paths[1]),
                str(paths[2]),
                now,
                now,
            ),
        )
        run_id = int(cur.lastrowid)
        candidates = fetch_candidates(conn, selected_diagnostic_run_id, composition_bucket)
        items = [
            build_item(
                row,
                run_id=run_id,
                diagnostic_run_id=selected_diagnostic_run_id,
                checkpoint_run_id=selected_checkpoint_run_id,
            )
            for row in candidates
        ]
        insert_items(conn, items)
        bucket_counts = Counter(item["queue_bucket"] for item in items)
        decision_counts = Counter(item["dry_run_decision"] for item in items)
        block_counts = Counter(item["block_reason"] for item in items if not item["dry_run_allowed"])
        allowed = sum(1 for item in items if item["dry_run_allowed"])
        finished_at = db.utc_now()
        conn.execute(
            """
            UPDATE ml_issue_short_label_semantic_guarded_expansion_runs
            SET
                candidate_count = ?,
                allowed_count = ?,
                blocked_count = ?,
                bucket_counts_json = ?,
                decision_counts_json = ?,
                block_reason_counts_json = ?,
                finished_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                len(items),
                allowed,
                len(items) - allowed,
                json.dumps(dict(bucket_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(block_counts), ensure_ascii=False, sort_keys=True),
                finished_at,
                finished_at,
                run_id,
            ),
        )
        write_outputs(paths=paths, run_id=run_id, diagnostic_run=diagnostic_run, checkpoint_run=checkpoint_run, items=items)
        conn.commit()
    print("[issue_short_label_semantic_guarded_expansion_dry_run] Dry-run generated")
    print(f"[issue_short_label_semantic_guarded_expansion_dry_run] Run id: {run_id}")
    print(f"[issue_short_label_semantic_guarded_expansion_dry_run] Candidates: {len(items):,}")
    print(f"[issue_short_label_semantic_guarded_expansion_dry_run] Allowed: {allowed:,}")
    print(f"[issue_short_label_semantic_guarded_expansion_dry_run] Blocked: {len(items) - allowed:,}")
    print(f"[issue_short_label_semantic_guarded_expansion_dry_run] Report: {paths[0]}")
    return {
        "run_id": run_id,
        "diagnostic_run_id": selected_diagnostic_run_id,
        "checkpoint_run_id": selected_checkpoint_run_id,
        "candidates": len(items),
        "allowed": allowed,
        "blocked": len(items) - allowed,
        "report_path": str(paths[0]),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Dry-run guarded expansion over all short-label + semantic Tier 1 composition candidates."
    )
    parser.add_argument("--diagnostic-run-id", type=int, default=None)
    parser.add_argument("--checkpoint-run-id", type=int, default=None)
    parser.add_argument("--composition-bucket", default=DEFAULT_BUCKET)
    args = parser.parse_args()
    main(
        diagnostic_run_id=args.diagnostic_run_id,
        checkpoint_run_id=args.checkpoint_run_id,
        composition_bucket=args.composition_bucket,
    )
