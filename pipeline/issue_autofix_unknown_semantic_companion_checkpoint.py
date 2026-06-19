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
from issue_autofix_unknown_domain_guarded_checkpoint import (
    ALLOWED_CLUSTERS,
    CHECKPOINT_ACTION as BASE_CHECKPOINT_ACTION,
    stable_hash,
    subpolicy_for,
    surface_cluster,
    text_metrics,
    visible_text_block,
)
from segment_state_snapshot import canonical_localization_text, protected_tokens_signature


RULE_VERSION = "issue_autofix_unknown_semantic_companion_checkpoint_v3_rule_effect_second"
CHECKPOINT_NAME = "autofix_unknown_semantic_companion_checkpoint_v3_rule_effect_second"
CHECKPOINT_ACTION = "cover_autofix_unknown_semantic_companion_false_reopen"
AGENT_KEY = "micro_autofix_unknown_semantic_companion"
ISSUE_FAMILY = "autofix_unknown_microagent"
COMPANION_FAMILY = "semantic_review_router"
COMPANION_KIND = "needs_human_or_semantic_conflict"
ALLOWED_COMPANION_SUBPOLICIES = {
    "building_description_guarded",
    "rule_effect_modifier_guarded",
}
COMPANION_VISIBLE_BAD_PATTERN = re.compile(
    r"\bthis\s+side\b"
    r"|\bwill\b"
    r"|\benemy\b"
    r"|\badvancing\b"
    r"|\bd\u00eaem\b"
    r"|\bv\u00eaem\b"
    r"|\bum\s+passagem\b"
    r"|\bscript\s+antigo\b"
    r"|\beles\s+est\u00e3o\b"
    r"|\bexcitou\b"
    r"|\bconduzindo\b"
    r"|\blascivo\b"
    r"|\bardentes\b"
    r"|\bsem\s+um\s+grande\s+envio\b"
    r"|\bdobr\u00e1-lo\b"
    r"|\ba\s+partir\s+de\s+origens\s+humildes\b"
    r"|\bsua\s+renome\b"
    r"|\biberia\b",
    re.IGNORECASE,
)


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
    base = reports_dir / f"{stamp}_issue_autofix_unknown_semantic_companion_checkpoint"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_autofix_unknown_semantic_companion_checkpoint_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_version TEXT NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_blocked_count INTEGER NOT NULL DEFAULT 0,
            subpolicy_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_autofix_unknown_semantic_companion_checkpoint_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checkpoint_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            autofix_ledger_item_id INTEGER NOT NULL,
            semantic_ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            cluster TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            open_issue_count INTEGER NOT NULL DEFAULT 0,
            token_count INTEGER NOT NULL DEFAULT 0,
            word_count INTEGER NOT NULL DEFAULT 0,
            text_length INTEGER NOT NULL DEFAULT 0,
            evidence_text TEXT,
            current_confirmed_text_hash TEXT,
            current_output_text_hash TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(checkpoint_run_id) REFERENCES ml_issue_autofix_unknown_semantic_companion_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def fetch_rows(conn, *, ledger_run_id: int, segment_state_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH issue_counts AS (
            SELECT
                segment_id,
                COUNT(*) AS open_issue_count,
                SUM(CASE WHEN issue_family = ? THEN 1 ELSE 0 END) AS autofix_unknown_count,
                SUM(CASE WHEN issue_family = ? AND issue_kind = ? THEN 1 ELSE 0 END) AS semantic_companion_count,
                SUM(CASE WHEN lower(COALESCE(issue_severity, '')) IN ('high', 'error', 'critical') THEN 1 ELSE 0 END) AS high_issue_count,
                SUM(CASE WHEN COALESCE(token_status, '') <> 'ok' THEN 1 ELSE 0 END) AS token_not_ok_count,
                MIN(CASE WHEN issue_family = ? THEN id ELSE NULL END) AS autofix_ledger_item_id,
                MIN(CASE WHEN issue_family = ? AND issue_kind = ? THEN id ELSE NULL END) AS semantic_ledger_item_id
            FROM ml_issue_ledger_items
            WHERE run_id = ?
              AND status = 'open'
            GROUP BY segment_id
        )
        SELECT
            item.*,
            issue_counts.semantic_ledger_item_id,
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
            COALESCE(issue_counts.autofix_unknown_count, 0) AS autofix_unknown_count,
            COALESCE(issue_counts.semantic_companion_count, 0) AS semantic_companion_count,
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
          AND item.id = issue_counts.autofix_ledger_item_id
          AND issue_counts.open_issue_count = 2
          AND issue_counts.autofix_unknown_count = 1
          AND issue_counts.semantic_companion_count = 1
        ORDER BY item.relative_path, item.source_line_number, item.source_key, item.id
        """,
        (
            ISSUE_FAMILY,
            COMPANION_FAMILY,
            COMPANION_KIND,
            ISSUE_FAMILY,
            COMPANION_FAMILY,
            COMPANION_KIND,
            ledger_run_id,
            segment_state_run_id,
            ledger_run_id,
        ),
    ).fetchall()
    return [dict(row) for row in rows]


def classify(row: dict[str, Any]) -> tuple[int, str, str, int, int, int]:
    cluster = surface_cluster(row)
    tokens, words, length = text_metrics(row)
    subpolicy = subpolicy_for(row, cluster)
    text = row.get("current_confirmed_text") or ""
    output = row.get("current_output_text") or ""

    if cluster not in ALLOWED_CLUSTERS:
        return 0, "cluster_not_allowed", subpolicy, tokens, words, length
    if int(row.get("open_issue_count") or 0) != 2:
        return 0, "open_issue_count_not_two", subpolicy, tokens, words, length
    if int(row.get("autofix_unknown_count") or 0) != 1:
        return 0, "autofix_unknown_count_not_one", subpolicy, tokens, words, length
    if int(row.get("semantic_companion_count") or 0) != 1:
        return 0, "semantic_companion_count_not_one", subpolicy, tokens, words, length
    if int(row.get("high_issue_count") or 0) != 0:
        return 0, "high_issue_present", subpolicy, tokens, words, length
    if int(row.get("token_not_ok_count") or 0) != 0:
        return 0, "token_status_not_ok", subpolicy, tokens, words, length
    if row.get("current_state_group") != "pending":
        return 0, "state_not_pending", subpolicy, tokens, words, length
    if str(row.get("current_final_state") or "") != "reopen_auto_confirmed_autofix":
        return 0, "state_not_reopen_auto_confirmed_autofix", subpolicy, tokens, words, length
    if row.get("current_review_state") != "auto_confirmed":
        return 0, "review_state_not_auto_confirmed", subpolicy, tokens, words, length
    if int(row.get("current_needs_output_apply") or 0) != 0:
        return 0, "needs_output_apply", subpolicy, tokens, words, length
    if int(row.get("current_confirmed_matches_output") or 0) != 1:
        return 0, "confirmed_not_marked_aligned_with_output", subpolicy, tokens, words, length
    if int(row.get("current_state_locked") or 0) or int(row.get("current_confirmation_locked") or 0):
        return 0, "human_locked_or_confirmed", subpolicy, tokens, words, length
    if not text or not output:
        return 0, "missing_confirmation_or_output", subpolicy, tokens, words, length
    if canonical_localization_text(text) != canonical_localization_text(output):
        return 0, "confirmed_output_canonical_mismatch", subpolicy, tokens, words, length
    if protected_tokens_signature(text) != protected_tokens_signature(output):
        return 0, "token_signature_mismatch", subpolicy, tokens, words, length

    text_block = visible_text_block(text)
    if text_block:
        return 0, text_block, subpolicy, tokens, words, length
    companion_text_block = COMPANION_VISIBLE_BAD_PATTERN.search(text)
    if companion_text_block:
        return 0, f"companion_visible_bad_literal:{companion_text_block.group(0)}", subpolicy, tokens, words, length
    if subpolicy not in ALLOWED_COMPANION_SUBPOLICIES:
        return 0, f"companion_subpolicy_not_first_release:{subpolicy}", subpolicy, tokens, words, length

    if cluster == "plain_sentence_without_known_issue" and (length > 220 or words > 35 or tokens > 4):
        return 0, "plain_sentence_too_complex", subpolicy, tokens, words, length
    if cluster == "building_or_holding_description" and (length > 260 or words > 42 or tokens > 2):
        return 0, "building_description_too_complex", subpolicy, tokens, words, length
    if cluster == "rule_effect_or_modifier_text" and (length > 220 or words > 34 or tokens > 6):
        return 0, "rule_effect_modifier_too_complex", subpolicy, tokens, words, length

    return 1, "", subpolicy, tokens, words, length


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
        "autofix_ledger_item_id",
        "semantic_ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "cluster",
        "subpolicy_name",
        "checkpoint_action",
        "checkpoint_allowed",
        "block_reason",
        "open_issue_count",
        "token_count",
        "word_count",
        "text_length",
        "evidence_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: row.get(field) for field in fields} for row in rows)

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {field: row.get(field) for field in fields}
            payload["confirmed_preview"] = short(row.get("current_confirmed_text"), 260)
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    allowed = [row for row in rows if int(row["checkpoint_allowed"]) == 1]
    blocked = [row for row in rows if int(row["checkpoint_allowed"]) == 0]
    blocker_counts = Counter(row["block_reason"] or "checkpoint_allowed" for row in rows)
    subpolicy_counts = Counter(row["subpolicy_name"] for row in allowed)
    cluster_counts = Counter(row["cluster"] for row in rows)

    lines = [
        "Autofix Unknown Semantic Companion Checkpoint",
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
        "Allowed by subpolicy:",
        *[f"- {key}: {value:,}" for key, value in subpolicy_counts.most_common()],
        "",
        "Candidates by cluster:",
        *[f"- {key}: {value:,}" for key, value in cluster_counts.most_common()],
        "",
        "Checkpoint counts:",
        *[f"- {key}: {value:,}" for key, value in blocker_counts.most_common()],
        "",
        "Allowed samples:",
    ]
    for row in allowed[:40]:
        lines.append(
            f"- segment={row['segment_id']} | {row['subpolicy_name']} | "
            f"{row['relative_path']}::{row['source_key']} | {short(row.get('current_confirmed_text'), 220)}"
        )
    lines.extend(["", "Blocked samples:"])
    for row in blocked[:40]:
        lines.append(
            f"- {row['block_reason']} | segment={row['segment_id']} | "
            f"{row['cluster']} | {row['relative_path']}::{row['source_key']} | "
            f"{short(row.get('current_confirmed_text'), 220)}"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Checkpoint only: no source/output writes, no confirmations, no segment-state closure.",
            "- Allowed means semantic companion evidence for future lifecycle bridge, not production authority by itself.",
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
            allowed, block_reason, subpolicy, tokens, words, length = classify(row)
            row["cluster"] = surface_cluster(row)
            row["subpolicy_name"] = subpolicy
            row["checkpoint_action"] = CHECKPOINT_ACTION
            row["checkpoint_allowed"] = allowed
            row["block_reason"] = block_reason
            row["token_count"] = tokens
            row["word_count"] = words
            row["text_length"] = length
            row["current_confirmed_text_hash"] = stable_hash(row.get("current_confirmed_text"))
            row["current_output_text_hash"] = stable_hash(row.get("current_output_text"))

        allowed_count = sum(1 for row in rows if int(row["checkpoint_allowed"]) == 1)
        blocked_count = len(rows) - allowed_count
        checkpoint_status = "ready_for_shadow_lifecycle" if allowed_count else "blocked_by_checkpoint_guard"
        subpolicy_counts = Counter(row["subpolicy_name"] for row in rows if int(row["checkpoint_allowed"]) == 1)
        blocker_counts = Counter(row["block_reason"] or "checkpoint_allowed" for row in rows)

        checkpoint_run_id = int(
            conn.execute(
                """
                INSERT INTO ml_issue_autofix_unknown_semantic_companion_checkpoint_runs (
                    rule_version,
                    checkpoint_name,
                    checkpoint_status,
                    agent_key,
                    ledger_run_id,
                    segment_state_run_id,
                    total_candidates,
                    checkpoint_allowed_count,
                    checkpoint_blocked_count,
                    subpolicy_counts_json,
                    blocker_counts_json,
                    report_path,
                    csv_path,
                    jsonl_path,
                    started_at,
                    finished_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    RULE_VERSION,
                    CHECKPOINT_NAME,
                    checkpoint_status,
                    AGENT_KEY,
                    selected_ledger_run_id,
                    selected_segment_state_run_id,
                    len(rows),
                    allowed_count,
                    blocked_count,
                    json.dumps(dict(subpolicy_counts), ensure_ascii=False, sort_keys=True),
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
                    INSERT INTO ml_issue_autofix_unknown_semantic_companion_checkpoint_items (
                        checkpoint_run_id,
                        ledger_run_id,
                        segment_state_run_id,
                        autofix_ledger_item_id,
                        semantic_ledger_item_id,
                        segment_id,
                        relative_path,
                        source_key,
                        source_line_number,
                        cluster,
                        subpolicy_name,
                        checkpoint_action,
                        checkpoint_allowed,
                        block_reason,
                        open_issue_count,
                        token_count,
                        word_count,
                        text_length,
                        evidence_text,
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
                        row["semantic_ledger_item_id"],
                        row["segment_id"],
                        row["relative_path"],
                        row["source_key"],
                        row["source_line_number"],
                        row["cluster"],
                        row["subpolicy_name"],
                        row["checkpoint_action"],
                        int(row["checkpoint_allowed"]),
                        row["block_reason"],
                        int(row.get("open_issue_count") or 0),
                        int(row.get("token_count") or 0),
                        int(row.get("word_count") or 0),
                        int(row.get("text_length") or 0),
                        row.get("evidence_text"),
                        row["current_confirmed_text_hash"],
                        row["current_output_text_hash"],
                        now,
                    ),
                ).lastrowid
            )
            row["checkpoint_item_id"] = checkpoint_item_id
            row["checkpoint_run_id"] = checkpoint_run_id
            row["autofix_ledger_item_id"] = int(row["id"])
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
    print("[autofix_unknown_semantic_companion_checkpoint] Checkpoint generated")
    print(f"[autofix_unknown_semantic_companion_checkpoint] Run id: {checkpoint_run_id}")
    print(f"[autofix_unknown_semantic_companion_checkpoint] Ledger run id: {selected_ledger_run_id}")
    print(f"[autofix_unknown_semantic_companion_checkpoint] Segment-state run id: {selected_segment_state_run_id}")
    print(f"[autofix_unknown_semantic_companion_checkpoint] Candidates: {len(rows):,}")
    print(f"[autofix_unknown_semantic_companion_checkpoint] Allowed: {allowed_count:,}")
    print(f"[autofix_unknown_semantic_companion_checkpoint] Blocked: {blocked_count:,}")
    print(f"[autofix_unknown_semantic_companion_checkpoint] Report: {txt_path}")
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
    parser = argparse.ArgumentParser(description="Checkpoint autofix_unknown + semantic companion candidates.")
    parser.add_argument("--ledger-run-id", type=int)
    parser.add_argument("--segment-state-run-id", type=int)
    args = parser.parse_args()
    main(ledger_run_id=args.ledger_run_id, segment_state_run_id=args.segment_state_run_id)
