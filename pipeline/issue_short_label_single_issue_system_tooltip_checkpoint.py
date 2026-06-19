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


RULE_VERSION = "issue_short_label_single_issue_system_tooltip_checkpoint_v1"
CHECKPOINT_NAME = "short_label_single_issue_system_tooltip_checkpoint_v1"
AGENT_KEY = "micro_short_label_style"
ISSUE_FAMILY = "short_label_style_microagent"
ISSUE_KIND = "short_or_compact_label_reopened"

TOKEN_RE = re.compile(
    r"\[[^\]]+\]|\$[^$]+\$|#\w+|@\w+!|Select_CString|SelectLocalization|Custom\(|Get[A-Z]|ROOT\.|SCOPE\."
)
SENTENCE_RE = re.compile(r"[.!?]")
BAD_FRAGMENTS = (
    "#bol",
    "#bold no#!",
    " no#!",
    "sonido",
    "the beneficiary",
    "probabilidad",
    "reduccion",
    "reducción",
    "interludio",
    "iranio",
    "seran",
    "serán",
    "debug key",
)


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_single_issue_system_tooltip_checkpoint"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_single_issue_system_tooltip_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            issue_family TEXT NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_blocked_count INTEGER NOT NULL DEFAULT 0,
            profile_counts_json TEXT,
            blocker_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_short_label_single_issue_system_tooltip_checkpoint_items (
            id INTEGER PRIMARY KEY,
            checkpoint_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            issue_family TEXT NOT NULL,
            issue_kind TEXT NOT NULL,
            profile TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            token_count INTEGER NOT NULL DEFAULT 0,
            word_count INTEGER NOT NULL DEFAULT 0,
            text_length INTEGER NOT NULL DEFAULT 0,
            evidence_text TEXT,
            current_confirmed_text_hash TEXT,
            current_output_text_hash TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_short_label_single_issue_system_tooltip_checkpoint_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_short_label_single_issue_system_tooltip_checkpoint_items_run
        ON ml_issue_short_label_single_issue_system_tooltip_checkpoint_items(checkpoint_run_id, checkpoint_allowed, profile);

        CREATE INDEX IF NOT EXISTS idx_short_label_single_issue_system_tooltip_checkpoint_items_segment
        ON ml_issue_short_label_single_issue_system_tooltip_checkpoint_items(segment_id);
        """
    )


def latest_finished_id(conn, table: str) -> int:
    row = conn.execute(
        f"""
        SELECT id
        FROM {table}
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No finished run found in {table}.")
    return int(row["id"])


def parse_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def text_stats(text: str) -> tuple[int, int, int]:
    return len(TOKEN_RE.findall(text)), len(re.findall(r"\S+", text)), len(text)


def classify(row: dict[str, Any]) -> tuple[str, str | None, int, int, int]:
    text = str(row.get("evidence_text") or "")
    lower = text.lower()
    token_count, word_count, text_length = text_stats(text)
    path = str(row.get("relative_path") or "")
    key = str(row.get("source_key") or "")

    if any(fragment in lower for fragment in BAD_FRAGMENTS):
        return "blocked_visible_residual_or_bad_literal", "visible_residual_or_bad_literal", token_count, word_count, text_length
    if '"' in text:
        return "blocked_dialogue_or_quote_surface", "dialogue_or_quote_surface", token_count, word_count, text_length
    if "#D DEBUG" in text or "DEBUG KEY" in text:
        return "blocked_debug_surface", "debug_surface", token_count, word_count, text_length
    if SENTENCE_RE.search(text):
        return "blocked_sentence_punctuation", "sentence_punctuation", token_count, word_count, text_length
    if word_count > 8:
        return "blocked_too_many_words", "too_many_words", token_count, word_count, text_length
    if token_count < 1:
        return "blocked_no_token", "no_token", token_count, word_count, text_length

    system_path = path.startswith("effects") or path.startswith("triggers")
    system_key = key.isupper() and any(marker in key for marker in ("TRIGGER", "EFFECT", "RESOURCE", "MISSING"))
    if not (system_path or system_key):
        return "blocked_not_system_tooltip_surface", "not_system_tooltip_surface", token_count, word_count, text_length

    return "system_tooltip_single_issue_clean", None, token_count, word_count, text_length


def fetch_rows(conn, *, ledger_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH counts AS (
            SELECT segment_id, COUNT(*) AS issue_count
            FROM ml_issue_ledger_items
            WHERE run_id = ?
            GROUP BY segment_id
        )
        SELECT
            item.id AS ledger_item_id,
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.source_line_number,
            item.issue_family,
            item.issue_kind,
            item.evidence_text,
            item.evidence_json,
            state.review_state,
            state.confirmed_matches_output,
            state.needs_output_apply,
            state.needs_reopen,
            state.final_state,
            state.state_group
        FROM ml_issue_ledger_items item
        JOIN counts c ON c.segment_id = item.segment_id
        JOIN segment_state_items state ON state.id = item.state_item_id
        WHERE item.run_id = ?
          AND c.issue_count = 1
          AND item.issue_family = ?
          AND item.issue_kind = ?
          AND item.status = 'open'
        ORDER BY item.relative_path, item.source_line_number, item.source_key, item.segment_id
        """,
        (ledger_run_id, ledger_run_id, ISSUE_FAMILY, ISSUE_KIND),
    ).fetchall()
    return [dict(row) for row in rows]


def write_reports(
    *,
    paths: tuple[Path, Path, Path],
    run_id: int,
    ledger_run_id: int,
    segment_state_run_id: int,
    rows: list[dict[str, Any]],
    profile_counts: Counter[str],
    blocker_counts: Counter[str],
) -> None:
    txt_path, csv_path, jsonl_path = paths
    allowed = [row for row in rows if int(row["checkpoint_allowed"]) == 1]
    blocked = [row for row in rows if int(row["checkpoint_allowed"]) == 0]

    lines = [
        "Short label single-issue system tooltip checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Ledger run id: {ledger_run_id}",
        f"Segment-state run id: {segment_state_run_id}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Allowed: {len(allowed):,}",
        f"- Blocked: {len(blocked):,}",
        "",
        "Profiles:",
        *[f"- {key}: {value:,}" for key, value in profile_counts.most_common()],
        "",
        "Blockers:",
        *[f"- {key}: {value:,}" for key, value in blocker_counts.most_common()],
        "",
        "Allowed samples:",
    ]
    for row in allowed[:20]:
        text = str(row.get("evidence_text") or "").replace("\n", "\\n").replace("\t", "\\t")
        if len(text) > 180:
            text = text[:177] + "..."
        lines.append(f"- {row['segment_id']} | {row['relative_path']}::{row['source_key']} | {text}")
    lines.extend(["", "Safety note:", "- Checkpoint only: no source/output writes, no confirmations, no production promotion."])
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    fieldnames = [
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "profile",
        "checkpoint_action",
        "checkpoint_allowed",
        "block_reason",
        "token_count",
        "word_count",
        "text_length",
        "evidence_text",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main(*, ledger_run_id: int | None = None, segment_state_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    paths = report_paths(settings)
    started_at = db.utc_now()

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_ledger_run_id = ledger_run_id or latest_finished_id(conn, "ml_issue_ledger_runs")
        selected_segment_state_run_id = segment_state_run_id or int(
            conn.execute(
                "SELECT segment_state_run_id FROM ml_issue_ledger_runs WHERE id = ?",
                (selected_ledger_run_id,),
            ).fetchone()["segment_state_run_id"]
        )

        candidates = fetch_rows(conn, ledger_run_id=selected_ledger_run_id)
        profile_counts: Counter[str] = Counter()
        blocker_counts: Counter[str] = Counter()
        item_rows: list[dict[str, Any]] = []

        for row in candidates:
            profile, block_reason, token_count, word_count, text_length = classify(row)
            profile_counts[profile] += 1
            allowed = block_reason is None
            if block_reason:
                blocker_counts[block_reason] += 1
            action = (
                "close_short_label_single_issue_system_tooltip"
                if allowed
                else "hold_short_label_single_issue_system_tooltip_review"
            )
            evidence = parse_json(row.get("evidence_json"))
            item_rows.append(
                {
                    **row,
                    "profile": profile,
                    "checkpoint_action": action,
                    "checkpoint_allowed": 1 if allowed else 0,
                    "block_reason": block_reason or "",
                    "token_count": token_count,
                    "word_count": word_count,
                    "text_length": text_length,
                    "current_confirmed_text_hash": str(evidence.get("confirmed_hash") or ""),
                    "current_output_text_hash": str(evidence.get("output_hash") or ""),
                }
            )

        run_id = conn.execute(
            """
            INSERT INTO ml_issue_short_label_single_issue_system_tooltip_checkpoint_runs (
                rule_version,
                checkpoint_name,
                checkpoint_status,
                agent_key,
                issue_family,
                ledger_run_id,
                segment_state_run_id,
                total_candidates,
                checkpoint_allowed_count,
                checkpoint_blocked_count,
                profile_counts_json,
                blocker_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                CHECKPOINT_NAME,
                "ready_for_shadow_lifecycle_policy",
                AGENT_KEY,
                ISSUE_FAMILY,
                selected_ledger_run_id,
                selected_segment_state_run_id,
                len(item_rows),
                sum(1 for item in item_rows if int(item["checkpoint_allowed"]) == 1),
                sum(1 for item in item_rows if int(item["checkpoint_allowed"]) == 0),
                json.dumps(dict(profile_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(blocker_counts), ensure_ascii=False, sort_keys=True),
                str(paths[0]),
                str(paths[1]),
                str(paths[2]),
                started_at,
                db.utc_now(),
                db.utc_now(),
            ),
        ).lastrowid

        now = db.utc_now()
        conn.executemany(
            """
            INSERT INTO ml_issue_short_label_single_issue_system_tooltip_checkpoint_items (
                checkpoint_run_id,
                ledger_run_id,
                segment_state_run_id,
                ledger_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                issue_family,
                issue_kind,
                profile,
                checkpoint_action,
                checkpoint_allowed,
                block_reason,
                token_count,
                word_count,
                text_length,
                evidence_text,
                current_confirmed_text_hash,
                current_output_text_hash,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    selected_ledger_run_id,
                    selected_segment_state_run_id,
                    int(item["ledger_item_id"]),
                    int(item["segment_id"]),
                    item["relative_path"],
                    item["source_key"],
                    item["source_line_number"],
                    item["issue_family"],
                    item["issue_kind"],
                    item["profile"],
                    item["checkpoint_action"],
                    int(item["checkpoint_allowed"]),
                    item["block_reason"],
                    int(item["token_count"]),
                    int(item["word_count"]),
                    int(item["text_length"]),
                    item.get("evidence_text") or "",
                    item.get("current_confirmed_text_hash") or "",
                    item.get("current_output_text_hash") or "",
                    now,
                )
                for item in item_rows
            ],
        )
        conn.commit()

    write_reports(
        paths=paths,
        run_id=int(run_id),
        ledger_run_id=int(selected_ledger_run_id),
        segment_state_run_id=int(selected_segment_state_run_id),
        rows=item_rows,
        profile_counts=profile_counts,
        blocker_counts=blocker_counts,
    )

    print("[issue_short_label_single_issue_system_tooltip_checkpoint] Checkpoint generated")
    print(f"[issue_short_label_single_issue_system_tooltip_checkpoint] Run id: {run_id}")
    print(f"[issue_short_label_single_issue_system_tooltip_checkpoint] Ledger run id: {selected_ledger_run_id}")
    print(f"[issue_short_label_single_issue_system_tooltip_checkpoint] Segment-state run id: {selected_segment_state_run_id}")
    print(f"[issue_short_label_single_issue_system_tooltip_checkpoint] Candidates: {len(item_rows):,}")
    print(f"[issue_short_label_single_issue_system_tooltip_checkpoint] Allowed: {sum(1 for item in item_rows if int(item['checkpoint_allowed']) == 1):,}")
    print(f"[issue_short_label_single_issue_system_tooltip_checkpoint] Blocked: {sum(1 for item in item_rows if int(item['checkpoint_allowed']) == 0):,}")
    print(f"[issue_short_label_single_issue_system_tooltip_checkpoint] Report: {paths[0]}")

    return {
        "run_id": int(run_id),
        "ledger_run_id": int(selected_ledger_run_id),
        "segment_state_run_id": int(selected_segment_state_run_id),
        "candidate_count": len(item_rows),
        "allowed_count": sum(1 for item in item_rows if int(item["checkpoint_allowed"]) == 1),
        "blocked_count": sum(1 for item in item_rows if int(item["checkpoint_allowed"]) == 0),
        "report_path": str(paths[0]),
        "csv_path": str(paths[1]),
        "jsonl_path": str(paths[2]),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Checkpoint clean single-issue short-label system tooltips.")
    parser.add_argument("--ledger-run-id", type=int, default=None)
    parser.add_argument("--segment-state-run-id", type=int, default=None)
    args = parser.parse_args()
    main(ledger_run_id=args.ledger_run_id, segment_state_run_id=args.segment_state_run_id)
