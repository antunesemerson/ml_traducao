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


RULE_VERSION = "issue_nickname_pauper_king_title_semantic_checkpoint_v1"
POLICY_NAME = "nickname_pauper_king_title_semantic_shadow_v1"
POLICY_STATUS = "shadow"
AGENT_KEY = "micro_nickname_pauper_king_title_semantic"
CHECKPOINT_ACTION = "stage_nickname_pauper_king_title_semantic_shadow"
SOURCE_BLOCK_REASON = "title_semantic_mismatch_requires_title_neuron"

CURRENT_TEXT = "[Select_CString( CHARACTER.IsFemale, 'la Reina', 'el Rey' )] Convidador[CHARACTER.Custom('ES_XA')]"
CORRECTED_TEXT = (
    "[Select_CString( CHARACTER.IsFemale, 'a Rainha', 'o Rei' )] "
    "Mendig[CHARACTER.Custom('ES_OA')]"
)
REFERENCE_KEY = "journey_pauper_king_modifier"
REFERENCE_TRANSLATION = "O Rei Mendigo"
EXPECTED_REMOVED_TOKEN = "[CHARACTER.Custom('ES_XA')]"
EXPECTED_ADDED_TOKEN = "[CHARACTER.Custom('ES_OA')]"


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
        CREATE TABLE IF NOT EXISTS ml_issue_nickname_pauper_king_title_semantic_checkpoint_runs (
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
        CREATE TABLE IF NOT EXISTS ml_issue_nickname_pauper_king_title_semantic_checkpoint_items (
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
            reference_key TEXT,
            reference_translation TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_nickname_pauper_king_title_semantic_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], source_checkpoint_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_nickname_pauper_king_title_semantic_checkpoint_source_run_{source_checkpoint_run_id}"
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


def reference_is_present(conn) -> bool:
    row = conn.execute(
        """
        SELECT old_text
        FROM source_segments
        WHERE source_key = ?
          AND english_text = 'The Pauper King'
        ORDER BY id
        LIMIT 1
        """,
        (REFERENCE_KEY,),
    ).fetchone()
    return bool(row and (row["old_text"] or "") == REFERENCE_TRANSLATION)


def expected_gender_helper_delta(current: str, corrected: str) -> bool:
    current_tokens = structural_tokens(current)
    corrected_tokens = structural_tokens(corrected)
    removed = current_tokens - corrected_tokens
    added = corrected_tokens - current_tokens
    return dict(removed) == {EXPECTED_REMOVED_TOKEN: 1} and dict(added) == {EXPECTED_ADDED_TOKEN: 1}


def propose_rewrite(row: dict[str, Any], *, has_reference: bool) -> tuple[str, list[str], str]:
    current = row["current_text"] or ""
    if row.get("relative_path") != "nicknames_l_spanish.yml":
        return "", [], "path_not_nicknames"
    if row.get("source_key") != "nick_pauper_king":
        return "", [], "unexpected_source_key"
    if (row.get("english_text") or "").strip().casefold() != "the pauper king":
        return "", [], "english_alignment_missing"
    if current != CURRENT_TEXT:
        return "", [], "exact_pauper_king_current_text_missing"
    if not has_reference:
        return "", [], "reference_translation_missing"
    return (
        CORRECTED_TEXT,
        [
            "reference_translation_journey_pauper_king_modifier=o_rei_mendigo",
            "select_cstring_reina_rey_to_rainha_rei",
            "convidador_semantic_mismatch_to_mendigo",
            "gender_helper_es_xa_to_es_oa_for_mendigo_mendiga",
        ],
        "",
    )


def classify(row: dict[str, Any], *, has_reference: bool) -> tuple[int, str, str, str, str, list[str]]:
    current = row["current_text"] or ""
    subpolicy_name = "nickname_pauper_king_title_semantic_rewrite"
    if not current.strip():
        return 0, "missing_current_text", "missing_text", "", subpolicy_name, []

    corrected, reasons, proposal_block = propose_rewrite(row, has_reference=has_reference)
    if proposal_block:
        return 0, proposal_block, "no_text_delta", "", subpolicy_name, reasons
    if corrected == current:
        return 0, "no_title_semantic_rewrite_delta", "no_text_delta", "", subpolicy_name, reasons
    if "Convidador" in corrected or "la Reina" in corrected or "el Rey" in corrected:
        return 0, "residual_title_semantic_marker", "residual_spanish_after_rewrite", corrected, subpolicy_name, reasons
    if structural_tokens(current) == structural_tokens(corrected):
        return 1, "", "same_structural_tokens", corrected, subpolicy_name, reasons
    if expected_gender_helper_delta(current, corrected):
        return 1, "", "expected_gender_helper_change_es_xa_to_es_oa", corrected, subpolicy_name, reasons
    return 0, "unexpected_structural_token_change", "structural_token_change_review_required", corrected, subpolicy_name, reasons


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
        "reference_key",
        "reference_translation",
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
        "Issue nickname Pauper King title semantic checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Source checkpoint run id: {source_checkpoint_run_id}",
        f"Policy: {POLICY_NAME}",
        "Production release allowed: 0",
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
                f"  reference: {row['reference_key']} -> {row['reference_translation']}",
                f"  current: {short(row['current_text'], 260)}",
                f"  corrected: {short(row['corrected_text'], 260)}",
                f"  reasons: {', '.join(row['reasons'])}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This checkpoint stages shadow evidence for the exact nick_pauper_king semantic mismatch.",
            "- It uses an internal reference translation already present in the database: The Pauper King -> O Rei Mendigo.",
            "- It changes ES_XA to ES_OA only for the Mendig[o/a] adjective and therefore still requires token-policy awareness before production.",
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
        has_reference = reference_is_present(conn)
        source_rows = fetch_rows(conn, source_checkpoint_run_id=selected_source_run_id)
        classified: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        for source in source_rows:
            allowed, block_reason, token_status, corrected, subpolicy_name, reasons = classify(
                source,
                has_reference=has_reference,
            )
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
                    "reference_key": REFERENCE_KEY if has_reference else "",
                    "reference_translation": REFERENCE_TRANSLATION if has_reference else "",
                    "reasons": reasons,
                }
            )

        txt_path, csv_path, jsonl_path = report_paths(settings, selected_source_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_nickname_pauper_king_title_semantic_checkpoint_runs (
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
                INSERT INTO ml_issue_nickname_pauper_king_title_semantic_checkpoint_items (
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
                    reference_key,
                    reference_translation,
                    reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    row["reference_key"],
                    row["reference_translation"],
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
    print("[issue_nickname_pauper_king_title_semantic_checkpoint] Checkpoint generated")
    print(f"[issue_nickname_pauper_king_title_semantic_checkpoint] Run id: {run_id}")
    print(f"[issue_nickname_pauper_king_title_semantic_checkpoint] Source checkpoint run id: {selected_source_run_id}")
    print(f"[issue_nickname_pauper_king_title_semantic_checkpoint] Candidates: {len(classified)}")
    print(f"[issue_nickname_pauper_king_title_semantic_checkpoint] Allowed: {counts['allowed']}")
    print(f"[issue_nickname_pauper_king_title_semantic_checkpoint] Blocked: {counts['blocked']}")
    print(f"[issue_nickname_pauper_king_title_semantic_checkpoint] Report: {txt_path}")
    return {
        "run_id": run_id,
        "source_checkpoint_run_id": selected_source_run_id,
        "candidate_count": len(classified),
        "allowed_count": counts["allowed"],
        "blocked_count": counts["blocked"],
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Checkpoint guarded Pauper King nickname semantic rewrite.")
    parser.add_argument("--source-checkpoint-run-id", type=int, default=None)
    args = parser.parse_args()
    main(source_checkpoint_run_id=args.source_checkpoint_run_id)
