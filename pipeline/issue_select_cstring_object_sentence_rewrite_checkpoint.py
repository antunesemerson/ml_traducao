from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short, structural_tokens


RULE_VERSION = "issue_select_cstring_object_sentence_rewrite_checkpoint_v1"
POLICY_NAME = "select_cstring_object_sentence_rewrite_shadow_v1"
POLICY_STATUS = "shadow"
AGENT_KEY = "micro_select_cstring_object_sentence_rewrite"
CHECKPOINT_ACTION = "stage_select_cstring_object_sentence_rewrite_shadow"
SOURCE_BLOCK_REASON = "action_sentence_requires_object_rewrite_neuron"

OLD_FRAGMENT = (
    "[Select_CString( ROOT.Char.IsLocalPlayer, 'obligu\u00e9', 'oblig\u00f3' )] "
    "a [Select_CString( second.IsLocalPlayer, 'mi persona', second.GetFirstNameNoTooltip )] "
    "a uma posi\u00e7\u00e3o de desist\u00eancia."
)
NEW_FRAGMENT = (
    "[Select_CString( ROOT.Char.IsLocalPlayer, 'forcei', 'for\u00e7ou' )] "
    "[Select_CString( second.IsLocalPlayer, 'voc\u00ea', second.GetFirstNameNoTooltip )] "
    "a uma posi\u00e7\u00e3o de desist\u00eancia."
)
RESIDUAL_MARKERS = ("obligu\u00e9", "oblig\u00f3", "mi persona")


def latest_source_checkpoint_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_select_cstring_label_rewrite_checkpoint_runs
        WHERE policy_name = 'select_cstring_visible_label_rewrite_shadow_v1'
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No Select_CString label rewrite checkpoint run found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_object_sentence_rewrite_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            source_checkpoint_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            allowed_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
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
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_object_sentence_rewrite_checkpoint_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            source_checkpoint_run_id INTEGER NOT NULL,
            source_checkpoint_item_id INTEGER NOT NULL,
            decision_run_id INTEGER NOT NULL,
            decision_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            agent_key TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            checkpoint_action TEXT NOT NULL,
            block_reason TEXT,
            token_status TEXT NOT NULL,
            current_text TEXT NOT NULL,
            corrected_text TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_select_cstring_object_sentence_rewrite_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], source_checkpoint_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_select_cstring_object_sentence_rewrite_checkpoint_source_run_{source_checkpoint_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_rows(conn, *, source_checkpoint_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.id AS source_checkpoint_item_id,
            item.run_id AS source_checkpoint_run_id,
            item.decision_run_id,
            item.decision_id,
            item.queue_item_id,
            item.ledger_item_id,
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.source_line_number,
            item.current_text,
            source.english_text,
            source.spanish_text,
            source.old_text
        FROM ml_issue_select_cstring_label_rewrite_checkpoint_items item
        JOIN source_segments source ON source.id = item.segment_id
        WHERE item.run_id = ?
          AND item.checkpoint_allowed = 0
          AND item.block_reason = ?
        ORDER BY item.segment_id
        """,
        (source_checkpoint_run_id, SOURCE_BLOCK_REASON),
    ).fetchall()
    return [dict(row) for row in rows]


def propose_rewrite(row: dict[str, Any]) -> tuple[str, list[str], str]:
    current = row["current_text"] or ""
    english = row.get("english_text") or ""
    if row.get("source_key") != "tournament_versus_contest_end_flavor_board_game_3":
        return "", [], "unexpected_source_key"
    if "forced" not in english.casefold() or "position of forfeit" not in english.casefold():
        return "", [], "english_alignment_missing"
    if OLD_FRAGMENT not in current:
        return "", [], "exact_forced_object_fragment_missing"
    corrected = current.replace(OLD_FRAGMENT, NEW_FRAGMENT, 1)
    return (
        corrected,
        [
            "root_localplayer_obligue_obligo_to_forcei_forcou",
            "second_localplayer_mi_persona_to_voce",
            "removed_spanish_object_preposition_before_second_token",
        ],
        "",
    )


def classify(row: dict[str, Any]) -> tuple[int, str, str, str, str, list[str]]:
    current = row["current_text"] or ""
    subpolicy_name = "select_cstring_forced_object_sentence_rewrite"
    if not current.strip():
        return 0, "missing_current_text", "missing_text", "", subpolicy_name, []

    corrected, reasons, proposal_block = propose_rewrite(row)
    if proposal_block:
        return 0, proposal_block, "no_text_delta", "", subpolicy_name, reasons
    if corrected == current:
        return 0, "no_object_sentence_rewrite_delta", "no_text_delta", "", subpolicy_name, reasons
    if "?" in corrected and "?" not in current:
        return 0, "lossy_question_marker_after_rewrite", "encoding_review_required", corrected, subpolicy_name, reasons
    if structural_tokens(current) != structural_tokens(corrected):
        return 0, "structural_tokens_changed", "structural_token_change_review_required", corrected, subpolicy_name, reasons
    residual = [marker for marker in RESIDUAL_MARKERS if marker.casefold() in corrected.casefold()]
    if residual:
        return (
            0,
            "residual_spanish_object_sentence_marker",
            "residual_spanish_after_rewrite",
            corrected,
            subpolicy_name,
            [*reasons, f"residual={','.join(residual)}"],
        )
    return 1, "", "same_structural_tokens", corrected, subpolicy_name, reasons


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    source_checkpoint_run_id: int,
    rows: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    fields = [
        "checkpoint_allowed",
        "block_reason",
        "source_checkpoint_item_id",
        "decision_id",
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "subpolicy_name",
        "token_status",
        "current_text",
        "corrected_text",
        "reasons",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            payload = dict(row)
            payload["reasons"] = "; ".join(row["reasons"])
            writer.writerow(payload)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Issue Select_CString object sentence rewrite checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Source checkpoint run id: {source_checkpoint_run_id}",
        f"Policy: {POLICY_NAME}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Allowed: {counts['allowed']:,}",
        f"- Blocked: {counts['blocked']:,}",
        "",
        "Token status:",
        *[f"- {key}: {value:,}" for key, value in counts.items() if key.startswith("token:")],
        "",
        "Subpolicies:",
        *[f"- {key}: {value:,}" for key, value in counts.items() if key.startswith("subpolicy:")],
        "",
        "Blocks:",
        *[f"- {key}: {value:,}" for key, value in counts.items() if key.startswith("block:")],
        "",
        "Samples:",
    ]
    for row in rows[:80]:
        lines.extend(
            [
                (
                    f"- allowed={row['checkpoint_allowed']} token={row['token_status']} "
                    f"block={row['block_reason'] or 'none'} segment={row['segment_id']} "
                    f"{row['relative_path']}::{row['source_key']} subpolicy={row['subpolicy_name']}"
                ),
                f"  current: {short(row['current_text'], 260)}",
                f"  corrected: {short(row['corrected_text'], 260)}",
                f"  reasons: {', '.join(row['reasons'])}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This checkpoint stages shadow evidence for the exact forced-object Select_CString sentence pattern.",
            "- It preserves the CK3 structural token set and only changes visible text and a surface preposition.",
            "- It does not write source/output and does not promote production policy.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, source_checkpoint_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_source_run_id = source_checkpoint_run_id or latest_source_checkpoint_run_id(conn)
        source_rows = fetch_rows(conn, source_checkpoint_run_id=selected_source_run_id)
        classified: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        for source in source_rows:
            allowed, block_reason, token_status, corrected, subpolicy_name, reasons = classify(source)
            counts["allowed" if allowed else "blocked"] += 1
            counts[f"token:{token_status}"] += 1
            counts[f"subpolicy:{subpolicy_name}"] += 1
            if block_reason:
                counts[f"block:{block_reason}"] += 1
            classified.append(
                {
                    "source_checkpoint_item_id": source["source_checkpoint_item_id"],
                    "decision_run_id": source["decision_run_id"],
                    "decision_id": source["decision_id"],
                    "queue_item_id": source["queue_item_id"],
                    "ledger_item_id": source["ledger_item_id"],
                    "segment_id": source["segment_id"],
                    "relative_path": source["relative_path"],
                    "source_key": source["source_key"],
                    "source_line_number": source.get("source_line_number"),
                    "agent_key": AGENT_KEY,
                    "subpolicy_name": subpolicy_name,
                    "checkpoint_allowed": allowed,
                    "checkpoint_action": CHECKPOINT_ACTION,
                    "block_reason": block_reason,
                    "token_status": token_status,
                    "current_text": source["current_text"],
                    "corrected_text": corrected,
                    "reasons": reasons,
                }
            )

        txt_path, csv_path, jsonl_path = report_paths(settings, selected_source_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_select_cstring_object_sentence_rewrite_checkpoint_runs (
                rule_version,
                policy_name,
                policy_status,
                source_checkpoint_run_id,
                candidate_count,
                allowed_count,
                blocked_count,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                POLICY_NAME,
                POLICY_STATUS,
                selected_source_run_id,
                len(classified),
                counts["allowed"],
                counts["blocked"],
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        run_id = int(cur.lastrowid)
        for row in classified:
            conn.execute(
                """
                INSERT INTO ml_issue_select_cstring_object_sentence_rewrite_checkpoint_items (
                    run_id,
                    source_checkpoint_run_id,
                    source_checkpoint_item_id,
                    decision_run_id,
                    decision_id,
                    queue_item_id,
                    ledger_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    agent_key,
                    subpolicy_name,
                    checkpoint_allowed,
                    checkpoint_action,
                    block_reason,
                    token_status,
                    current_text,
                    corrected_text,
                    reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_source_run_id,
                    row["source_checkpoint_item_id"],
                    row["decision_run_id"],
                    row["decision_id"],
                    row["queue_item_id"],
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    row["agent_key"],
                    row["subpolicy_name"],
                    row["checkpoint_allowed"],
                    row["checkpoint_action"],
                    row["block_reason"],
                    row["token_status"],
                    row["current_text"],
                    row["corrected_text"],
                    json.dumps(row["reasons"], ensure_ascii=False),
                    now,
                ),
            )
        conn.commit()

    write_reports(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        run_id=run_id,
        source_checkpoint_run_id=selected_source_run_id,
        rows=classified,
        counts=counts,
    )
    print("[issue_select_cstring_object_sentence_rewrite_checkpoint] Checkpoint generated")
    print(f"[issue_select_cstring_object_sentence_rewrite_checkpoint] Run id: {run_id}")
    print(f"[issue_select_cstring_object_sentence_rewrite_checkpoint] Source checkpoint run id: {selected_source_run_id}")
    print(f"[issue_select_cstring_object_sentence_rewrite_checkpoint] Candidates: {len(classified)}")
    print(f"[issue_select_cstring_object_sentence_rewrite_checkpoint] Allowed: {counts['allowed']}")
    print(f"[issue_select_cstring_object_sentence_rewrite_checkpoint] Blocked: {counts['blocked']}")
    print(f"[issue_select_cstring_object_sentence_rewrite_checkpoint] Report: {txt_path}")
    return {
        "run_id": run_id,
        "source_checkpoint_run_id": selected_source_run_id,
        "candidate_count": len(classified),
        "allowed_count": counts["allowed"],
        "blocked_count": counts["blocked"],
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Checkpoint guarded Select_CString object sentence rewrites.")
    parser.add_argument("--source-checkpoint-run-id", type=int, default=None)
    args = parser.parse_args()
    main(source_checkpoint_run_id=args.source_checkpoint_run_id)
