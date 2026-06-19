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


RULE_VERSION = "issue_short_label_pure_no_token_assisted_review_v1"
REVIEWER = "codex_short_label_pure_no_token_assisted"

EVENT_KEY_RE = re.compile(r"\.\d+\.[a-z](?:\.|$)", flags=re.IGNORECASE)
CUSTOM_CONTEXT_HINTS = (
    "RandomConflictDescriptor",
    "GreeterExchange",
    "Descriptor",
    "Custom",
    "Loc",
)
SPANISH_HINT_RE = re.compile(
    r"\b(de los|de las|por el|señor|señora|caballero|éxito|probabilidad de éxito|si pierdes)\b",
    flags=re.IGNORECASE,
)
ENGLISH_HINT_RE = re.compile(
    r"\b(the|and|of|for|with|without|success|failure|knight|war|house|court|trinket)\b",
    flags=re.IGNORECASE,
)
SENTENCE_PUNCT_RE = re.compile(r"[.!?]|^\"|\"$")


def latest_queue_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_pure_no_token_review_queue_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished pure no-token review queue runs found.")
    return int(row["id"])


def report_base(settings: dict[str, Any]) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    return reports_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_issue_short_label_pure_no_token_assisted_review"


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_pure_no_token_assisted_review_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            queue_run_id INTEGER NOT NULL,
            reviewer TEXT NOT NULL,
            reviewed_count INTEGER NOT NULL DEFAULT 0,
            safe_count INTEGER NOT NULL DEFAULT 0,
            needs_context_count INTEGER NOT NULL DEFAULT 0,
            residual_spanish_count INTEGER NOT NULL DEFAULT 0,
            semantic_error_count INTEGER NOT NULL DEFAULT 0,
            structure_error_count INTEGER NOT NULL DEFAULT 0,
            decision_counts_json TEXT,
            reason_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_short_label_pure_no_token_assisted_review_items (
            id INTEGER PRIMARY KEY,
            review_run_id INTEGER NOT NULL,
            queue_run_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            decision TEXT NOT NULL,
            reason TEXT NOT NULL,
            corrected_text TEXT,
            confidence_label TEXT NOT NULL,
            evidence_text TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(review_run_id) REFERENCES ml_issue_short_label_pure_no_token_assisted_review_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_short_label_pure_assisted_items_run
        ON ml_issue_short_label_pure_no_token_assisted_review_items(review_run_id, decision);

        CREATE INDEX IF NOT EXISTS idx_short_label_pure_assisted_items_segment
        ON ml_issue_short_label_pure_no_token_assisted_review_items(segment_id);
        """
    )


def fetch_queue_rows(conn, *, queue_run_id: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT *
            FROM ml_issue_short_label_pure_no_token_review_queue_items
            WHERE queue_run_id = ?
            ORDER BY id
            """,
            (queue_run_id,),
        )
    ]


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def classify(row: dict[str, Any]) -> tuple[str, str, str]:
    text = normalize_space(str(row.get("evidence_text") or ""))
    source_key = str(row.get("source_key") or "")
    relative_path = str(row.get("relative_path") or "")
    word_count = int(row.get("word_count") or 0)
    text_length = int(row.get("text_length") or len(text))

    if not text:
        return "needs_context", "empty_text", "boundary"
    if any(marker in text for marker in ("[", "]", "$", "{", "}")):
        return "structure_error", "unexpected_token_marker_in_pure_no_token_lane", "negative"
    if text.startswith('\\"') or text.endswith('\\"') or text.startswith('"') or text.endswith('"'):
        return "needs_context", "quoted_dialogue_or_fragment_requires_context", "boundary"
    if EVENT_KEY_RE.search(source_key):
        if source_key.lower().endswith(".t") and text_length <= 70 and not text.startswith(('"', '\\"')):
            return "safe_short_label", "event_title_short_no_token", "positive"
        return "needs_context", "event_option_or_dialogue_requires_context", "boundary"
    if any(hint in source_key for hint in CUSTOM_CONTEXT_HINTS) or "custom_localization/" in relative_path:
        return "needs_context", "custom_localization_fragment_requires_context", "boundary"
    if SPANISH_HINT_RE.search(text):
        return "residual_spanish", "spanish_surface_hint", "negative"
    if ENGLISH_HINT_RE.search(text):
        return "semantic_error", "english_surface_hint", "negative"
    if SENTENCE_PUNCT_RE.search(text):
        if text_length <= 38 and word_count <= 5 and not text.startswith('"'):
            return "safe_short_label", "short_title_like_sentence_surface", "positive"
        return "needs_context", "sentence_or_dialogue_surface_requires_context", "boundary"
    if word_count <= 5 and text_length <= 42:
        return "safe_short_label", "compact_nominal_label_no_token", "positive"
    if word_count <= 7 and text_length <= 56 and not re.search(r"\b(que|como|quando|porque|pois|mas|e)\b", text, flags=re.IGNORECASE):
        return "safe_short_label", "medium_nominal_label_no_token", "positive"
    return "needs_context", "long_or_clause_like_no_token_text", "boundary"


def main(*, queue_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = db.utc_now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_queue = queue_run_id or latest_queue_run_id(conn)
        rows = fetch_queue_rows(conn, queue_run_id=selected_queue)
        if not rows:
            raise RuntimeError(f"No queue items found for queue run {selected_queue}.")

        reviewed: list[dict[str, Any]] = []
        decision_counts: Counter[str] = Counter()
        reason_counts: Counter[str] = Counter()
        confidence_counts: Counter[str] = Counter()
        for row in rows:
            decision, reason, confidence = classify(row)
            decision_counts[decision] += 1
            reason_counts[reason] += 1
            confidence_counts[confidence] += 1
            reviewed.append(
                {
                    "queue_run_id": selected_queue,
                    "queue_item_id": row["id"],
                    "ledger_item_id": row["ledger_item_id"],
                    "segment_id": row["segment_id"],
                    "relative_path": row["relative_path"],
                    "source_key": row["source_key"],
                    "source_line_number": row["source_line_number"],
                    "decision": decision,
                    "reason": reason,
                    "corrected_text": "",
                    "confidence_label": confidence,
                    "evidence_text": row["evidence_text"],
                    "notes": f"{RULE_VERSION}; assisted review only; no output authority",
                }
            )

        cursor = conn.execute(
            """
            INSERT INTO ml_issue_short_label_pure_no_token_assisted_review_runs (
                rule_version, queue_run_id, reviewer, reviewed_count,
                safe_count, needs_context_count, residual_spanish_count,
                semantic_error_count, structure_error_count,
                decision_counts_json, reason_counts_json,
                started_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_queue,
                REVIEWER,
                len(reviewed),
                decision_counts["safe_short_label"],
                decision_counts["needs_context"],
                decision_counts["residual_spanish"],
                decision_counts["semantic_error"],
                decision_counts["structure_error"],
                json.dumps(dict(decision_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(reason_counts), ensure_ascii=False, sort_keys=True),
                started_at,
                started_at,
            ),
        )
        review_run_id = int(cursor.lastrowid)
        created_at = db.utc_now()
        conn.executemany(
            """
            INSERT INTO ml_issue_short_label_pure_no_token_assisted_review_items (
                review_run_id, queue_run_id, queue_item_id, ledger_item_id,
                segment_id, relative_path, source_key, source_line_number,
                decision, reason, corrected_text, confidence_label,
                evidence_text, notes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    review_run_id,
                    row["queue_run_id"],
                    row["queue_item_id"],
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["decision"],
                    row["reason"],
                    row["corrected_text"],
                    row["confidence_label"],
                    row["evidence_text"],
                    row["notes"],
                    created_at,
                )
                for row in reviewed
            ],
        )

        base = report_base(settings)
        txt_path = base.with_suffix(".txt")
        csv_path = base.with_suffix(".csv")
        jsonl_path = base.with_suffix(".jsonl")
        conn.execute(
            """
            UPDATE ml_issue_short_label_pure_no_token_assisted_review_runs
            SET report_path = ?, csv_path = ?, jsonl_path = ?,
                finished_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (str(txt_path), str(csv_path), str(jsonl_path), db.utc_now(), db.utc_now(), review_run_id),
        )
        conn.commit()

    fields = [
        "review_run_id",
        "queue_run_id",
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "decision",
        "reason",
        "confidence_label",
        "evidence_text",
        "notes",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in reviewed:
            writer.writerow({"review_run_id": review_run_id, **{key: row.get(key) for key in fields if key != "review_run_id"}})
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in reviewed:
            handle.write(json.dumps({"review_run_id": review_run_id, **row}, ensure_ascii=False, sort_keys=True) + "\n")

    samples_by_decision: dict[str, list[str]] = {}
    for row in reviewed:
        samples_by_decision.setdefault(row["decision"], [])
        if len(samples_by_decision[row["decision"]]) < 12:
            samples_by_decision[row["decision"]].append(
                f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']} | "
                f"{row['reason']} | {row['evidence_text']}"
            )

    safe_rate = decision_counts["safe_short_label"] / len(reviewed) if reviewed else 0.0
    lines = [
        "Short Label Pure No-Token Assisted Review",
        f"Rule version: {RULE_VERSION}",
        f"Review run id: {review_run_id}",
        f"Queue run id: {selected_queue}",
        f"Rows reviewed: {len(reviewed):,}",
        f"Safe short-label rate: {safe_rate:.2%}",
        "",
        "Decision counts:",
        *[f"- {key}: {value:,}" for key, value in decision_counts.most_common()],
        "",
        "Confidence counts:",
        *[f"- {key}: {value:,}" for key, value in confidence_counts.most_common()],
        "",
        "Reason counts:",
        *[f"- {key}: {value:,}" for key, value in reason_counts.most_common()],
        "",
        "Interpretation:",
        "- This is assisted review evidence, not production authority.",
        "- The pure no-token lane is not pure enough to promote wholesale if many rows require context.",
        "- Safe rows can seed a narrower nominal-label checkpoint; boundary rows should split into event-title, event-option, custom-localization-fragment and nominal-label sublanes.",
        "",
        "Samples:",
    ]
    for decision, samples in samples_by_decision.items():
        lines.append(f"{decision}:")
        lines.extend(samples)
    lines.extend(["", f"CSV: {csv_path}", f"JSONL: {jsonl_path}"])
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("[issue_short_label_pure_no_token_assisted_review] Review generated")
    print(f"[issue_short_label_pure_no_token_assisted_review] Review run id: {review_run_id}")
    print(f"[issue_short_label_pure_no_token_assisted_review] Queue run id: {selected_queue}")
    print(f"[issue_short_label_pure_no_token_assisted_review] Rows: {len(reviewed):,}")
    print(f"[issue_short_label_pure_no_token_assisted_review] Safe: {decision_counts['safe_short_label']:,}")
    print(f"[issue_short_label_pure_no_token_assisted_review] Needs context: {decision_counts['needs_context']:,}")
    print(f"[issue_short_label_pure_no_token_assisted_review] Report: {txt_path}")
    return {
        "review_run_id": review_run_id,
        "queue_run_id": selected_queue,
        "reviewed_count": len(reviewed),
        "decision_counts": dict(decision_counts),
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Conservative assisted review for pure no-token short-label queue.")
    parser.add_argument("--queue-run-id", type=int, default=None)
    args = parser.parse_args()
    main(queue_run_id=args.queue_run_id)
