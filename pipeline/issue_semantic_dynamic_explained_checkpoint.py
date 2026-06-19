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


RULE_VERSION = "issue_semantic_dynamic_explained_checkpoint_v1"
POLICY_NAME = "semantic_review_explained_by_dynamic_pattern_v1"
POLICY_STATUS = "shadow"
AGENT_KEY = "micro_semantic_review_router"
CHECKPOINT_ACTION = "clear_semantic_review_after_dynamic_pattern_shadow"

SAFE_DYNAMIC_SUBPOLICIES = {
    "dynamic_interaction_haggler_aptitude_value_line",
    "dynamic_single_combat_enthusiastic_onslaught",
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
        CREATE TABLE IF NOT EXISTS ml_issue_semantic_dynamic_explained_runs (
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
        CREATE TABLE IF NOT EXISTS ml_issue_semantic_dynamic_explained_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            partial_coverage_run_id INTEGER NOT NULL,
            partial_coverage_item_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            dynamic_ledger_item_id INTEGER NOT NULL DEFAULT 0,
            dynamic_checkpoint_run_id INTEGER NOT NULL DEFAULT 0,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            dynamic_subpolicy_name TEXT,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            checkpoint_action TEXT NOT NULL,
            block_reason TEXT,
            total_issue_count INTEGER NOT NULL DEFAULT 0,
            covered_issue_count INTEGER NOT NULL DEFAULT 0,
            blocked_issue_count INTEGER NOT NULL DEFAULT 0,
            open_issue_count INTEGER NOT NULL DEFAULT 0,
            open_families_json TEXT,
            covered_families_json TEXT,
            english_text TEXT,
            spanish_text TEXT,
            current_text TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_semantic_dynamic_explained_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], partial_coverage_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_semantic_dynamic_explained_checkpoint_run_{partial_coverage_run_id}"
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
        SELECT
            pci.*,
            seg.english_text,
            seg.spanish_text,
            conf.confirmed_text AS current_text
        FROM ml_issue_partial_coverage_items pci
        JOIN source_segments seg ON seg.id = pci.segment_id
        LEFT JOIN segment_confirmations conf
          ON conf.id = (
              SELECT c2.id
              FROM segment_confirmations c2
              WHERE c2.segment_id = pci.segment_id
              ORDER BY c2.updated_at DESC, c2.id DESC
              LIMIT 1
          )
        WHERE pci.run_id = ?
          AND pci.coverage_state = 'partial'
        ORDER BY pci.segment_id, pci.id
        """,
        (partial_coverage_run_id,),
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        open_families = parse_json_dict(payload.get("open_families_json"))
        if int(open_families.get("semantic_review_router") or 0) > 0:
            candidates.append(payload)
    return ledger_run_id, candidates


def fetch_semantic_ledger_item(conn, *, ledger_run_id: int, segment_id: int) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND segment_id = ?
          AND issue_family = 'semantic_review_router'
        ORDER BY id
        LIMIT 1
        """,
        (ledger_run_id, segment_id),
    ).fetchone()
    return int(row["id"]) if row else None


def fetch_dynamic_checkpoint_evidence(conn, *, ledger_run_id: int, segment_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
            item.ledger_item_id AS dynamic_ledger_item_id,
            item.checkpoint_run_id AS dynamic_checkpoint_run_id,
            item.subpolicy_name AS dynamic_subpolicy_name,
            item.checkpoint_action AS dynamic_checkpoint_action,
            item.block_reason AS dynamic_block_reason,
            run.finished_at AS dynamic_finished_at
        FROM ml_issue_dynamic_ck3_pattern_checkpoint_items item
        JOIN ml_issue_dynamic_ck3_pattern_checkpoint_runs run
          ON item.checkpoint_run_id = run.id
        JOIN ml_issue_ledger_items ledger ON ledger.id = item.ledger_item_id
        WHERE run.ledger_run_id = ?
          AND item.segment_id = ?
          AND ledger.issue_family = 'dynamic_ck3_expression_microagent'
          AND item.checkpoint_allowed = 1
          AND run.finished_at IS NOT NULL
        ORDER BY run.finished_at DESC, run.id DESC, item.id DESC
        LIMIT 1
        """,
        (ledger_run_id, segment_id),
    ).fetchone()
    return dict(row) if row else None


def classify(
    row: dict[str, Any],
    *,
    ledger_item_id: int | None,
    dynamic_evidence: dict[str, Any] | None,
) -> tuple[int, str, str]:
    if ledger_item_id is None:
        return 0, "missing_semantic_ledger_item", "semantic_dynamic_unclassified"

    open_families = parse_json_dict(row.get("open_families_json"))
    if int(open_families.get("semantic_review_router") or 0) <= 0:
        return 0, "semantic_review_not_open", "semantic_dynamic_unclassified"

    if int(row.get("blocked_issue_count") or 0) != 0:
        return 0, "segment_has_blocked_issue_items", "semantic_dynamic_unclassified"

    if dynamic_evidence is None:
        return 0, "missing_dynamic_pattern_checkpoint_evidence", "semantic_dynamic_unclassified"

    dynamic_subpolicy = dynamic_evidence.get("dynamic_subpolicy_name") or ""
    if dynamic_subpolicy not in SAFE_DYNAMIC_SUBPOLICIES:
        return 0, "dynamic_subpolicy_not_safe_for_semantic_clear", dynamic_subpolicy

    if not (row.get("current_text") or "").strip():
        return 0, "missing_current_text", dynamic_subpolicy

    return 1, "", dynamic_subpolicy


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
        "dynamic_ledger_item_id",
        "dynamic_checkpoint_run_id",
        "segment_id",
        "relative_path",
        "source_key",
        "subpolicy_name",
        "dynamic_subpolicy_name",
        "total_issue_count",
        "covered_issue_count",
        "blocked_issue_count",
        "open_issue_count",
        "current_text",
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
        "Issue semantic dynamic explained checkpoint",
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
        "Allowed dynamic subpolicies:",
        *[f"- {key}: {value:,}" for key, value in counts.items() if key.startswith("dynamic:")],
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
                    f"- allowed={row['checkpoint_allowed']} block={row['block_reason'] or 'none'} "
                    f"dynamic={row['dynamic_subpolicy_name'] or 'none'} segment={row['segment_id']} "
                    f"{row['relative_path']}::{row['source_key']}"
                ),
                f"  current: {short(row.get('current_text'), 220)}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This checkpoint clears only the generic semantic-review router item.",
            "- It requires a narrow dynamic-pattern checkpoint for the same segment.",
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
            segment_id = int(source["segment_id"])
            ledger_item_id = fetch_semantic_ledger_item(
                conn,
                ledger_run_id=ledger_run_id,
                segment_id=segment_id,
            )
            dynamic_evidence = fetch_dynamic_checkpoint_evidence(
                conn,
                ledger_run_id=ledger_run_id,
                segment_id=segment_id,
            )
            allowed, block_reason, subpolicy_name = classify(
                source,
                ledger_item_id=ledger_item_id,
                dynamic_evidence=dynamic_evidence,
            )
            counts["allowed" if allowed else "blocked"] += 1
            if allowed:
                counts[f"dynamic:{subpolicy_name}"] += 1
            if block_reason:
                counts[f"block:{block_reason}"] += 1
            classified.append(
                {
                    "partial_coverage_item_id": source["id"],
                    "ledger_item_id": ledger_item_id or 0,
                    "dynamic_ledger_item_id": int((dynamic_evidence or {}).get("dynamic_ledger_item_id") or 0),
                    "dynamic_checkpoint_run_id": int(
                        (dynamic_evidence or {}).get("dynamic_checkpoint_run_id") or 0
                    ),
                    "segment_id": segment_id,
                    "relative_path": source["relative_path"],
                    "source_key": source["source_key"],
                    "agent_key": AGENT_KEY,
                    "subpolicy_name": subpolicy_name,
                    "dynamic_subpolicy_name": (dynamic_evidence or {}).get("dynamic_subpolicy_name") or "",
                    "checkpoint_allowed": allowed,
                    "checkpoint_action": CHECKPOINT_ACTION,
                    "block_reason": block_reason,
                    "total_issue_count": source["total_issue_count"],
                    "covered_issue_count": source["covered_issue_count"],
                    "blocked_issue_count": source["blocked_issue_count"],
                    "open_issue_count": source["open_issue_count"],
                    "open_families_json": source.get("open_families_json") or "{}",
                    "covered_families_json": source.get("covered_families_json") or "{}",
                    "english_text": source.get("english_text") or "",
                    "spanish_text": source.get("spanish_text") or "",
                    "current_text": source.get("current_text") or "",
                }
            )

        txt_path, csv_path, jsonl_path = report_paths(settings, selected_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_semantic_dynamic_explained_runs (
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
                POLICY_STATUS,
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
                INSERT INTO ml_issue_semantic_dynamic_explained_items (
                    run_id,
                    partial_coverage_run_id,
                    partial_coverage_item_id,
                    ledger_item_id,
                    dynamic_ledger_item_id,
                    dynamic_checkpoint_run_id,
                    segment_id,
                    relative_path,
                    source_key,
                    agent_key,
                    subpolicy_name,
                    dynamic_subpolicy_name,
                    checkpoint_allowed,
                    checkpoint_action,
                    block_reason,
                    total_issue_count,
                    covered_issue_count,
                    blocked_issue_count,
                    open_issue_count,
                    open_families_json,
                    covered_families_json,
                    english_text,
                    spanish_text,
                    current_text,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_run_id,
                    row["partial_coverage_item_id"],
                    row["ledger_item_id"],
                    row["dynamic_ledger_item_id"],
                    row["dynamic_checkpoint_run_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["agent_key"],
                    row["subpolicy_name"],
                    row["dynamic_subpolicy_name"],
                    row["checkpoint_allowed"],
                    row["checkpoint_action"],
                    row["block_reason"],
                    row["total_issue_count"],
                    row["covered_issue_count"],
                    row["blocked_issue_count"],
                    row["open_issue_count"],
                    row["open_families_json"],
                    row["covered_families_json"],
                    row["english_text"],
                    row["spanish_text"],
                    row["current_text"],
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

    print("[issue_semantic_dynamic_explained_checkpoint] Checkpoint generated")
    print(f"[issue_semantic_dynamic_explained_checkpoint] Rule version: {RULE_VERSION}")
    print(f"[issue_semantic_dynamic_explained_checkpoint] Run id: {run_id}")
    print(f"[issue_semantic_dynamic_explained_checkpoint] Partial coverage run id: {selected_run_id}")
    print(f"[issue_semantic_dynamic_explained_checkpoint] Candidates: {len(classified):,}")
    print(f"[issue_semantic_dynamic_explained_checkpoint] Allowed: {counts['allowed']:,}")
    print(f"[issue_semantic_dynamic_explained_checkpoint] Blocked: {counts['blocked']:,}")
    print(f"[issue_semantic_dynamic_explained_checkpoint] Report: {txt_path}")
    return {
        "run_id": run_id,
        "partial_coverage_run_id": selected_run_id,
        "candidate_count": len(classified),
        "allowed": counts["allowed"],
        "blocked": counts["blocked"],
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Clear semantic-review items explained by safe dynamic CK3 patterns.")
    parser.add_argument("--partial-coverage-run-id", type=int, default=None)
    args = parser.parse_args()
    main(partial_coverage_run_id=args.partial_coverage_run_id)
