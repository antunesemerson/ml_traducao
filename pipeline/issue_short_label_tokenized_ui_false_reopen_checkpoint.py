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
from apply_segment_state_updates import short
from issue_review_assisted_draft import english_hits, has_actual_mojibake, spanish_hits
from segment_state_snapshot import canonical_localization_text, protected_tokens_signature


RULE_VERSION = "issue_short_label_tokenized_ui_false_reopen_checkpoint_v1"
CHECKPOINT_NAME = "short_label_tokenized_ui_false_reopen_checkpoint_v1"
CHECKPOINT_ACTION = "cover_short_label_tokenized_ui_false_reopen"
AGENT_KEY = "micro_short_label_tokenized_ui"
ISSUE_FAMILY = "short_label_style_microagent"
ISSUE_KIND = "short_or_compact_label_reopened"

VISIBLE_BAD_PATTERN = re.compile(
    r"\b(?:"
    r"red[uú]cci[oó]n|probabilidad|[eé]xito|ser[aá]n|si pierdes|unci[oó]n|ligeramente|"
    r"de tipo|"
    r"interludio|sonido|trabajo|romancear|levies|county|duchy|kingdom|empire|"
    r"increase|decrease|not|the beneficiary|debug key"
    r")\b|#bold\s+no#!|#bold\s+not#!|#bol(?!d)\b",
    re.IGNORECASE,
)
TRAILING_PREPOSITION = re.compile(r"\b(?:em|de|da|do|para|por|com)\s*$", re.IGNORECASE)


def latest_finished_run_id(conn, table_name: str) -> int:
    row = conn.execute(
        f"""
        SELECT id
        FROM {table_name}
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No finished run found in {table_name}.")
    return int(row["id"])


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_short_label_tokenized_ui_false_reopen_checkpoint"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_tokenized_ui_false_reopen_checkpoint_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            lane_counts_json TEXT,
            blocker_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_tokenized_ui_false_reopen_checkpoint_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            lane TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            open_issue_count INTEGER NOT NULL DEFAULT 0,
            token_count INTEGER NOT NULL DEFAULT 0,
            word_count INTEGER NOT NULL DEFAULT 0,
            text_length INTEGER NOT NULL DEFAULT 0,
            current_text TEXT,
            current_confirmed_text_hash TEXT,
            current_output_text_hash TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_short_label_tokenized_ui_false_reopen_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def stable_hash(value: str | None) -> str:
    import hashlib

    return hashlib.sha1((value or "").encode("utf-8")).hexdigest()


def fetch_rows(conn, *, ledger_run_id: int, segment_state_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH issue_counts AS (
            SELECT
                segment_id,
                COUNT(*) AS open_issue_count,
                SUM(CASE WHEN lower(COALESCE(issue_severity, '')) IN ('high', 'error', 'critical') THEN 1 ELSE 0 END) AS high_issue_count,
                SUM(CASE WHEN COALESCE(token_status, '') <> 'ok' THEN 1 ELSE 0 END) AS token_not_ok_count
            FROM ml_issue_ledger_items
            WHERE run_id = ?
              AND status = 'open'
            GROUP BY segment_id
        )
        SELECT
            item.*,
            state.final_state AS current_final_state,
            state.state_group AS current_state_group,
            state.review_state AS current_review_state,
            state.apply_state AS current_apply_state,
            COALESCE(state.needs_output_apply, 0) AS current_needs_output_apply,
            COALESCE(state.confirmed_matches_output, 0) AS current_confirmed_matches_output,
            COALESCE(state.locked, 0) AS current_state_locked,
            confirmation.confirmed_text AS current_confirmed_text,
            COALESCE(confirmation.locked, 0) AS current_confirmation_locked,
            output.portuguese_text AS current_output_text,
            COALESCE(issue_counts.open_issue_count, 0) AS open_issue_count,
            COALESCE(issue_counts.high_issue_count, 0) AS high_issue_count,
            COALESCE(issue_counts.token_not_ok_count, 0) AS token_not_ok_count
        FROM ml_issue_ledger_items item
        JOIN issue_counts ON issue_counts.segment_id = item.segment_id
        LEFT JOIN segment_state_items state
          ON state.run_id = ?
         AND state.segment_id = item.segment_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.segment_id = item.segment_id
        LEFT JOIN output_segments output
          ON output.segment_id = item.segment_id
        WHERE item.run_id = ?
          AND item.issue_family = ?
          AND item.issue_kind = ?
          AND item.status = 'open'
        ORDER BY item.relative_path, item.source_line_number, item.source_key, item.id
        """,
        (ledger_run_id, segment_state_run_id, ledger_run_id, ISSUE_FAMILY, ISSUE_KIND),
    ).fetchall()
    return [dict(row) for row in rows]


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def token_count(text: str) -> int:
    token_patterns = (
        r"\[[^\]]+\]",
        r"\$[^$]+\$",
        r"@[A-Za-z0-9_]+!",
        r"#[A-Za-z0-9_]+",
        r"#!",
        r"[A-Za-z0-9_.]+\(.*?\)",
    )
    return sum(len(re.findall(pattern, text or "")) for pattern in token_patterns)


def lane_for(row: dict[str, Any], text: str) -> str:
    key = str(row.get("source_key") or "").lower()
    path = str(row.get("relative_path") or "").lower()
    if text.startswith("@warning_icon!") or "#x" in text.lower() or "#alert" in text.lower():
        return "warning_or_blocker"
    if "$effect_list_bullet$" in text.lower():
        return "effect_list_bullet"
    if path.startswith("triggers/") or path == "effects_l_spanish.yml":
        return "system_trigger_effect"
    if key.endswith("_tt") or "tooltip" in key or key.endswith("_tooltip"):
        return "tooltip_label"
    if key.endswith("_desc") or "_desc_" in key:
        return "description_like_hold"
    if "[" in text and "]" in text and len(text) <= 120:
        return "compact_tokenized_label"
    return "not_owned_surface"


def visible_text_block(text: str) -> str:
    if not text.strip():
        return "missing_current_text"
    if has_actual_mojibake(text):
        return "visible_mojibake_or_encoding_marker"
    spanish = spanish_hits(text)
    if spanish:
        return "visible_spanish_residual:" + ",".join(spanish[:3])
    english = english_hits(text)
    if english:
        return "visible_english_residual:" + ",".join(english[:3])
    if VISIBLE_BAD_PATTERN.search(text):
        return "visible_bad_literal_or_foreign_term"
    if TRAILING_PREPOSITION.search(text.strip()):
        return "trailing_preposition_context_required"
    if '"' in text or '\\"' in text:
        return "quote_or_dialogue_surface"
    return ""


def classify(row: dict[str, Any]) -> tuple[int, str, str, int, int, int]:
    text = row.get("current_confirmed_text") or ""
    output = row.get("current_output_text") or ""
    words = word_count(text)
    tokens = token_count(text)
    length = len(text)
    lane = lane_for(row, text)

    if int(row.get("open_issue_count") or 0) != 1:
        return 0, "other_open_issues_remain", lane, tokens, words, length
    if int(row.get("high_issue_count") or 0) != 0:
        return 0, "high_issue_present", lane, tokens, words, length
    if int(row.get("token_not_ok_count") or 0) != 0:
        return 0, "token_status_not_ok", lane, tokens, words, length
    if row.get("current_state_group") != "pending":
        return 0, "state_not_pending", lane, tokens, words, length
    if str(row.get("current_final_state") or "") != "reopen_auto_confirmed_autofix":
        return 0, "state_not_reopen_auto_confirmed_autofix", lane, tokens, words, length
    if row.get("current_review_state") != "auto_confirmed":
        return 0, "review_state_not_auto_confirmed", lane, tokens, words, length
    if int(row.get("current_needs_output_apply") or 0) != 0:
        return 0, "needs_output_apply", lane, tokens, words, length
    if int(row.get("current_confirmed_matches_output") or 0) != 1:
        return 0, "confirmed_not_marked_aligned_with_output", lane, tokens, words, length
    if int(row.get("current_state_locked") or 0) or int(row.get("current_confirmation_locked") or 0):
        return 0, "human_locked_or_confirmed", lane, tokens, words, length
    if not text or not output:
        return 0, "missing_confirmation_or_output", lane, tokens, words, length
    if canonical_localization_text(text) != canonical_localization_text(output):
        return 0, "confirmed_output_canonical_mismatch", lane, tokens, words, length
    if protected_tokens_signature(text) != protected_tokens_signature(output):
        return 0, "token_signature_mismatch", lane, tokens, words, length

    text_block = visible_text_block(text)
    if text_block:
        return 0, text_block, lane, tokens, words, length

    if lane == "description_like_hold":
        return 0, "description_like_surface_requires_context", lane, tokens, words, length
    if lane == "not_owned_surface":
        return 0, "lane_not_owned", lane, tokens, words, length
    if lane in {"warning_or_blocker", "effect_list_bullet", "system_trigger_effect", "tooltip_label", "compact_tokenized_label"}:
        if length <= 140 and words <= 22 and tokens <= 14:
            return 1, "", lane, tokens, words, length
        return 0, "tokenized_label_too_dense", lane, tokens, words, length
    return 0, "lane_not_allowed", lane, tokens, words, length


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    ledger_run_id: int,
    segment_state_run_id: int,
    rows: list[dict[str, Any]],
) -> None:
    fields = [
        "checkpoint_item_id",
        "checkpoint_run_id",
        "ledger_run_id",
        "segment_state_run_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "lane",
        "checkpoint_action",
        "checkpoint_allowed",
        "block_reason",
        "open_issue_count",
        "token_count",
        "word_count",
        "text_length",
        "current_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: row.get(field) for field in fields} for row in rows)

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps({field: row.get(field) for field in fields}, ensure_ascii=False, sort_keys=True) + "\n")

    allowed = [row for row in rows if int(row["checkpoint_allowed"]) == 1]
    blocked = [row for row in rows if int(row["checkpoint_allowed"]) == 0]
    blocker_counts = Counter(row["block_reason"] or "checkpoint_allowed" for row in rows)
    lane_counts = Counter(row["lane"] for row in rows)
    allowed_lane_counts = Counter(row["lane"] for row in allowed)

    lines = [
        "Short Label Tokenized UI False-Reopen Checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint name: {CHECKPOINT_NAME}",
        f"Checkpoint run id: {checkpoint_run_id}",
        "Production release allowed: 0",
        f"Ledger run id: {ledger_run_id}",
        f"Segment-state run id: {segment_state_run_id}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Allowed: {len(allowed):,}",
        f"- Blocked: {len(blocked):,}",
        "",
        "Allowed by lane:",
        *[f"- {key}: {value:,}" for key, value in allowed_lane_counts.most_common()],
        "",
        "Candidates by lane:",
        *[f"- {key}: {value:,}" for key, value in lane_counts.most_common()],
        "",
        "Checkpoint counts:",
        *[f"- {key}: {value:,}" for key, value in blocker_counts.most_common()],
        "",
        "Allowed samples:",
    ]
    for row in allowed[:45]:
        lines.append(
            f"- segment={row['segment_id']} | {row['lane']} | {row['relative_path']}::{row['source_key']} | {short(row.get('current_text'), 220)}"
        )
    lines.extend(["", "Blocked samples:"])
    for row in blocked[:45]:
        lines.append(
            f"- {row['block_reason']} | segment={row['segment_id']} | {row['lane']} | "
            f"{row['relative_path']}::{row['source_key']} | {short(row.get('current_text'), 220)}"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Checkpoint only: no source/output writes, no confirmations, no segment-state closure.",
            "- Allowed means tokenized short-label false-reopen evidence, not production authority by itself.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, ledger_run_id: int | None = None, segment_state_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    txt_path, csv_path, jsonl_path = report_paths(settings)
    started_at = datetime.now().isoformat(timespec="seconds")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_ledger_run_id = ledger_run_id or latest_finished_run_id(conn, "ml_issue_ledger_runs")
        selected_segment_state_run_id = segment_state_run_id or latest_finished_run_id(conn, "segment_state_runs")
        rows = fetch_rows(conn, ledger_run_id=selected_ledger_run_id, segment_state_run_id=selected_segment_state_run_id)
        now = db.utc_now()

        for row in rows:
            allowed, block_reason, lane, tokens, words, length = classify(row)
            row["lane"] = lane
            row["checkpoint_action"] = CHECKPOINT_ACTION
            row["checkpoint_allowed"] = allowed
            row["block_reason"] = block_reason
            row["token_count"] = tokens
            row["word_count"] = words
            row["text_length"] = length
            row["current_text"] = row.get("current_confirmed_text") or ""
            row["current_confirmed_text_hash"] = stable_hash(row.get("current_confirmed_text"))
            row["current_output_text_hash"] = stable_hash(row.get("current_output_text"))

        allowed_count = sum(1 for row in rows if int(row["checkpoint_allowed"]) == 1)
        blocked_count = len(rows) - allowed_count
        checkpoint_status = "ready_for_shadow_lifecycle" if allowed_count else "blocked_by_checkpoint_guard"
        lane_counts = Counter(row["lane"] for row in rows if int(row["checkpoint_allowed"]) == 1)
        blocker_counts = Counter(row["block_reason"] or "checkpoint_allowed" for row in rows)

        checkpoint_run_id = int(
            conn.execute(
                """
                INSERT INTO ml_issue_short_label_tokenized_ui_false_reopen_checkpoint_runs (
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
                    lane_counts_json,
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
                    checkpoint_status,
                    AGENT_KEY,
                    ISSUE_FAMILY,
                    selected_ledger_run_id,
                    selected_segment_state_run_id,
                    len(rows),
                    allowed_count,
                    blocked_count,
                    json.dumps(dict(lane_counts), ensure_ascii=False, sort_keys=True),
                    json.dumps(dict(blocker_counts), ensure_ascii=False, sort_keys=True),
                    str(txt_path),
                    str(csv_path),
                    str(jsonl_path),
                    started_at,
                    now,
                    now,
                ),
            ).lastrowid
        )

        for row in rows:
            checkpoint_item_id = int(
                conn.execute(
                    """
                    INSERT INTO ml_issue_short_label_tokenized_ui_false_reopen_checkpoint_items (
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
                        lane,
                        checkpoint_action,
                        checkpoint_allowed,
                        block_reason,
                        open_issue_count,
                        token_count,
                        word_count,
                        text_length,
                        current_text,
                        current_confirmed_text_hash,
                        current_output_text_hash,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        checkpoint_run_id,
                        selected_ledger_run_id,
                        selected_segment_state_run_id,
                        row["id"],
                        row["segment_id"],
                        row["relative_path"],
                        row["source_key"],
                        row["source_line_number"],
                        row["issue_family"],
                        row["issue_kind"],
                        row["lane"],
                        row["checkpoint_action"],
                        int(row["checkpoint_allowed"]),
                        row["block_reason"],
                        int(row.get("open_issue_count") or 0),
                        int(row.get("token_count") or 0),
                        int(row.get("word_count") or 0),
                        int(row.get("text_length") or 0),
                        row.get("current_text"),
                        row["current_confirmed_text_hash"],
                        row["current_output_text_hash"],
                        now,
                    ),
                ).lastrowid
            )
            row["checkpoint_item_id"] = checkpoint_item_id
            row["checkpoint_run_id"] = checkpoint_run_id
            row["ledger_item_id"] = int(row["id"])
            row["ledger_run_id"] = selected_ledger_run_id
            row["segment_state_run_id"] = selected_segment_state_run_id

        conn.commit()

    write_outputs(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        checkpoint_run_id=checkpoint_run_id,
        ledger_run_id=selected_ledger_run_id,
        segment_state_run_id=selected_segment_state_run_id,
        rows=rows,
    )
    print("[short_label_tokenized_ui_checkpoint] Checkpoint generated")
    print(f"[short_label_tokenized_ui_checkpoint] Run id: {checkpoint_run_id}")
    print(f"[short_label_tokenized_ui_checkpoint] Ledger run id: {selected_ledger_run_id}")
    print(f"[short_label_tokenized_ui_checkpoint] Segment-state run id: {selected_segment_state_run_id}")
    print(f"[short_label_tokenized_ui_checkpoint] Candidates: {len(rows):,}")
    print(f"[short_label_tokenized_ui_checkpoint] Allowed: {allowed_count:,}")
    print(f"[short_label_tokenized_ui_checkpoint] Blocked: {blocked_count:,}")
    print(f"[short_label_tokenized_ui_checkpoint] Report: {txt_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "ledger_run_id": selected_ledger_run_id,
        "segment_state_run_id": selected_segment_state_run_id,
        "candidates": len(rows),
        "allowed": allowed_count,
        "blocked": blocked_count,
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Checkpoint tokenized UI short-label false reopen candidates.")
    parser.add_argument("--ledger-run-id", type=int)
    parser.add_argument("--segment-state-run-id", type=int)
    args = parser.parse_args()
    main(ledger_run_id=args.ledger_run_id, segment_state_run_id=args.segment_state_run_id)
