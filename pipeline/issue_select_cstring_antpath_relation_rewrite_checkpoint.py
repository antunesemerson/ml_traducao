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


RULE_VERSION = "issue_select_cstring_antpath_relation_rewrite_checkpoint_v1"
POLICY_NAME = "select_cstring_antpath_relation_rewrite_shadow_v1"
POLICY_STATUS = "shadow"
AGENT_KEY = "micro_select_cstring_antpath_relation_rewrite"
CHECKPOINT_ACTION = "stage_select_cstring_antpath_relation_rewrite_shadow"
SOURCE_BLOCK_REASON = "no_auxiliary_rewrite_rule"

TARGET_KEYS = {
    "friend_humored_antpath_superstition",
    "friend_humored_antpath_superstition_corresponding",
}
TARGET_PATH = "relationship_reasons_filippa_l_spanish.yml"

CURRENT_TEXT_BY_KEY = {
    "friend_humored_antpath_superstition": (
        "[CHARACTER.GetShortUIName|U] "
        "[Select_CString( TARGET_CHARACTER.IsLocalPlayer, 'te', 'le' )] "
        "[Select_CString( CHARACTER.IsLocalPlayer, 'seguiste', 'sigui\u00f3' )] "
        "\u00e0s supersti\u00e7\u00f5es de "
        "[Select_CString( TARGET_CHARACTER.IsLocalPlayer, 'tus ancestros', TARGET_CHARACTER.GetShortUIName )] "
        "sobre cruzar caminhos de formigas."
    ),
    "friend_humored_antpath_superstition_corresponding": (
        "[TARGET_CHARACTER.GetShortUIName|U] "
        "[Select_CString( CHARACTER.IsLocalPlayer, 'te', 'le' )] "
        "[Select_CString( TARGET_CHARACTER.IsLocalPlayer, 'seguiste', 'sigui\u00f3' )] "
        "seguiu a corrente das supersti\u00e7\u00f5es de "
        "[Select_CString( CHARACTER.IsLocalPlayer, 'tus ancestros', CHARACTER.GetShortUIName )] "
        "sobre cruzar caminhos de formigas."
    ),
}

CORRECTED_TEXT_BY_KEY = {
    "friend_humored_antpath_superstition": (
        "[CHARACTER.GetShortUIName|U] "
        "[Select_CString( TARGET_CHARACTER.IsLocalPlayer, 'respeitou suas supersti\u00e7\u00f5es', "
        "'respeitou as supersti\u00e7\u00f5es de ' )]"
        "[Select_CString( CHARACTER.IsLocalPlayer, '', '' )]"
        "[Select_CString( TARGET_CHARACTER.IsLocalPlayer, '', TARGET_CHARACTER.GetShortUIName )] "
        "sobre n\u00e3o cruzar caminhos de formigas."
    ),
    "friend_humored_antpath_superstition_corresponding": (
        "[TARGET_CHARACTER.GetShortUIName|U] "
        "[Select_CString( CHARACTER.IsLocalPlayer, 'respeitou suas supersti\u00e7\u00f5es', "
        "'respeitou as supersti\u00e7\u00f5es de ' )]"
        "[Select_CString( TARGET_CHARACTER.IsLocalPlayer, '', '' )]"
        "[Select_CString( CHARACTER.IsLocalPlayer, '', CHARACTER.GetShortUIName )] "
        "sobre n\u00e3o cruzar caminhos de formigas."
    ),
}

RESIDUAL_MARKERS = ("seguiste", "sigui\u00f3", "tus ancestros", "'te'", "'le'")


def latest_source_checkpoint_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_select_cstring_auxiliary_rewrite_checkpoint_runs
        WHERE policy_name = 'select_cstring_auxiliary_sentence_rewrite_shadow_v1'
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No Select_CString auxiliary rewrite checkpoint run found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_antpath_relation_rewrite_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            source_auxiliary_checkpoint_run_id INTEGER NOT NULL,
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
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_antpath_relation_rewrite_checkpoint_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            source_auxiliary_checkpoint_run_id INTEGER NOT NULL,
            source_auxiliary_checkpoint_item_id INTEGER,
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
            english_text TEXT,
            current_text TEXT NOT NULL,
            corrected_text TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_select_cstring_antpath_relation_rewrite_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], source_auxiliary_checkpoint_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = (
        reports_dir
        / f"{stamp}_issue_select_cstring_antpath_relation_rewrite_checkpoint_aux_run_{source_auxiliary_checkpoint_run_id}"
    )
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_rows(conn, *, source_auxiliary_checkpoint_run_id: int) -> list[dict[str, Any]]:
    aux_items = {
        int(row["segment_id"]): int(row["id"])
        for row in conn.execute(
            """
            SELECT id, segment_id
            FROM ml_issue_select_cstring_auxiliary_rewrite_checkpoint_items
            WHERE run_id = ?
              AND checkpoint_allowed = 0
              AND block_reason = ?
            """,
            (source_auxiliary_checkpoint_run_id, SOURCE_BLOCK_REASON),
        ).fetchall()
    }
    placeholders = ", ".join("?" for _ in TARGET_KEYS)
    rows = conn.execute(
        f"""
        SELECT
            id AS segment_id,
            relative_path,
            source_key,
            source_line_number,
            english_text,
            old_text AS current_text
        FROM source_segments
        WHERE relative_path = ?
          AND source_key IN ({placeholders})
        ORDER BY segment_id
        """,
        (TARGET_PATH, *sorted(TARGET_KEYS)),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        payload["source_auxiliary_checkpoint_item_id"] = aux_items.get(int(row["segment_id"]))
        result.append(payload)
    return result


def propose_rewrite(row: dict[str, Any]) -> tuple[str, list[str], str]:
    source_key = row["source_key"]
    current = row["current_text"] or ""
    english = row.get("english_text") or ""
    expected_current = CURRENT_TEXT_BY_KEY.get(source_key)
    corrected = CORRECTED_TEXT_BY_KEY.get(source_key)
    if row.get("relative_path") != TARGET_PATH:
        return "", [], "path_not_relationship_reasons_filippa"
    if source_key not in TARGET_KEYS:
        return "", [], "unexpected_source_key"
    if "humored" not in english.casefold() or "ant paths" not in english.casefold():
        return "", [], "english_alignment_missing"
    if current != expected_current:
        return "", [], "exact_antpath_current_text_missing"
    if corrected is None:
        return "", [], "missing_corrected_text"
    return (
        corrected,
        [
            "relationship_reason_humored_superstitions_alignment",
            "te_le_plus_seguiste_siguio_to_respeitou_supersticoes",
            "tus_ancestros_removed_from_possessive_superstition_phrase",
            "not_crossing_ant_paths_restored_from_english",
        ],
        "",
    )


def residual_markers(corrected: str) -> list[str]:
    low = corrected.casefold()
    return [marker for marker in RESIDUAL_MARKERS if marker.casefold() in low]


def classify(row: dict[str, Any]) -> tuple[int, str, str, str, str, list[str]]:
    current = row["current_text"] or ""
    subpolicy_name = "select_cstring_antpath_relationship_rewrite"
    if not current.strip():
        return 0, "missing_current_text", "missing_text", "", subpolicy_name, []

    corrected, reasons, proposal_block = propose_rewrite(row)
    if proposal_block:
        return 0, proposal_block, "no_text_delta", "", subpolicy_name, reasons
    if corrected == current:
        return 0, "no_antpath_relation_rewrite_delta", "no_text_delta", "", subpolicy_name, reasons
    residual = residual_markers(corrected)
    if residual:
        return (
            0,
            "residual_antpath_relation_marker:" + ",".join(residual[:6]),
            "residual_spanish_after_rewrite",
            corrected,
            subpolicy_name,
            reasons,
        )
    if structural_tokens(current) != structural_tokens(corrected):
        return (
            0,
            "structural_tokens_changed",
            "structural_token_change_review_required",
            corrected,
            subpolicy_name,
            reasons,
        )
    return 1, "", "same_structural_tokens", corrected, subpolicy_name, reasons


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    source_auxiliary_checkpoint_run_id: int,
    rows: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    fields = [
        "checkpoint_allowed",
        "block_reason",
        "source_auxiliary_checkpoint_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "subpolicy_name",
        "token_status",
        "english_text",
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
        "Issue Select_CString antpath relation rewrite checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Source auxiliary checkpoint run id: {source_auxiliary_checkpoint_run_id}",
        f"Policy: {POLICY_NAME}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Allowed shadow: {counts['allowed']:,}",
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
                f"  english: {short(row.get('english_text'), 260)}",
                f"  current: {short(row['current_text'], 260)}",
                f"  corrected: {short(row['corrected_text'], 260)}",
                f"  reasons: {', '.join(row['reasons'])}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety:",
            "- Shadow-only checkpoint; no source/output files read and no output is written.",
            "- The rewrite keeps the normalized CK3 token shell identical and only changes literal payloads.",
            "- It is not promoted for production until a token-policy/lifecycle bridge explicitly accepts this subpolicy.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Checkpoint antpath relationship Select_CString rewrites.")
    parser.add_argument("--source-auxiliary-checkpoint-run-id", type=int, default=None)
    args = parser.parse_args()

    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        source_auxiliary_checkpoint_run_id = args.source_auxiliary_checkpoint_run_id or latest_source_checkpoint_run_id(conn)
        txt_path, csv_path, jsonl_path = report_paths(settings, source_auxiliary_checkpoint_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_select_cstring_antpath_relation_rewrite_checkpoint_runs (
                rule_version,
                policy_name,
                policy_status,
                source_auxiliary_checkpoint_run_id,
                started_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (RULE_VERSION, POLICY_NAME, POLICY_STATUS, source_auxiliary_checkpoint_run_id, now, now),
        )
        run_id = int(cursor.lastrowid)

        rows = []
        counts: Counter[str] = Counter()
        for source_row in fetch_rows(conn, source_auxiliary_checkpoint_run_id=source_auxiliary_checkpoint_run_id):
            allowed, block_reason, token_status, corrected, subpolicy_name, reasons = classify(source_row)
            row = {
                **source_row,
                "checkpoint_allowed": allowed,
                "checkpoint_action": CHECKPOINT_ACTION,
                "block_reason": block_reason,
                "token_status": token_status,
                "agent_key": AGENT_KEY,
                "subpolicy_name": subpolicy_name,
                "corrected_text": corrected,
                "reasons": reasons,
            }
            rows.append(row)
            counts["allowed" if allowed else "blocked"] += 1
            counts[f"token:{token_status}"] += 1
            counts[f"subpolicy:{subpolicy_name}"] += 1
            if block_reason:
                counts[f"block:{block_reason}"] += 1
            conn.execute(
                """
                INSERT INTO ml_issue_select_cstring_antpath_relation_rewrite_checkpoint_items (
                    run_id,
                    source_auxiliary_checkpoint_run_id,
                    source_auxiliary_checkpoint_item_id,
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
                    english_text,
                    current_text,
                    corrected_text,
                    reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    source_auxiliary_checkpoint_run_id,
                    row.get("source_auxiliary_checkpoint_item_id"),
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["agent_key"],
                    row["subpolicy_name"],
                    row["checkpoint_allowed"],
                    row["checkpoint_action"],
                    row["block_reason"],
                    row["token_status"],
                    row.get("english_text"),
                    row["current_text"],
                    row["corrected_text"],
                    json.dumps(row["reasons"], ensure_ascii=False),
                    now,
                ),
            )

        finished_at = datetime.now().isoformat(timespec="seconds")
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            source_auxiliary_checkpoint_run_id=source_auxiliary_checkpoint_run_id,
            rows=rows,
            counts=counts,
        )
        conn.execute(
            """
            UPDATE ml_issue_select_cstring_antpath_relation_rewrite_checkpoint_runs
            SET candidate_count = ?,
                allowed_count = ?,
                blocked_count = ?,
                report_path = ?,
                csv_path = ?,
                jsonl_path = ?,
                finished_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                len(rows),
                counts["allowed"],
                counts["blocked"],
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                finished_at,
                finished_at,
                run_id,
            ),
        )
        conn.commit()

    payload = {
        "run_id": run_id,
        "source_auxiliary_checkpoint_run_id": source_auxiliary_checkpoint_run_id,
        "candidate_count": len(rows),
        "allowed_count": counts["allowed"],
        "blocked_count": counts["blocked"],
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return payload


if __name__ == "__main__":
    main()
