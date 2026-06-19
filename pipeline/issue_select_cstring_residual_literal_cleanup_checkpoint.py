from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator
from apply_segment_state_updates import short, structural_tokens


RULE_VERSION = "issue_select_cstring_residual_literal_cleanup_checkpoint_v1"
POLICY_NAME = "select_cstring_residual_literal_cleanup_shadow_v1"
POLICY_STATUS = "shadow"
AGENT_KEY = "micro_select_cstring_residual_literal_cleanup"
CHECKPOINT_ACTION = "stage_select_cstring_residual_literal_cleanup_shadow"
SOURCE_POLICY_NAME = "select_cstring_same_token_shadow_lifecycle_v1"
SOURCE_BLOCK_REASON = "blocking_validation_issue"

BLOCKING_VALIDATION_CODES = {
    "spanish_punctuation",
    "mojibake_or_unexpected_script",
    "utf8_mojibake_sequence",
    "replacement_question_mark_mojibake",
    "spanish_residue",
    "spanish_residue_in_literal",
    "gender_token_extra_suffix",
}

FINAL_TEXT_BY_KEY = {
    "MIGRATION_INTERACTION_OBEDIENT_ACCEPTANCE": (
        "[recipient.GetSheHe|U][Select_CString( actor.IsLocalPlayer, '', '' )] "
        "est\u00e1 [obedient|lE] a "
        "[Select_CString( actor.IsLocalPlayer, 'voc\u00ea', actor.GetShortUINameNoFormat )] : $VALUE|+0=$"
    ),
    "nick_snake_in_the_eye_desc": (
        "Os viajantes sussurram que "
        "[Select_CString( CHARACTER.IsLocalPlayer, 'voc\u00ea', CHARACTER.GetSheHe )] "
        "[Select_CString( CHARACTER.IsLocalPlayer, 'porta', 'porta' )] o ouroboros, "
        "o s\u00edmbolo eterno de uma serpente mordendo a pr\u00f3pria cauda, como uma mancha no olho. "
        "Poucos est\u00e3o dispostos a chegar t\u00e3o perto para verificar a veracidade de tais afirma\u00e7\u00f5es."
    ),
    "nick_the_heartbreaker_desc": (
        "Dizem que [Select_CString( CHARACTER.IsLocalPlayer, 'voc\u00ea', CHARACTER.GetShortUINameNoTooltipNoFormat )] "
        "[Select_CString( CHARACTER.IsLocalPlayer, '', '' )] gosta um pouco de perigo."
    ),
    "nick_the_bully_desc": (
        "[Select_CString( CHARACTER.IsLocalPlayer, 'voc\u00ea', CHARACTER.GetShortUINameNoTooltipNoFormat )] "
        "[Select_CString( CHARACTER.IsLocalPlayer, '', '' )] gosta de implicar"
        "[Select_CString( CHARACTER.IsLocalPlayer, '', '' )] com aqueles que "
        "[Select_CString( CHARACTER.IsLocalPlayer, '', '' )] considera inferiores a "
        "[Select_CString( CHARACTER.IsLocalPlayer, 'voc\u00ea', CHARACTER.GetSheHe)]."
    ),
    "nick_the_silly_desc": (
        "Seja o que os outros disserem sobre "
        "[Select_CString( CHARACTER.IsLocalPlayer, 'voc\u00ea', CHARACTER.GetShortUINameNoTooltipNoFormat )] "
        "a respeito, [Select_CString( CHARACTER.IsLocalPlayer, 'voc\u00ea', CHARACTER.GetSheHe )] nunca "
        "[Select_CString( CHARACTER.IsLocalPlayer, 'leva', 'leva' )] a vida #EMP t\u00e3o a s\u00e9rio#!."
    ),
    "nick_of_a_thousand_faces_desc": (
        "[Select_CString( CHARACTER.IsLocalPlayer, 'voc\u00ea', CHARACTER.GetShortUINameNoTooltipNoFormat )] "
        "nunca \u00e9 vista com a mesma m\u00e1scara dois dias seguidos\u2026 se \u00e9 que "
        "[Select_CString( CHARACTER.IsLocalPlayer, 'voc\u00ea', CHARACTER.GetHerHim )] "
        "[Select_CString( CHARACTER.IsLocalPlayer, '\u00e9', '\u00e9' )] "
        "[Select_CString( CHARACTER.IsLocalPlayer, 'voc\u00ea', CHARACTER.GetSheHe )]\u2026"
    ),
    "nick_the_feeble_desc": (
        "[CHARACTER.GetShortUINameNoTooltipNoFormat|U] raramente "
        "[Select_CString( CHARACTER.IsLocalPlayer, 'pode', 'pode' )] reunir a for\u00e7a necess\u00e1ria "
        "at\u00e9 mesmo para mover objetos pequenos, e muitas vezes "
        "[Select_CString( CHARACTER.IsLocalPlayer, 'vira', 'vira' )] motivo de rid\u00edculo por isso."
    ),
}


def latest_lifecycle_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_select_cstring_same_token_lifecycle_runs
        WHERE finished_at IS NOT NULL
          AND policy_name = ?
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (SOURCE_POLICY_NAME,),
    ).fetchone()
    if row is None:
        raise RuntimeError("No Select_CString same-token lifecycle run found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_residual_literal_cleanup_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            source_lifecycle_run_id INTEGER NOT NULL,
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
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_residual_literal_cleanup_checkpoint_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            source_lifecycle_run_id INTEGER NOT NULL,
            source_lifecycle_item_id INTEGER NOT NULL,
            source_checkpoint_item_id INTEGER NOT NULL,
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
            original_text TEXT NOT NULL,
            current_text TEXT NOT NULL,
            corrected_text TEXT,
            validation_issues_json TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_select_cstring_residual_literal_cleanup_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], source_lifecycle_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_select_cstring_residual_literal_cleanup_checkpoint_lifecycle_run_{source_lifecycle_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_rows(conn, *, source_lifecycle_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            life.id AS source_lifecycle_item_id,
            life.source_checkpoint_item_id,
            life.segment_id,
            life.relative_path,
            life.source_key,
            life.source_line_number,
            life.validation_issues_json AS lifecycle_validation_issues_json,
            item.current_text AS original_text,
            item.corrected_text AS current_text,
            item.reasons_json AS source_reasons_json
        FROM ml_issue_select_cstring_same_token_lifecycle_items life
        JOIN ml_issue_dynamic_token_literal_repair_checkpoint_items item
          ON item.id = life.source_checkpoint_item_id
        WHERE life.run_id = ?
          AND life.policy_allowed = 0
          AND life.block_reason = ?
          AND life.source_family = 'dynamic_literal_payload'
        ORDER BY life.segment_id
        """,
        (source_lifecycle_run_id, SOURCE_BLOCK_REASON),
    ).fetchall()
    return [dict(row) for row in rows]


def blocking_validation_issues(text: str | None) -> list[dict[str, Any]]:
    validation = local_quality_validator.validate_text(text)
    issues = validation.get("issues") or []
    return [
        issue
        for issue in issues
        if issue.get("severity") == "high" or issue.get("code") in BLOCKING_VALIDATION_CODES
    ]


def proposal(row: dict[str, Any]) -> tuple[str, list[str], str]:
    source_key = row["source_key"]
    corrected = FINAL_TEXT_BY_KEY.get(source_key)
    if corrected is None:
        return "", [], "no_exact_cleanup_rule"
    reasons = [
        "second_layer_cleanup_after_dynamic_literal_checkpoint",
        "remove_residual_spanish_literals_from_select_cstring",
    ]
    if source_key.startswith("nick_"):
        reasons.append("nickname_sentence_rebuilt_for_ptbr_fluency")
    return corrected, reasons, ""


def classify(row: dict[str, Any]) -> tuple[int, str, str, str, str, list[str], list[dict[str, Any]]]:
    original = row.get("original_text") or ""
    current = row.get("current_text") or ""
    subpolicy_name = "select_cstring_residual_literal_cleanup"
    if not original.strip() or not current.strip():
        return 0, "missing_text", "missing_text", "", subpolicy_name, [], []
    corrected, reasons, block = proposal(row)
    if block:
        return 0, block, "no_text_delta", "", subpolicy_name, reasons, []
    if corrected == current:
        return 0, "no_cleanup_delta", "no_text_delta", "", subpolicy_name, reasons, []
    if structural_tokens(original) != structural_tokens(corrected):
        return (
            0,
            "structural_tokens_changed_from_original",
            "structural_token_change_review_required",
            corrected,
            subpolicy_name,
            reasons,
            [],
        )
    if structural_tokens(current) != structural_tokens(corrected):
        return (
            0,
            "structural_tokens_changed_from_current",
            "structural_token_change_review_required",
            corrected,
            subpolicy_name,
            reasons,
            [],
        )
    validation_issues = blocking_validation_issues(corrected)
    if validation_issues:
        return (
            0,
            "blocking_validation_issue_after_cleanup",
            "blocking_validation_issue",
            corrected,
            subpolicy_name,
            reasons,
            validation_issues,
        )
    return 1, "", "same_structural_tokens", corrected, subpolicy_name, reasons, validation_issues


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    source_lifecycle_run_id: int,
    rows: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    fields = [
        "checkpoint_allowed",
        "block_reason",
        "source_lifecycle_item_id",
        "source_checkpoint_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "subpolicy_name",
        "token_status",
        "current_text",
        "corrected_text",
        "validation_issues",
        "reasons",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            payload = dict(row)
            payload["validation_issues"] = json.dumps(row["validation_issues"], ensure_ascii=False, sort_keys=True)
            payload["reasons"] = "; ".join(row["reasons"])
            writer.writerow(payload)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Issue Select_CString residual literal cleanup checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Source lifecycle run id: {source_lifecycle_run_id}",
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
        "Blocks:",
        *([f"- {key}: {value:,}" for key, value in counts.items() if key.startswith("block:")] or ["- none"]),
        "",
        "Samples:",
    ]
    for row in rows[:80]:
        lines.extend(
            [
                (
                    f"- allowed={row['checkpoint_allowed']} token={row['token_status']} "
                    f"block={row['block_reason'] or 'none'} segment={row['segment_id']} "
                    f"{row['relative_path']}::{row['source_key']}"
                ),
                f"  current: {short(row['current_text'], 260)}",
                f"  corrected: {short(row['corrected_text'], 260)}",
                f"  validation: {json.dumps(row['validation_issues'], ensure_ascii=False)}",
                f"  reasons: {', '.join(row['reasons'])}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety:",
            "- Shadow-only checkpoint; no source/output files read and no output is written.",
            "- This is a second-layer cleanup on top of the dynamic literal checkpoint and preserves the normalized CK3 token shell.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Checkpoint residual Select_CString literal cleanup after lifecycle blocks.")
    parser.add_argument("--source-lifecycle-run-id", type=int, default=None)
    args = parser.parse_args()

    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        source_lifecycle_run_id = args.source_lifecycle_run_id or latest_lifecycle_run_id(conn)
        txt_path, csv_path, jsonl_path = report_paths(settings, source_lifecycle_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_select_cstring_residual_literal_cleanup_checkpoint_runs (
                rule_version,
                policy_name,
                policy_status,
                source_lifecycle_run_id,
                started_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (RULE_VERSION, POLICY_NAME, POLICY_STATUS, source_lifecycle_run_id, now, now),
        )
        run_id = int(cursor.lastrowid)

        rows: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        for source_row in fetch_rows(conn, source_lifecycle_run_id=source_lifecycle_run_id):
            allowed, block_reason, token_status, corrected, subpolicy_name, reasons, validation_issues = classify(source_row)
            row = {
                **source_row,
                "agent_key": AGENT_KEY,
                "subpolicy_name": subpolicy_name,
                "checkpoint_allowed": allowed,
                "checkpoint_action": CHECKPOINT_ACTION,
                "block_reason": block_reason,
                "token_status": token_status,
                "corrected_text": corrected,
                "validation_issues": validation_issues,
                "reasons": reasons,
            }
            rows.append(row)
            counts["allowed" if allowed else "blocked"] += 1
            counts[f"token:{token_status}"] += 1
            if block_reason:
                counts[f"block:{block_reason}"] += 1
            conn.execute(
                """
                INSERT INTO ml_issue_select_cstring_residual_literal_cleanup_checkpoint_items (
                    run_id,
                    source_lifecycle_run_id,
                    source_lifecycle_item_id,
                    source_checkpoint_item_id,
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
                    original_text,
                    current_text,
                    corrected_text,
                    validation_issues_json,
                    reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    source_lifecycle_run_id,
                    row["source_lifecycle_item_id"],
                    row["source_checkpoint_item_id"],
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
                    row["original_text"],
                    row["current_text"],
                    row["corrected_text"],
                    json.dumps(row["validation_issues"], ensure_ascii=False, sort_keys=True),
                    json.dumps(row["reasons"], ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )

        finished_at = datetime.now().isoformat(timespec="seconds")
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            source_lifecycle_run_id=source_lifecycle_run_id,
            rows=rows,
            counts=counts,
        )
        conn.execute(
            """
            UPDATE ml_issue_select_cstring_residual_literal_cleanup_checkpoint_runs
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
        "source_lifecycle_run_id": source_lifecycle_run_id,
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
