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


RULE_VERSION = "issue_event_gender_suffix_blocker_queue_v1"
QUEUE_STRATEGY = "event_short_phrase_gender_suffix_blocker_queue"
AGENT_KEY = "micro_gender_suffix_surface"
ISSUE_FAMILY = "gender_suffix_surface_microagent"
DEFAULT_LIMIT = 80

TRUNCATED_SUFFIX_RE = re.compile(r"\b[A-Za-zÀ-ÿ]+ad\b", re.IGNORECASE)
DECISION_OPTIONS = [
    "needs_repair",
    "needs_domain_context",
    "needs_new_microagent",
    "manual_exception",
    "false_positive",
]


def latest_event_checkpoint_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_event_short_phrase_checkpoint_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished event short phrase checkpoint run found.")
    return int(row["id"])


def fetch_event_checkpoint_run(conn, *, run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_event_short_phrase_checkpoint_runs
        WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Event short phrase checkpoint run not found: {run_id}")
    return dict(row)


def mask_ck3_references(text: str) -> str:
    masked = re.sub(r"\[[^\]]+\]", " ", text)
    masked = re.sub(r"\$[^$]+\$", " ", masked)
    masked = re.sub(r"#[^#\s]+", " ", masked)
    return re.sub(r"\s+", " ", masked).strip()


def has_visible_truncated_suffix(text: str) -> bool:
    return TRUNCATED_SUFFIX_RE.search(mask_ck3_references(text)) is not None


def report_paths(settings: dict[str, Any], event_checkpoint_run_id: int) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_event_gender_suffix_blocker_queue_run_{event_checkpoint_run_id}"
    return (
        base.with_suffix(".txt"),
        base.with_suffix(".csv"),
        base.with_suffix(".jsonl"),
        base.with_name(base.name + "_decisions_template").with_suffix(".jsonl"),
    )


def fetch_candidates(conn, *, event_checkpoint_run_id: int, include_existing: bool) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.*,
            source.english_text,
            source.spanish_text
        FROM ml_issue_event_short_phrase_checkpoint_items item
        JOIN source_segments source ON source.id = item.segment_id
        WHERE item.run_id = ?
          AND item.checkpoint_allowed = 0
          AND item.block_reason LIKE 'suspicious_surface:%'
        ORDER BY item.relative_path, item.source_line_number, item.source_key
        """,
        (event_checkpoint_run_id,),
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        current_text = str(row.get("current_text") or "")
        if not has_visible_truncated_suffix(current_text):
            continue
        if not include_existing:
            exists = conn.execute(
                """
                SELECT 1
                FROM ml_issue_review_queue_items item
                WHERE item.segment_id = ?
                  AND item.agent_key = ?
                LIMIT 1
                """,
                (int(row["segment_id"]), AGENT_KEY),
            ).fetchone()
            decision_exists = conn.execute(
                """
                SELECT 1
                FROM ml_issue_review_decisions decision
                WHERE decision.segment_id = ?
                  AND decision.agent_key = ?
                  AND decision.valid = 1
                  AND decision.validation_status = 'accepted'
                LIMIT 1
                """,
                (int(row["segment_id"]), AGENT_KEY),
            ).fetchone()
            if exists or decision_exists:
                continue
        row["queue_bucket"] = "event_visible_truncated_gender_suffix"
        row["priority_score"] = 1200.0 + min(len(current_text), 160) / 10.0
        row["suggested_decision"] = "audit_event_gender_suffix_surface"
        candidates.append(row)
    return candidates


def ensure_review_tables(conn) -> None:
    # The project creates these tables elsewhere; this keeps the queue script robust in isolated runs.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_review_queue_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            agent_key TEXT NOT NULL,
            issue_family TEXT NOT NULL,
            queue_strategy TEXT NOT NULL,
            limit_count INTEGER NOT NULL DEFAULT 0,
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
            id INTEGER PRIMARY KEY,
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
            suggested_decision TEXT,
            evidence_text TEXT,
            evidence_json TEXT,
            english_text TEXT,
            spanish_text TEXT,
            confirmed_text TEXT,
            reviewer_decision TEXT,
            reviewer_notes TEXT,
            corrected_text TEXT,
            reviewed_at TEXT,
            created_at TEXT NOT NULL
        )
        """
    )


def insert_queue_run(
    conn,
    *,
    event_run: dict[str, Any],
    selected: list[dict[str, Any]],
    paths: tuple[Path, Path, Path, Path],
    limit: int,
) -> int:
    now = db.utc_now()
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    bucket_counts = Counter(row["queue_bucket"] for row in selected)
    cur = conn.execute(
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
            int(event_run["ledger_run_id"]),
            AGENT_KEY,
            ISSUE_FAMILY,
            QUEUE_STRATEGY,
            limit,
            len(selected),
            len(selected),
            json.dumps(dict(bucket_counts), ensure_ascii=False, sort_keys=True),
            str(txt_path),
            str(csv_path),
            str(jsonl_path),
            str(decisions_template_path),
            now,
            now,
            now,
        ),
    )
    return int(cur.lastrowid)


def insert_queue_items(
    conn,
    *,
    queue_run_id: int,
    event_run: dict[str, Any],
    rows: list[dict[str, Any]],
) -> None:
    now = db.utc_now()
    for row in rows:
        current_text = row.get("current_text") or ""
        evidence = {
            "event_checkpoint_run_id": int(event_run["id"]),
            "event_checkpoint_item_id": int(row["id"]),
            "dry_run_id": int(event_run["dry_run_id"]),
            "source_block_reason": row.get("block_reason"),
            "queue_strategy": QUEUE_STRATEGY,
            "char_count": int(row.get("char_count") or len(current_text)),
            "token_count": int(row.get("token_count") or 0),
        }
        cur = conn.execute(
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
                int(event_run["ledger_run_id"]),
                int(row["ledger_item_id"]),
                int(row["segment_id"]),
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                ISSUE_FAMILY,
                row.get("issue_kind") or "visible_truncated_gender_suffix",
                AGENT_KEY,
                row["queue_bucket"],
                row["priority_score"],
                row["suggested_decision"],
                current_text,
                json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                row.get("english_text"),
                row.get("spanish_text"),
                current_text,
                now,
            ),
        )
        row["queue_item_id"] = int(cur.lastrowid)


def trim(value: str | None, limit: int = 180) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def write_outputs(
    *,
    paths: tuple[Path, Path, Path, Path],
    queue_run_id: int,
    event_run: dict[str, Any],
    candidates: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    limit: int,
) -> None:
    txt_path, csv_path, jsonl_path, decisions_template_path = paths
    fields = [
        "queue_run_id",
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "block_reason",
        "char_count",
        "token_count",
        "priority_score",
        "suggested_decision",
        "evidence_text",
        "english_text",
        "spanish_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in selected:
            payload = {field: row.get(field) for field in fields}
            payload["queue_run_id"] = queue_run_id
            payload["evidence_text"] = row.get("current_text") or ""
            writer.writerow(payload)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            payload = {field: row.get(field) for field in fields}
            payload["queue_run_id"] = queue_run_id
            payload["evidence_text"] = row.get("current_text") or ""
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in selected:
            payload = {
                "queue_run_id": queue_run_id,
                "queue_item_id": row.get("queue_item_id"),
                "ledger_item_id": row.get("ledger_item_id"),
                "segment_id": row["segment_id"],
                "decision": "pending",
                "corrected_text": "",
                "notes": "",
                "decision_options": DECISION_OPTIONS,
                "review_hint": (
                    "Review visible truncated PT-BR gender suffixes from event short phrases. "
                    "Prefer neutral rewrites when subject gender is unknown; preserve all CK3 tokens."
                ),
                "evidence_text": row.get("current_text") or "",
                "english_text": row.get("english_text"),
                "spanish_text": row.get("spanish_text"),
                "block_reason": row.get("block_reason"),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Issue event gender suffix blocker queue",
        f"Rule version: {RULE_VERSION}",
        f"Queue run id: {queue_run_id}",
        f"Event checkpoint run id: {event_run['id']}",
        f"Agent key: {AGENT_KEY}",
        f"Issue family: {ISSUE_FAMILY}",
        f"Limit: {limit}",
        "",
        "Summary:",
        f"- candidates: {len(candidates):,}",
        f"- selected: {len(selected):,}",
        "",
        "Samples:",
    ]
    for row in selected[:80]:
        lines.append(
            (
                f"- segment={row['segment_id']} | {row['relative_path']}:{row.get('source_line_number') or '?'} "
                f"| {row['source_key']} | {trim(row.get('current_text'), 220)}"
            )
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Queue only: no source/output file reads, no confirmation updates and no output writes.",
            "- False positives inside CK3 references are excluded by masking [..] and $..$ before matching.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, event_checkpoint_run_id: int | None = None, limit: int = DEFAULT_LIMIT, include_existing: bool = False) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_review_tables(conn)
        selected_event_run_id = event_checkpoint_run_id or latest_event_checkpoint_run_id(conn)
        event_run = fetch_event_checkpoint_run(conn, run_id=selected_event_run_id)
        candidates = fetch_candidates(conn, event_checkpoint_run_id=selected_event_run_id, include_existing=include_existing)
        selected = candidates[: max(0, limit)]
        paths = report_paths(settings, selected_event_run_id)
        queue_run_id = insert_queue_run(conn, event_run=event_run, selected=selected, paths=paths, limit=limit)
        insert_queue_items(conn, queue_run_id=queue_run_id, event_run=event_run, rows=selected)
        conn.commit()

    write_outputs(paths=paths, queue_run_id=queue_run_id, event_run=event_run, candidates=candidates, selected=selected, limit=limit)
    txt_path, _, jsonl_path, _ = paths
    print("[issue_event_gender_suffix_blocker_queue] Queue generated")
    print(f"[issue_event_gender_suffix_blocker_queue] Queue run id: {queue_run_id}")
    print(f"[issue_event_gender_suffix_blocker_queue] Event checkpoint run id: {selected_event_run_id}")
    print(f"[issue_event_gender_suffix_blocker_queue] Candidates: {len(candidates):,}")
    print(f"[issue_event_gender_suffix_blocker_queue] Selected: {len(selected):,}")
    print(f"[issue_event_gender_suffix_blocker_queue] Report: {txt_path}")
    print(f"[issue_event_gender_suffix_blocker_queue] JSONL: {jsonl_path}")
    return {
        "queue_run_id": queue_run_id,
        "event_checkpoint_run_id": selected_event_run_id,
        "candidates": len(candidates),
        "selected": len(selected),
        "report_path": str(txt_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a focused queue for event short phrase truncated gender suffix blockers.")
    parser.add_argument("--event-checkpoint-run-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--include-existing", action="store_true")
    args = parser.parse_args()
    main(event_checkpoint_run_id=args.event_checkpoint_run_id, limit=args.limit, include_existing=args.include_existing)
