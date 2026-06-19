from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "issue_nickname_select_residual_checkpoint_v1"
POLICY_NAME = "nickname_select_residual_explained_by_specific_coverage_v1"
AGENT_KEY = "nickname_residual_spanish_select_cstring_boundary"
ISSUE_FAMILY = "nickname_select_cstring_spanish_residual_microagent"
SUBPOLICY_NAME = "nickname_select_cstring_residual_explained"
CHECKPOINT_ACTION = "clear_nickname_select_residual_after_specific_coverage_shadow"

REQUIRED_COVERED_FAMILIES = {
    "dynamic_ck3_expression_microagent",
    "gender_token_microagent",
    "nickname_name_policy",
    "short_label_style_microagent",
    "spanish_residual_microagent",
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


def parse_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_nickname_select_residual_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            partial_coverage_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
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
        CREATE TABLE IF NOT EXISTS ml_issue_nickname_select_residual_checkpoint_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            partial_coverage_run_id INTEGER NOT NULL,
            partial_coverage_item_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            issue_family TEXT NOT NULL,
            issue_kind TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            checkpoint_action TEXT NOT NULL,
            block_reason TEXT,
            total_issue_count INTEGER NOT NULL DEFAULT 0,
            covered_issue_count INTEGER NOT NULL DEFAULT 0,
            blocked_issue_count INTEGER NOT NULL DEFAULT 0,
            open_issue_count INTEGER NOT NULL DEFAULT 0,
            covered_families_json TEXT,
            evidence_preview TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_nickname_select_residual_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], partial_coverage_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_nickname_select_residual_checkpoint_run_{partial_coverage_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_candidates(conn, *, partial_coverage_run_id: int) -> tuple[int, list[dict[str, Any]]]:
    run = conn.execute(
        "SELECT ledger_run_id FROM ml_issue_partial_coverage_runs WHERE id = ?",
        (partial_coverage_run_id,),
    ).fetchone()
    if run is None:
        raise RuntimeError(f"Partial coverage run not found: {partial_coverage_run_id}")
    ledger_run_id = int(run["ledger_run_id"])
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_partial_coverage_items
        WHERE run_id = ?
          AND coverage_state = 'partial'
        ORDER BY segment_id, id
        """,
        (partial_coverage_run_id,),
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        open_families = parse_json_dict(payload.get("open_families_json"))
        remaining = remaining_families(payload)
        if ISSUE_FAMILY in open_families or ISSUE_FAMILY in remaining:
            candidates.append(payload)
    return ledger_run_id, candidates


def remaining_families(row: dict[str, Any]) -> dict[str, int]:
    issue_families = parse_json_dict(row.get("issue_families_json"))
    covered_families = parse_json_dict(row.get("covered_families_json"))
    remaining: dict[str, int] = {}
    for family, total in issue_families.items():
        count = int(total or 0) - int(covered_families.get(family) or 0)
        if count > 0:
            remaining[str(family)] = count
    return remaining


def fetch_ledger_item(conn, *, ledger_run_id: int, segment_id: int) -> tuple[int | None, str, str]:
    row = conn.execute(
        """
        SELECT id, issue_kind, evidence_text
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND segment_id = ?
          AND issue_family = ?
        ORDER BY id
        LIMIT 1
        """,
        (ledger_run_id, segment_id, ISSUE_FAMILY),
    ).fetchone()
    if row is None:
        return None, "", ""
    return int(row["id"]), row["issue_kind"] or "", row["evidence_text"] or ""


def classify(row: dict[str, Any], *, ledger_item_id: int | None, evidence_text: str) -> tuple[int, str]:
    if ledger_item_id is None:
        return 0, "missing_nickname_select_residual_ledger_item"
    if row.get("relative_path") != "nicknames_l_spanish.yml":
        return 0, "path_not_nicknames"
    source_key = row.get("source_key") or ""
    if not source_key.startswith("nick_") or not source_key.endswith("_desc"):
        return 0, "not_nickname_description"
    if "Select_CString" not in evidence_text:
        return 0, "missing_select_cstring"
    if not any(hit in evidence_text for hit in ("te comprometes", "se compromete", "puedes", "puede", "hablas", "habla", "'tus'", "'sus'")):
        return 0, "missing_known_spanish_select_literal"
    covered_families = parse_json_dict(row.get("covered_families_json"))
    missing = sorted(REQUIRED_COVERED_FAMILIES - set(covered_families))
    if missing:
        return 0, "missing_required_coverage:" + ",".join(missing)
    if int(row.get("covered_issue_count") or 0) < 5:
        return 0, "not_enough_specific_coverage"
    return 1, ""


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
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "issue_family",
        "issue_kind",
        "subpolicy_name",
        "total_issue_count",
        "covered_issue_count",
        "blocked_issue_count",
        "open_issue_count",
        "covered_families_json",
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
        "Issue nickname Select_CString residual checkpoint",
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
        "Blocks:",
        *[f"- {key}: {value:,}" for key, value in counts.items() if key.startswith("block:")],
        "",
        "Allowed samples:",
    ]
    for row in [item for item in rows if item["checkpoint_allowed"]][:40]:
        lines.extend(
            [
                (
                    f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']} "
                    f"covered={row['covered_issue_count']}/{row['total_issue_count']}"
                ),
                f"  evidence: {short(row.get('evidence_preview'), 220)}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This checkpoint clears only the nickname Select_CString residual guard.",
            "- It requires existing dynamic, residual, gender, short-label and nickname policy coverage.",
            "- It does not write source/output and does not promote production apply by itself.",
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
        ledger_run_id, source_rows = fetch_candidates(conn, partial_coverage_run_id=selected_run_id)
        classified: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        for source in source_rows:
            ledger_item_id, issue_kind, evidence_text = fetch_ledger_item(
                conn,
                ledger_run_id=ledger_run_id,
                segment_id=int(source["segment_id"]),
            )
            allowed, block_reason = classify(source, ledger_item_id=ledger_item_id, evidence_text=evidence_text)
            counts["allowed" if allowed else "blocked"] += 1
            if block_reason:
                counts[f"block:{block_reason}"] += 1
            classified.append(
                {
                    "partial_coverage_item_id": source["id"],
                    "ledger_item_id": ledger_item_id or 0,
                    "segment_id": source["segment_id"],
                    "relative_path": source["relative_path"],
                    "source_key": source["source_key"],
                    "source_line_number": source["source_line_number"],
                    "issue_family": ISSUE_FAMILY,
                    "issue_kind": issue_kind or "nickname_select_cstring_spanish_residual",
                    "agent_key": AGENT_KEY,
                    "subpolicy_name": SUBPOLICY_NAME,
                    "checkpoint_allowed": allowed,
                    "checkpoint_action": CHECKPOINT_ACTION,
                    "block_reason": block_reason,
                    "total_issue_count": source["total_issue_count"],
                    "covered_issue_count": source["covered_issue_count"],
                    "blocked_issue_count": source["blocked_issue_count"],
                    "open_issue_count": source["open_issue_count"],
                    "covered_families_json": source.get("covered_families_json") or "{}",
                    "evidence_preview": evidence_text,
                }
            )

        txt_path, csv_path, jsonl_path = report_paths(settings, selected_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_nickname_select_residual_checkpoint_runs (
                rule_version,
                policy_name,
                policy_status,
                partial_coverage_run_id,
                ledger_run_id,
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                POLICY_NAME,
                "shadow",
                selected_run_id,
                ledger_run_id,
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
                INSERT INTO ml_issue_nickname_select_residual_checkpoint_items (
                    run_id,
                    partial_coverage_run_id,
                    partial_coverage_item_id,
                    ledger_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    issue_family,
                    issue_kind,
                    agent_key,
                    subpolicy_name,
                    checkpoint_allowed,
                    checkpoint_action,
                    block_reason,
                    total_issue_count,
                    covered_issue_count,
                    blocked_issue_count,
                    open_issue_count,
                    covered_families_json,
                    evidence_preview,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_run_id,
                    row["partial_coverage_item_id"],
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["issue_family"],
                    row["issue_kind"],
                    row["agent_key"],
                    row["subpolicy_name"],
                    row["checkpoint_allowed"],
                    row["checkpoint_action"],
                    row["block_reason"],
                    row["total_issue_count"],
                    row["covered_issue_count"],
                    row["blocked_issue_count"],
                    row["open_issue_count"],
                    row["covered_families_json"],
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

    print("[issue_nickname_select_residual_checkpoint] Checkpoint generated")
    print(f"[issue_nickname_select_residual_checkpoint] Run id: {run_id}")
    print(f"[issue_nickname_select_residual_checkpoint] Partial coverage run id: {selected_run_id}")
    print(f"[issue_nickname_select_residual_checkpoint] Candidates: {len(classified):,}")
    print(f"[issue_nickname_select_residual_checkpoint] Allowed: {counts['allowed']:,}")
    print(f"[issue_nickname_select_residual_checkpoint] Blocked: {counts['blocked']:,}")
    print(f"[issue_nickname_select_residual_checkpoint] Report: {txt_path}")
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
        description="Checkpoint nickname Select_CString Spanish residual guards after specific coverage."
    )
    parser.add_argument("--partial-coverage-run-id", type=int, default=None)
    args = parser.parse_args()
    main(partial_coverage_run_id=args.partial_coverage_run_id)
