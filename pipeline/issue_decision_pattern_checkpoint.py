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


RULE_VERSION = "issue_decision_pattern_checkpoint_v1"
POLICY_NAME = "review_decision_pattern_coverage_v1"
POLICY_STATUS = "checkpoint"

SELECT_CSTRING_RE = re.compile(r"Select_CString\(", re.IGNORECASE)
CUSTOM_ES_RE = re.compile(r"\.Custom\('ES_[A-Za-z]+?'\)")
CUSTOM_GET_RE = re.compile(r"\.Custom\('Get[A-Za-z]+?'\)")
GENDER_SUFFIX_RE = re.compile(r"\w+\[[^\]]+\.Custom\('ES_[A-Za-z]+?'\)\]")
MARKUP_NO_RE = re.compile(r"#(?:EMP|bold) no#!|\bno#!", re.IGNORECASE)
SPANISH_RESIDUAL_RE = re.compile(
    r"\b("
    r"decidiste|decidi[oó]|ganaste|gan[oó]|puedes|puede|has|ha|sois|son|"
    r"encarcelad(?:o|a|os|as)?|te\s+comprometes|se\s+compromete|"
    r"te\s+opones|se\s+opone|te\s+opusiste|se\s+opuso|"
    r"tus|sus|ti|te"
    r")\b",
    re.IGNORECASE,
)

SUPPORTED_DECISIONS = {
    "needs_repair",
    "needs_new_microagent",
    "needs_domain_context",
    "safe_short_label",
}


def latest_partial_coverage_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_partial_coverage_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No partial coverage run found.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_decision_pattern_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            partial_coverage_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            allowed_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            decision_counts_json TEXT,
            subpolicy_counts_json TEXT,
            block_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_decision_pattern_checkpoint_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            partial_coverage_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            decision_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            issue_family TEXT NOT NULL,
            issue_kind TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            normalized_decision TEXT NOT NULL,
            evidence_label TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            pattern_flags_json TEXT,
            evidence_preview TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_decision_pattern_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], partial_coverage_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_decision_pattern_checkpoint_run_{partial_coverage_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_context(conn, *, partial_coverage_run_id: int) -> tuple[int, set[int]]:
    run = conn.execute(
        """
        SELECT ledger_run_id
        FROM ml_issue_partial_coverage_runs
        WHERE id = ?
        """,
        (partial_coverage_run_id,),
    ).fetchone()
    if row := run:
        ledger_run_id = int(row["ledger_run_id"])
    else:
        raise RuntimeError(f"Partial coverage run not found: {partial_coverage_run_id}")
    segment_ids = {
        int(row["segment_id"])
        for row in conn.execute(
            """
            SELECT segment_id
            FROM ml_issue_partial_coverage_items
            WHERE run_id = ?
              AND coverage_state = 'partial'
            """,
            (partial_coverage_run_id,),
        ).fetchall()
    }
    return ledger_run_id, segment_ids


def fetch_latest_decisions(
    conn,
    *,
    ledger_run_id: int,
    segment_ids: set[int],
) -> list[dict[str, Any]]:
    if not segment_ids:
        return []
    placeholders = ",".join("?" for _ in segment_ids)
    rows = conn.execute(
        f"""
        SELECT
            decision.*,
            item.status AS ledger_status,
            item.token_status AS ledger_token_status,
            item.token_impact AS ledger_token_impact,
            item.validation_status AS ledger_validation_status,
            item.evidence_text AS ledger_evidence_text
        FROM ml_issue_review_decisions decision
        JOIN ml_issue_ledger_items item ON item.id = decision.ledger_item_id
        WHERE decision.ledger_run_id = ?
          AND decision.segment_id IN ({placeholders})
          AND decision.valid = 1
          AND decision.validation_status = 'accepted'
        ORDER BY decision.ledger_item_id, decision.created_at DESC, decision.id DESC
        """,
        [ledger_run_id, *sorted(segment_ids)],
    ).fetchall()
    latest_by_ledger_item: dict[int, dict[str, Any]] = {}
    for row in rows:
        payload = dict(row)
        ledger_item_id = int(payload["ledger_item_id"])
        if ledger_item_id not in latest_by_ledger_item:
            latest_by_ledger_item[ledger_item_id] = payload
    return list(latest_by_ledger_item.values())


def pattern_flags(text: str) -> dict[str, int]:
    return {
        "select_cstring": int(bool(SELECT_CSTRING_RE.search(text))),
        "custom_es": int(bool(CUSTOM_ES_RE.search(text))),
        "custom_get": int(bool(CUSTOM_GET_RE.search(text))),
        "gender_suffix": int(bool(GENDER_SUFFIX_RE.search(text))),
        "markup_no_spanish": int(bool(MARKUP_NO_RE.search(text))),
        "spanish_residual": int(bool(SPANISH_RESIDUAL_RE.search(text))),
    }


def classify(row: dict[str, Any]) -> tuple[int, str, str, str, dict[str, int]]:
    decision = row.get("normalized_decision") or ""
    family = row.get("issue_family") or ""
    kind = row.get("issue_kind") or ""
    text = row.get("ledger_evidence_text") or ""
    flags = pattern_flags(text)

    if decision not in SUPPORTED_DECISIONS:
        return 0, "unsupported_decision", "unsupported_decision", "hold_for_manual_review", flags

    if family == "high_issue_auditor":
        return 0, "high_issue_requires_specific_coverage_first", "high_issue_excluded", "hold_high_issue", flags

    if family == "structural_token_gate":
        return 0, "structural_gate_requires_token_policy", "structural_gate_excluded", "hold_structural_gate", flags

    if decision == "safe_short_label":
        if family != "short_label_style_microagent":
            return 0, "safe_short_label_wrong_family", "safe_short_label_positive", "hold_for_manual_review", flags
        return (
            1,
            "",
            "reviewed_safe_short_label_positive",
            "record_reviewed_safe_short_label_positive",
            flags,
        )

    if decision == "needs_repair":
        if flags["select_cstring"] and flags["spanish_residual"]:
            return (
                1,
                "",
                "select_cstring_spanish_literal_repair_required",
                "route_select_cstring_literal_repair",
                flags,
            )
        if flags["markup_no_spanish"]:
            return 1, "", "markup_spanish_no_repair_required", "route_markup_no_repair", flags
        if flags["spanish_residual"]:
            return 1, "", "spanish_residual_repair_required", "route_spanish_residual_repair", flags
        if flags["custom_es"] and flags["gender_suffix"]:
            return 1, "", "custom_gender_suffix_repair_required", "route_gender_suffix_repair", flags
        return 1, "", "reviewed_repair_required", "route_reviewed_repair", flags

    if decision == "needs_new_microagent":
        if flags["select_cstring"] and flags["spanish_residual"]:
            return (
                1,
                "",
                "select_cstring_complex_new_microagent_boundary",
                "route_select_cstring_complex_microagent",
                flags,
            )
        if flags["custom_es"] and flags["gender_suffix"]:
            return (
                1,
                "",
                "custom_gender_suffix_new_microagent_boundary",
                "route_custom_gender_suffix_microagent",
                flags,
            )
        if flags["custom_es"]:
            return (
                1,
                "",
                "custom_gender_helper_new_microagent_boundary",
                "route_custom_gender_helper_microagent",
                flags,
            )
        return 1, "", "reviewed_new_microagent_boundary", "route_reviewed_new_microagent", flags

    if decision == "needs_domain_context":
        if flags["select_cstring"]:
            return (
                1,
                "",
                "select_cstring_domain_context_boundary",
                "route_select_cstring_domain_context",
                flags,
            )
        if flags["custom_es"]:
            return 1, "", "custom_gender_helper_context_boundary", "route_custom_gender_context", flags
        return 1, "", "reviewed_domain_context_boundary", "route_reviewed_domain_context", flags

    return 0, "unclassified_decision", "unclassified_decision", "hold_for_manual_review", flags


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    partial_coverage_run_id: int,
    rows: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    fields = [
        "checkpoint_allowed",
        "block_reason",
        "decision_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "issue_family",
        "issue_kind",
        "normalized_decision",
        "evidence_label",
        "subpolicy_name",
        "checkpoint_action",
        "pattern_flags_json",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Issue decision pattern checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Partial coverage run id: {partial_coverage_run_id}",
        f"Policy: {POLICY_NAME}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Allowed: {counts['allowed']:,}",
        f"- Blocked: {counts['blocked']:,}",
        "",
        "Decisions:",
        *[f"- {key}: {value:,}" for key, value in counts.items() if key.startswith("decision:")],
        "",
        "Allowed subpolicies:",
        *[f"- {key}: {value:,}" for key, value in counts.items() if key.startswith("subpolicy:")],
        "",
        "Blocks:",
        *[f"- {key}: {value:,}" for key, value in counts.items() if key.startswith("block:")],
        "",
        "Allowed samples:",
    ]
    for row in [item for item in rows if item["checkpoint_allowed"]][:60]:
        lines.extend(
            [
                (
                    f"- {row['subpolicy_name']} | {row['relative_path']}::{row['source_key']} "
                    f"decision={row['normalized_decision']}"
                ),
                f"  evidence: {short(row.get('evidence_preview') or '', 180)}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Checkpoint only: consumes accepted human review decisions as reusable issue-pattern coverage.",
            "- It does not write source/output, does not create confirmations, and does not promote production apply.",
            "- A covered issue here means the network knows how to route/classify the issue, not that the segment is production-closed.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, partial_coverage_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_run_id = partial_coverage_run_id or latest_partial_coverage_run_id(conn)
        ledger_run_id, segment_ids = fetch_context(conn, partial_coverage_run_id=selected_run_id)
        source_rows = fetch_latest_decisions(conn, ledger_run_id=ledger_run_id, segment_ids=segment_ids)
        classified: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        for source in source_rows:
            allowed, block_reason, subpolicy_name, action, flags = classify(source)
            counts["allowed" if allowed else "blocked"] += 1
            counts[f"decision:{source['normalized_decision']}"] += 1
            if allowed:
                counts[f"subpolicy:{subpolicy_name}"] += 1
            if block_reason:
                counts[f"block:{block_reason}"] += 1
            classified.append(
                {
                    "decision_id": source["id"],
                    "ledger_item_id": source["ledger_item_id"],
                    "segment_id": source["segment_id"],
                    "relative_path": source["relative_path"],
                    "source_key": source["source_key"],
                    "source_line_number": source["source_line_number"],
                    "issue_family": source["issue_family"],
                    "issue_kind": source["issue_kind"],
                    "agent_key": source["agent_key"],
                    "normalized_decision": source["normalized_decision"],
                    "evidence_label": source["evidence_label"],
                    "subpolicy_name": subpolicy_name,
                    "checkpoint_action": action,
                    "checkpoint_allowed": allowed,
                    "block_reason": block_reason,
                    "pattern_flags_json": json.dumps(flags, ensure_ascii=False, sort_keys=True),
                    "evidence_preview": source.get("ledger_evidence_text") or "",
                }
            )

        txt_path, csv_path, jsonl_path = report_paths(settings, selected_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_decision_pattern_checkpoint_runs (
                rule_version,
                policy_name,
                policy_status,
                partial_coverage_run_id,
                ledger_run_id,
                candidate_count,
                allowed_count,
                blocked_count,
                decision_counts_json,
                subpolicy_counts_json,
                block_counts_json,
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
                POLICY_NAME,
                POLICY_STATUS,
                selected_run_id,
                ledger_run_id,
                len(classified),
                counts["allowed"],
                counts["blocked"],
                json.dumps(
                    {key.removeprefix("decision:"): value for key, value in counts.items() if key.startswith("decision:")},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                json.dumps(
                    {key.removeprefix("subpolicy:"): value for key, value in counts.items() if key.startswith("subpolicy:")},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                json.dumps(
                    {key.removeprefix("block:"): value for key, value in counts.items() if key.startswith("block:")},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
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
                INSERT INTO ml_issue_decision_pattern_checkpoint_items (
                    run_id,
                    partial_coverage_run_id,
                    ledger_run_id,
                    decision_id,
                    ledger_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    issue_family,
                    issue_kind,
                    agent_key,
                    normalized_decision,
                    evidence_label,
                    subpolicy_name,
                    checkpoint_action,
                    checkpoint_allowed,
                    block_reason,
                    pattern_flags_json,
                    evidence_preview,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_run_id,
                    ledger_run_id,
                    row["decision_id"],
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["issue_family"],
                    row["issue_kind"],
                    row["agent_key"],
                    row["normalized_decision"],
                    row["evidence_label"],
                    row["subpolicy_name"],
                    row["checkpoint_action"],
                    row["checkpoint_allowed"],
                    row["block_reason"],
                    row["pattern_flags_json"],
                    row["evidence_preview"],
                    now,
                ),
            )
        conn.commit()
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            partial_coverage_run_id=selected_run_id,
            rows=classified,
            counts=counts,
        )

    print("[issue_decision_pattern_checkpoint] Checkpoint generated")
    print(f"[issue_decision_pattern_checkpoint] Run id: {run_id}")
    print(f"[issue_decision_pattern_checkpoint] Partial coverage run id: {selected_run_id}")
    print(f"[issue_decision_pattern_checkpoint] Candidates: {len(classified):,}")
    print(f"[issue_decision_pattern_checkpoint] Allowed: {counts['allowed']:,}")
    print(f"[issue_decision_pattern_checkpoint] Blocked: {counts['blocked']:,}")
    print(f"[issue_decision_pattern_checkpoint] Report: {txt_path}")
    return {
        "run_id": run_id,
        "partial_coverage_run_id": selected_run_id,
        "candidate_count": len(classified),
        "allowed_count": counts["allowed"],
        "blocked_count": counts["blocked"],
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Convert accepted issue-review decisions into reusable pattern checkpoint coverage."
    )
    parser.add_argument("--partial-coverage-run-id", type=int, default=None)
    args = parser.parse_args()
    main(partial_coverage_run_id=args.partial_coverage_run_id)
