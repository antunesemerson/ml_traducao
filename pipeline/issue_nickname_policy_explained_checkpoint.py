from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_nickname_policy_explained_checkpoint_v1"
POLICY_NAME = "nickname_policy_explained_by_specific_coverage_v1"
AGENT_KEY = "micro_nickname_name_policy"
SUBPOLICY_NAME = "nickname_desc_context_explained_by_micro_coverage"
CHECKPOINT_ACTION = "clear_nickname_context_after_specific_issues_covered_shadow"

ALLOWED_PATH = "nicknames_l_spanish.yml"
REQUIRED_COVERED_FAMILIES = {
    "gender_token_microagent",
    "short_label_style_microagent",
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
        CREATE TABLE IF NOT EXISTS ml_issue_nickname_policy_explained_runs (
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
        CREATE TABLE IF NOT EXISTS ml_issue_nickname_policy_explained_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            partial_coverage_run_id INTEGER NOT NULL,
            partial_coverage_item_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
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
            open_families_json TEXT,
            coverage_sources_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_nickname_policy_explained_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], partial_coverage_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_nickname_policy_explained_checkpoint_run_{partial_coverage_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def parse_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


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
        if "nickname_name_policy" in open_families:
            candidates.append(payload)
    return ledger_run_id, candidates


def fetch_nickname_ledger_item(conn, *, ledger_run_id: int, segment_id: int) -> tuple[int | None, str]:
    row = conn.execute(
        """
        SELECT id, evidence_text
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND segment_id = ?
          AND issue_family = 'nickname_name_policy'
        ORDER BY id
        LIMIT 1
        """,
        (ledger_run_id, segment_id),
    ).fetchone()
    if row is None:
        return None, ""
    return int(row["id"]), row["evidence_text"] or ""


def classify(row: dict[str, Any], *, ledger_item_id: int | None, evidence_text: str) -> tuple[int, str]:
    if ledger_item_id is None:
        return 0, "missing_nickname_policy_ledger_item"
    if row.get("relative_path") != ALLOWED_PATH:
        return 0, "path_not_nicknames"
    source_key = str(row.get("source_key") or "")
    if not source_key.startswith("nick_"):
        return 0, "key_not_nickname"
    if not source_key.endswith("_desc"):
        return 0, "not_nickname_description"
    if int(row.get("covered_issue_count") or 0) < 2:
        return 0, "not_enough_specific_coverage"
    open_families = parse_json_dict(row.get("open_families_json"))
    if "nickname_name_policy" not in open_families:
        return 0, "nickname_policy_not_open"
    covered_families = parse_json_dict(row.get("covered_families_json"))
    if not REQUIRED_COVERED_FAMILIES.issubset(set(covered_families)):
        return 0, "missing_required_micro_coverage"
    if len(evidence_text) > 420:
        return 0, "nickname_desc_too_long_for_context_clear"
    if "[CHARACTER.GetShortUINameNoTooltipNoFormat" not in evidence_text:
        return 0, "missing_character_nickname_subject"
    if "Select_CString" not in evidence_text and "CHARACTER.Custom('ES_" not in evidence_text:
        return 0, "missing_dynamic_nickname_context"
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
        "total_issue_count",
        "covered_issue_count",
        "blocked_issue_count",
        "open_issue_count",
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
        "Issue nickname-policy explained checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Partial coverage run id: {partial_coverage_run_id}",
        f"Policy: {POLICY_NAME}",
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
        lines.append(
            (
                f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']} "
                f"covered={row['covered_issue_count']}/{row['total_issue_count']}"
            )
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This checkpoint clears only the generic nickname-name context item.",
            "- It requires nickname description scope plus existing micro-coverage evidence.",
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
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_run_id)
        classified: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        now = datetime.now().isoformat(timespec="seconds")
        for source in source_rows:
            ledger_item_id, evidence_text = fetch_nickname_ledger_item(
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
                    "agent_key": AGENT_KEY,
                    "subpolicy_name": SUBPOLICY_NAME,
                    "checkpoint_allowed": allowed,
                    "checkpoint_action": CHECKPOINT_ACTION,
                    "block_reason": block_reason,
                    "total_issue_count": source["total_issue_count"],
                    "covered_issue_count": source["covered_issue_count"],
                    "blocked_issue_count": source["blocked_issue_count"],
                    "open_issue_count": source["open_issue_count"],
                    "covered_families_json": source.get("covered_families_json"),
                    "open_families_json": source.get("open_families_json"),
                    "coverage_sources_json": source.get("coverage_sources_json"),
                    "created_at": now,
                }
            )

        cursor = conn.execute(
            """
            INSERT INTO ml_issue_nickname_policy_explained_runs (
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
                "shadow_checkpoint",
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
        run_id = int(cursor.lastrowid)
        for row in classified:
            conn.execute(
                """
                INSERT INTO ml_issue_nickname_policy_explained_items (
                    run_id,
                    partial_coverage_run_id,
                    partial_coverage_item_id,
                    ledger_item_id,
                    segment_id,
                    relative_path,
                    source_key,
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
                    open_families_json,
                    coverage_sources_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_run_id,
                    row["partial_coverage_item_id"],
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
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
                    row["open_families_json"],
                    row["coverage_sources_json"],
                    row["created_at"],
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
    print("[issue_nickname_policy_explained_checkpoint] Checkpoint generated")
    print(f"[issue_nickname_policy_explained_checkpoint] Rule version: {RULE_VERSION}")
    print(f"[issue_nickname_policy_explained_checkpoint] Run id: {run_id}")
    print(f"[issue_nickname_policy_explained_checkpoint] Partial coverage run id: {selected_run_id}")
    print(f"[issue_nickname_policy_explained_checkpoint] Candidates: {len(classified):,}")
    print(f"[issue_nickname_policy_explained_checkpoint] Allowed: {counts['allowed']:,}")
    print(f"[issue_nickname_policy_explained_checkpoint] Blocked: {counts['blocked']:,}")
    print(f"[issue_nickname_policy_explained_checkpoint] Report: {txt_path}")
    return {
        "run_id": run_id,
        "partial_coverage_run_id": selected_run_id,
        "candidate_count": len(classified),
        "allowed_count": counts["allowed"],
        "blocked_count": counts["blocked"],
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Clear nickname-name context items explained by specific issue coverage.")
    parser.add_argument("--partial-coverage-run-id", type=int, default=None)
    args = parser.parse_args()
    main(partial_coverage_run_id=args.partial_coverage_run_id)
