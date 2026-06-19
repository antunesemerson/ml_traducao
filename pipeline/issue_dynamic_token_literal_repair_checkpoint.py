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
from apply_segment_state_updates import short, structural_tokens
from issue_dynamic_literal_repair_diagnostic import propose_repair, residual_hits


RULE_VERSION = "issue_dynamic_token_literal_repair_checkpoint_v1"
POLICY_NAME = "dynamic_token_literal_payload_repair_shadow_v1"
POLICY_STATUS = "shadow"
AGENT_KEY = "micro_dynamic_ck3_expression"
SUBPOLICY_NAME = "dynamic_token_literal_payload_repair"
CHECKPOINT_ACTION = "stage_dynamic_token_literal_payload_repair_shadow"

ELIGIBLE_BUCKETS = {
    "dynamic_token_literal_residual",
    "semantic_dynamic_literal_residual",
    "all_mature_high_issue",
}

ELIGIBLE_ISSUE_KINDS = {
    "select_cstring_expression",
    "culture_context_policy",
}

AUXILIARY_REWRITE_MARKERS = (
    "'has', 'ha'",
    "'te has', 'se ha'",
    "'recibiste', 'recibió'",
    "'abatiste', 'abatió'",
    "'te pasas', 'se pasa'",
    "'sueles', 'suele'",
    "'ajustaste', 'ajustó'",
    "'seguiste', 'siguió'",
)

DISALLOWED_REPAIR_REASONS = (
    "text:convierte->converte",
)

SELECT_CSTRING_RE = re.compile(
    r"Select_CString\(\s*([^,]+?)\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)",
    re.IGNORECASE,
)
CONCEPT_LABEL_RE = re.compile(
    r"Concept\(\s*'([^']+)'\s*,\s*'([^']*)'\s*\)",
    re.IGNORECASE,
)


def latest_decision_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT run.id
        FROM ml_issue_review_decision_runs run
        JOIN ml_issue_review_decisions decision ON decision.run_id = run.id
        WHERE run.finished_at IS NOT NULL
          AND (
            decision.queue_bucket IN ('dynamic_token_literal_residual', 'semantic_dynamic_literal_residual')
            OR (
              decision.queue_bucket = 'all_mature_high_issue'
              AND decision.issue_kind IN ('select_cstring_expression', 'culture_context_policy')
            )
          )
          AND decision.valid = 1
          AND decision.validation_status = 'accepted'
        ORDER BY run.id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No governed dynamic token literal decision run found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_dynamic_token_literal_repair_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            decision_run_id INTEGER NOT NULL,
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
        CREATE TABLE IF NOT EXISTS ml_issue_dynamic_token_literal_repair_checkpoint_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
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
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_dynamic_token_literal_repair_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], decision_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_dynamic_token_literal_repair_checkpoint_decision_run_{decision_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_rows(conn, *, decision_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            decision.id AS decision_id,
            decision.queue_item_id,
            decision.ledger_item_id,
            decision.segment_id,
            decision.relative_path,
            decision.source_key,
            decision.source_line_number,
            decision.agent_key,
            decision.issue_family,
            decision.issue_kind,
            decision.queue_bucket,
            decision.normalized_decision,
            decision.corrected_text AS reviewed_corrected_text,
            decision.notes,
            queue.confirmed_text,
            queue.evidence_text,
            queue.english_text,
            queue.spanish_text
        FROM ml_issue_review_decisions decision
        JOIN ml_issue_review_queue_items queue ON queue.id = decision.queue_item_id
        WHERE decision.run_id = ?
          AND decision.valid = 1
          AND decision.validation_status = 'accepted'
          AND (
            decision.queue_bucket IN ('dynamic_token_literal_residual', 'semantic_dynamic_literal_residual')
            OR (
              decision.queue_bucket = 'all_mature_high_issue'
              AND decision.issue_kind IN ('select_cstring_expression', 'culture_context_policy')
            )
          )
        ORDER BY decision.id
        """,
        (decision_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def literal_payload_skeleton(text: str) -> str:
    def replace_select(match: re.Match[str]) -> str:
        condition = " ".join(match.group(1).split())
        return f"Select_CString({condition},'<lit>','<lit>')"

    def replace_concept(match: re.Match[str]) -> str:
        concept_key = match.group(1)
        return f"Concept('{concept_key}','<lit>')"

    text = SELECT_CSTRING_RE.sub(replace_select, text)
    text = CONCEPT_LABEL_RE.sub(replace_concept, text)
    return text


def dynamic_payload_changes_only(current: str, corrected: str) -> bool:
    if structural_tokens(literal_payload_skeleton(current)) != structural_tokens(literal_payload_skeleton(corrected)):
        return False
    return structural_tokens(current) != structural_tokens(corrected)


def row_is_eligible(row: dict[str, Any]) -> bool:
    bucket = row.get("queue_bucket")
    if bucket in {"dynamic_token_literal_residual", "semantic_dynamic_literal_residual"}:
        return True
    if bucket == "all_mature_high_issue" and row.get("issue_kind") in ELIGIBLE_ISSUE_KINDS:
        return True
    return False


def introduces_lossy_question_marker(current: str, corrected: str) -> bool:
    return "?" in corrected and "?" not in current


def uses_disallowed_repair_reason(reasons: list[str]) -> bool:
    return any(
        reason.startswith(disallowed)
        for reason in reasons
        for disallowed in DISALLOWED_REPAIR_REASONS
    )


def classify(row: dict[str, Any]) -> tuple[int, str, str, str, list[str]]:
    current = row.get("confirmed_text") or row.get("evidence_text") or ""
    reviewed_corrected = (row.get("reviewed_corrected_text") or "").strip()
    proposed, reasons = propose_repair(current)
    corrected = reviewed_corrected or proposed

    if not row_is_eligible(row):
        return 0, "queue_bucket_not_dynamic_token_literal_residual", "not_applicable", "", reasons
    if row.get("normalized_decision") not in {"needs_new_microagent", "needs_repair"}:
        return 0, "decision_not_repair_or_microagent", "not_applicable", "", reasons
    if not current.strip():
        return 0, "missing_current_text", "missing_text", "", reasons
    if any(marker in current for marker in AUXILIARY_REWRITE_MARKERS):
        return 0, "auxiliary_select_cstring_requires_sentence_rewrite_review", "sentence_rewrite_review_required", "", reasons
    if not corrected.strip() or corrected == current:
        return 0, "no_repair_proposal", "no_text_delta", "", reasons
    if introduces_lossy_question_marker(current, corrected):
        return 0, "lossy_question_marker_after_repair", "encoding_review_required", corrected, reasons
    if uses_disallowed_repair_reason(reasons):
        return 0, "partial_global_replacement_requires_literal_pair_review", "literal_pair_review_required", corrected, reasons
    if residual_hits(corrected):
        return (
            0,
            "residual_after_repair:" + ",".join(residual_hits(corrected)[:6]),
            "residual_after_repair",
            corrected,
            reasons,
        )
    if structural_tokens(current) == structural_tokens(corrected):
        return 1, "", "same_structural_tokens", corrected, reasons
    if dynamic_payload_changes_only(current, corrected):
        return 1, "", "dynamic_literal_payload_only", corrected, reasons
    return 0, "structural_tokens_changed_outside_dynamic_literal_payload", "structural_token_change_review_required", corrected, reasons


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    decision_run_id: int,
    rows: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    fields = [
        "checkpoint_allowed",
        "block_reason",
        "decision_id",
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "issue_family",
        "queue_bucket",
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
        "Issue dynamic token literal repair checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Decision run id: {decision_run_id}",
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
                    f"{row['relative_path']}::{row['source_key']} family={row['issue_family']}"
                ),
                f"  current: {short(row['current_text'], 240)}",
                f"  corrected: {short(row['corrected_text'], 240)}",
                f"  reasons: {', '.join(row['reasons'])}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This checkpoint records shadow repair evidence for dynamic literal payloads only.",
            "- It may allow token text changes only inside Select_CString/Concept literal payloads.",
            "- It does not write source/output and does not apply the correction.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, decision_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_decision_run_id = decision_run_id or latest_decision_run_id(conn)
        source_rows = fetch_rows(conn, decision_run_id=selected_decision_run_id)
        classified: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        for source in source_rows:
            allowed, block_reason, token_status, corrected, reasons = classify(source)
            counts["allowed" if allowed else "blocked"] += 1
            counts[f"token:{token_status}"] += 1
            if block_reason:
                counts[f"block:{block_reason}"] += 1
            current = source.get("confirmed_text") or source.get("evidence_text") or ""
            classified.append(
                {
                    "decision_id": source["decision_id"],
                    "queue_item_id": source["queue_item_id"],
                    "ledger_item_id": source["ledger_item_id"],
                    "segment_id": source["segment_id"],
                    "relative_path": source["relative_path"],
                    "source_key": source["source_key"],
                    "source_line_number": source.get("source_line_number"),
                    "issue_family": source["issue_family"],
                    "agent_key": AGENT_KEY,
                    "subpolicy_name": SUBPOLICY_NAME,
                    "queue_bucket": source["queue_bucket"],
                    "checkpoint_allowed": allowed,
                    "checkpoint_action": CHECKPOINT_ACTION,
                    "block_reason": block_reason,
                    "token_status": token_status,
                    "current_text": current,
                    "corrected_text": corrected,
                    "reasons": reasons,
                    "notes": source.get("notes") or "",
                }
            )
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_decision_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_dynamic_token_literal_repair_checkpoint_runs (
                rule_version,
                policy_name,
                policy_status,
                decision_run_id,
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
                selected_decision_run_id,
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
                INSERT INTO ml_issue_dynamic_token_literal_repair_checkpoint_items (
                    run_id,
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
                    notes,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_decision_run_id,
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
                    row["notes"],
                    now,
                ),
            )
        conn.commit()
    write_reports(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        run_id=run_id,
        decision_run_id=selected_decision_run_id,
        rows=classified,
        counts=counts,
    )
    print("[issue_dynamic_token_literal_repair_checkpoint] Checkpoint generated")
    print(f"[issue_dynamic_token_literal_repair_checkpoint] Run id: {run_id}")
    print(f"[issue_dynamic_token_literal_repair_checkpoint] Decision run id: {selected_decision_run_id}")
    print(f"[issue_dynamic_token_literal_repair_checkpoint] Candidates: {len(classified)}")
    print(f"[issue_dynamic_token_literal_repair_checkpoint] Allowed: {counts['allowed']}")
    print(f"[issue_dynamic_token_literal_repair_checkpoint] Blocked: {counts['blocked']}")
    print(f"[issue_dynamic_token_literal_repair_checkpoint] Report: {txt_path}")
    return {
        "run_id": run_id,
        "decision_run_id": selected_decision_run_id,
        "candidate_count": len(classified),
        "allowed_count": counts["allowed"],
        "blocked_count": counts["blocked"],
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Checkpoint governed dynamic literal repairs inside CK3 token payloads.")
    parser.add_argument("--decision-run-id", type=int, default=None)
    args = parser.parse_args()
    main(decision_run_id=args.decision_run_id)
