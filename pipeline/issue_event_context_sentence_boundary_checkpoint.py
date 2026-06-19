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


RULE_VERSION = "issue_event_context_sentence_boundary_checkpoint_v1"
POLICY_NAME = "event_context_sentence_boundary_strict_checkpoint"
POLICY_STATUS = "shadow_checkpoint"
CHECKPOINT_ACTION = "stage_event_context_sentence_boundary_strict_checkpoint"
POSITIVE_DECISIONS = {"composition_ready", "safe_short_label"}
INLINE_GENDER_FALLBACK_RE = re.compile(
    r"(?:o\(a\)|a\(o\)|meu/minha|minha/meu|seu/sua|sua/seu)",
    re.IGNORECASE,
)


def latest_queue_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_review_queue_runs
        WHERE finished_at IS NOT NULL
          AND agent_key = 'micro_event_context_composer'
          AND queue_strategy LIKE 'short_label_boundary_route_stratified_learning_queue:event_context_sentence%'
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished event context sentence boundary queue run found.")
    return int(row["id"])


def latest_decisions_for_queue(reports_dir: Path, queue_run_id: int) -> Path:
    pattern = "*issue_short_label_boundary_route_queue_event_context_sentence*event_surface_reviewed.jsonl"
    candidates: list[Path] = []
    for path in reports_dir.glob(pattern):
        try:
            first = next((line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()), "")
            if not first:
                continue
            payload = json.loads(first)
        except (json.JSONDecodeError, StopIteration, OSError):
            continue
        if int(payload.get("queue_run_id") or -1) == queue_run_id:
            candidates.append(path)
    if not candidates:
        raise RuntimeError(f"No assisted decisions JSONL found for queue run {queue_run_id}.")
    return max(candidates, key=lambda item: item.stat().st_mtime)


def report_paths(settings: dict[str, Any], queue_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_event_context_sentence_boundary_checkpoint_queue_{queue_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_event_context_sentence_boundary_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            queue_run_id INTEGER NOT NULL,
            decision_file_path TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            allowed_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            decision_counts_json TEXT,
            block_counts_json TEXT,
            bucket_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_event_context_sentence_boundary_checkpoint_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            queue_run_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            queue_bucket TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            checkpoint_action TEXT NOT NULL,
            block_reason TEXT,
            decision TEXT NOT NULL,
            corrected_text TEXT,
            current_text TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_event_context_sentence_boundary_checkpoint_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_event_context_sentence_boundary_items_run
        ON ml_issue_event_context_sentence_boundary_checkpoint_items(run_id, checkpoint_allowed, block_reason);

        CREATE INDEX IF NOT EXISTS idx_event_context_sentence_boundary_items_segment
        ON ml_issue_event_context_sentence_boundary_checkpoint_items(segment_id, run_id);
        """
    )


def load_decisions(path: Path) -> dict[int, dict[str, Any]]:
    decisions: dict[int, dict[str, Any]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        queue_item_id = int(payload.get("queue_item_id") or 0)
        if queue_item_id <= 0:
            raise ValueError(f"Missing queue_item_id in {path}:{line_number}")
        decisions[queue_item_id] = payload
    return decisions


def fetch_queue_rows(conn, *, queue_run_id: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT *
            FROM ml_issue_review_queue_items
            WHERE run_id = ?
            ORDER BY id
            """,
            (queue_run_id,),
        )
    ]


def is_debug_surface(row: dict[str, Any]) -> bool:
    bucket = str(row.get("queue_bucket") or "").lower()
    path = str(row.get("relative_path") or "").lower()
    key = str(row.get("source_key") or "").lower()
    return "debug_or_meta_key" in bucket or "debug" in path or key.startswith("debug")


def classify(row: dict[str, Any], decision: dict[str, Any] | None) -> tuple[int, str, str, str, str]:
    text = str(row.get("evidence_text") or row.get("confirmed_text") or "")
    if decision is None:
        return 0, "missing_assisted_decision", "", "", ""

    normalized = str(decision.get("decision") or "").strip()
    corrected_text = str(decision.get("corrected_text") or "").strip()
    notes = str(decision.get("notes") or "").strip()

    if normalized not in POSITIVE_DECISIONS:
        return 0, f"decision_not_positive:{normalized or 'empty'}", normalized, corrected_text, notes
    if is_debug_surface(row):
        return 0, "debug_or_meta_surface_not_production_training", normalized, corrected_text, notes
    if text.count('"') % 2 == 1:
        return 0, "unbalanced_quote_visible_surface", normalized, corrected_text, notes
    if INLINE_GENDER_FALLBACK_RE.search(text):
        return 0, "inline_gender_fallback_requires_specific_microagent", normalized, corrected_text, notes
    return 1, "", normalized, corrected_text, notes


def short(value: str | None, limit: int = 160) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def main(*, queue_run_id: int | None = None, decisions_path: str | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    reports_dir = db.project_path(settings["reports_dir"])
    started_at = db.utc_now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_queue_run_id = queue_run_id or latest_queue_run_id(conn)
        selected_decisions_path = (
            db.project_path(decisions_path)
            if decisions_path
            else latest_decisions_for_queue(reports_dir, selected_queue_run_id)
        )
        decisions = load_decisions(selected_decisions_path)
        rows = fetch_queue_rows(conn, queue_run_id=selected_queue_run_id)

        items: list[dict[str, Any]] = []
        decision_counts: Counter[str] = Counter()
        block_counts: Counter[str] = Counter()
        bucket_counts: Counter[str] = Counter()
        for row in rows:
            decision = decisions.get(int(row["id"]))
            allowed, block_reason, normalized, corrected_text, notes = classify(row, decision)
            decision_counts[normalized or "missing"] += 1
            if not allowed:
                block_counts[block_reason] += 1
            bucket_counts[f"{'allowed' if allowed else 'blocked'}|{row['queue_bucket']}"] += 1
            items.append(
                {
                    "queue_item_id": row["id"],
                    "ledger_item_id": row["ledger_item_id"],
                    "segment_id": row["segment_id"],
                    "relative_path": row["relative_path"],
                    "source_key": row["source_key"],
                    "source_line_number": row["source_line_number"],
                    "queue_bucket": row["queue_bucket"],
                    "checkpoint_allowed": allowed,
                    "block_reason": block_reason,
                    "decision": normalized or "missing",
                    "corrected_text": corrected_text,
                    "current_text": row.get("evidence_text") or row.get("confirmed_text") or "",
                    "notes": notes,
                }
            )

        allowed_count = sum(1 for item in items if item["checkpoint_allowed"])
        blocked_count = len(items) - allowed_count
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_queue_run_id)
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_event_context_sentence_boundary_checkpoint_runs (
                rule_version, policy_name, policy_status, queue_run_id, decision_file_path,
                candidate_count, allowed_count, blocked_count,
                decision_counts_json, block_counts_json, bucket_counts_json,
                report_path, csv_path, jsonl_path, started_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                POLICY_NAME,
                POLICY_STATUS,
                selected_queue_run_id,
                str(selected_decisions_path),
                len(items),
                allowed_count,
                blocked_count,
                json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(block_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(bucket_counts), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                started_at,
            ),
        )
        run_id = int(cursor.lastrowid)
        created_at = db.utc_now()
        conn.executemany(
            """
            INSERT INTO ml_issue_event_context_sentence_boundary_checkpoint_items (
                run_id, queue_run_id, queue_item_id, ledger_item_id, segment_id,
                relative_path, source_key, source_line_number, queue_bucket,
                checkpoint_allowed, checkpoint_action, block_reason, decision,
                corrected_text, current_text, notes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    selected_queue_run_id,
                    item["queue_item_id"],
                    item["ledger_item_id"],
                    item["segment_id"],
                    item["relative_path"],
                    item["source_key"],
                    item["source_line_number"],
                    item["queue_bucket"],
                    item["checkpoint_allowed"],
                    CHECKPOINT_ACTION,
                    item["block_reason"],
                    item["decision"],
                    item["corrected_text"],
                    item["current_text"],
                    item["notes"],
                    created_at,
                )
                for item in items
            ],
        )
        conn.execute(
            """
            UPDATE ml_issue_event_context_sentence_boundary_checkpoint_runs
            SET finished_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (db.utc_now(), db.utc_now(), run_id),
        )
        conn.commit()

    fields = [
        "checkpoint_run_id",
        "queue_run_id",
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "queue_bucket",
        "checkpoint_allowed",
        "block_reason",
        "decision",
        "corrected_text",
        "current_text",
        "notes",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in items:
            writer.writerow({"checkpoint_run_id": run_id, "queue_run_id": selected_queue_run_id, **item})
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for item in items:
            handle.write(
                json.dumps(
                    {"checkpoint_run_id": run_id, "queue_run_id": selected_queue_run_id, **item},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )

    lines = [
        "Event Context Sentence Boundary Checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint run id: {run_id}",
        f"Queue run id: {selected_queue_run_id}",
        f"Decisions: {selected_decisions_path}",
        f"Candidates: {len(items):,}",
        f"Allowed: {allowed_count:,}",
        f"Blocked: {blocked_count:,}",
        "",
        "Decision counts:",
        *[f"- {key}: {value:,}" for key, value in decision_counts.most_common()],
        "",
        "Block counts:",
        *[f"- {key}: {value:,}" for key, value in block_counts.most_common()],
        "",
        "Bucket counts:",
        *[f"- {key}: {value:,}" for key, value in bucket_counts.most_common()],
        "",
        "Blocked samples:",
    ]
    for item in [row for row in items if not row["checkpoint_allowed"]][:40]:
        lines.append(
            f"- segment={item['segment_id']} item={item['queue_item_id']} "
            f"| {item['block_reason']} | {item['source_key']} | {short(item['current_text'])}"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Shadow checkpoint only: no output writes, no confirmations, no lifecycle closure.",
            "- Allowed rows are evidence candidates; production consumption requires a separate governed bridge.",
            "",
            f"CSV: {csv_path}",
            f"JSONL: {jsonl_path}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("[issue_event_context_sentence_boundary_checkpoint] Checkpoint generated")
    print(f"[issue_event_context_sentence_boundary_checkpoint] Run id: {run_id}")
    print(f"[issue_event_context_sentence_boundary_checkpoint] Queue run id: {selected_queue_run_id}")
    print(f"[issue_event_context_sentence_boundary_checkpoint] Candidates: {len(items):,}")
    print(f"[issue_event_context_sentence_boundary_checkpoint] Allowed: {allowed_count:,}")
    print(f"[issue_event_context_sentence_boundary_checkpoint] Blocked: {blocked_count:,}")
    print(f"[issue_event_context_sentence_boundary_checkpoint] Report: {txt_path}")
    return {
        "checkpoint_run_id": run_id,
        "queue_run_id": selected_queue_run_id,
        "candidate_count": len(items),
        "allowed_count": allowed_count,
        "blocked_count": blocked_count,
        "report_path": str(txt_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Strict shadow checkpoint for event context sentence boundary route decisions.")
    parser.add_argument("--queue-run-id", type=int, default=None)
    parser.add_argument("--decisions-path", default=None)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id, decisions_path=args.decisions_path)
