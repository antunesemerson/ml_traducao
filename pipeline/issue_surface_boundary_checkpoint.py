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
from issue_dynamic_literal_repair_diagnostic import residual_hits


RULE_VERSION = "issue_surface_boundary_checkpoint_v1"
POLICY_NAME = "surface_boundary_safe_or_false_positive_v1"
POLICY_STATUS = "shadow"
AGENT_KEY = "micro_surface_boundary"
CHECKPOINT_ACTION = "stage_surface_boundary_shadow"

SUBPOLICY_SPACE_BEFORE_COMMA = "surface_remove_space_before_comma_after_relation_token"
SUBPOLICY_CK3_SUFFIX_FALSE_POSITIVE = "surface_ck3_plural_suffix_after_token_false_positive"


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
        CREATE TABLE IF NOT EXISTS ml_issue_surface_boundary_checkpoint_runs (
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
        CREATE TABLE IF NOT EXISTS ml_issue_surface_boundary_checkpoint_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            partial_coverage_run_id INTEGER NOT NULL,
            partial_coverage_item_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            issue_kind TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            checkpoint_action TEXT NOT NULL,
            block_reason TEXT,
            current_text TEXT NOT NULL,
            corrected_text TEXT,
            token_status TEXT NOT NULL,
            total_issue_count INTEGER NOT NULL DEFAULT 0,
            covered_issue_count INTEGER NOT NULL DEFAULT 0,
            blocked_issue_count INTEGER NOT NULL DEFAULT 0,
            open_issue_count INTEGER NOT NULL DEFAULT 0,
            open_families_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_surface_boundary_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], partial_coverage_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_surface_boundary_checkpoint_run_{partial_coverage_run_id}"
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
            pci.id AS partial_coverage_item_id,
            pci.total_issue_count,
            pci.covered_issue_count,
            pci.blocked_issue_count,
            pci.open_issue_count,
            pci.open_families_json,
            item.id AS ledger_item_id,
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.issue_kind,
            item.evidence_text AS current_text
        FROM ml_issue_partial_coverage_items pci
        JOIN ml_issue_ledger_items item
          ON item.run_id = ?
         AND item.segment_id = pci.segment_id
         AND item.issue_family = 'surface_boundary_microagent'
        WHERE pci.run_id = ?
          AND pci.coverage_state = 'partial'
        ORDER BY item.segment_id, item.id
        """,
        (ledger_run_id, partial_coverage_run_id),
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        open_families = parse_json_dict(payload.get("open_families_json"))
        if int(open_families.get("surface_boundary_microagent") or 0) > 0:
            candidates.append(payload)
    return ledger_run_id, candidates


def classify(row: dict[str, Any]) -> tuple[int, str, str, str, str]:
    text = row.get("current_text") or ""
    issue_kind = row.get("issue_kind") or ""
    relative_path = row.get("relative_path") or ""
    source_key = row.get("source_key") or ""

    if not text.strip():
        return 0, "missing_current_text", "surface_boundary_unclassified", "", "missing_text"

    hits = residual_hits(text)
    if hits and source_key != "tgp_movement_events_0160_scheme_blocking_effect_tt":
        return (
            0,
            "residual_or_context_must_be_resolved_before_surface:" + ",".join(hits[:6]),
            "surface_boundary_unclassified",
            "",
            "blocked_by_residual",
        )

    if (
        issue_kind == "space_before_punctuation"
        and relative_path == "dlc/ep2/tournament/dlc_ep2_contest_events_l_spanish.yml"
        and source_key in {"contest_events.0810.both_relation", "contest_events.0810.winner_relation"}
        and "] ," in text
        and "foi decidida" in text
    ):
        corrected = text.replace("] ,", "],")
        if corrected == text:
            return 0, "no_text_delta", SUBPOLICY_SPACE_BEFORE_COMMA, "", "no_text_delta"
        if structural_tokens(text) != structural_tokens(corrected):
            return (
                0,
                "structural_tokens_changed",
                SUBPOLICY_SPACE_BEFORE_COMMA,
                corrected,
                "structural_token_change_review_required",
            )
        return 1, "", SUBPOLICY_SPACE_BEFORE_COMMA, corrected, "same_structural_tokens"

    if (
        issue_kind == "missing_space_after_token"
        and relative_path == "dlc/tgp/dlc_tgp_japan_wars_l_spanish.yml"
        and source_key == "japan_demand_administrative_cb_defeat_desc_independence"
        and "[independent|lE]s" in text
        and "[realms|lE]" in text
        and "[soryo|lE]" in text
    ):
        return 1, "", SUBPOLICY_CK3_SUFFIX_FALSE_POSITIVE, text, "false_positive_no_text_delta"

    return 0, "surface_boundary_not_explained_by_safe_subpolicy", "surface_boundary_unclassified", "", "unclassified"


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
        "issue_kind",
        "subpolicy_name",
        "token_status",
        "current_text",
        "corrected_text",
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
        "Issue surface-boundary checkpoint",
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
        "Allowed subpolicies:",
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
                    f"- allowed={row['checkpoint_allowed']} block={row['block_reason'] or 'none'} "
                    f"subpolicy={row['subpolicy_name']} segment={row['segment_id']} "
                    f"{row['relative_path']}::{row['source_key']}"
                ),
                f"  current: {short(row.get('current_text'), 220)}",
                f"  corrected: {short(row.get('corrected_text'), 220) if row.get('corrected_text') else '<none>'}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This checkpoint records surface-boundary evidence only.",
            "- It does not write source/output and does not promote production apply by itself.",
            "- Mixed residual/context cases remain blocked for the residual/context specialists first.",
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
            allowed, block_reason, subpolicy_name, corrected_text, token_status = classify(source)
            counts["allowed" if allowed else "blocked"] += 1
            if allowed:
                counts[f"subpolicy:{subpolicy_name}"] += 1
            if block_reason:
                counts[f"block:{block_reason}"] += 1
            classified.append(
                {
                    "partial_coverage_item_id": source["partial_coverage_item_id"],
                    "ledger_item_id": source["ledger_item_id"],
                    "segment_id": source["segment_id"],
                    "relative_path": source["relative_path"],
                    "source_key": source["source_key"],
                    "agent_key": AGENT_KEY,
                    "issue_kind": source["issue_kind"],
                    "subpolicy_name": subpolicy_name,
                    "checkpoint_allowed": allowed,
                    "checkpoint_action": CHECKPOINT_ACTION,
                    "block_reason": block_reason,
                    "current_text": source["current_text"],
                    "corrected_text": corrected_text,
                    "token_status": token_status,
                    "total_issue_count": source["total_issue_count"],
                    "covered_issue_count": source["covered_issue_count"],
                    "blocked_issue_count": source["blocked_issue_count"],
                    "open_issue_count": source["open_issue_count"],
                    "open_families_json": source.get("open_families_json") or "{}",
                }
            )

        txt_path, csv_path, jsonl_path = report_paths(settings, selected_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_surface_boundary_checkpoint_runs (
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
                INSERT INTO ml_issue_surface_boundary_checkpoint_items (
                    run_id,
                    partial_coverage_run_id,
                    partial_coverage_item_id,
                    ledger_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    agent_key,
                    issue_kind,
                    subpolicy_name,
                    checkpoint_allowed,
                    checkpoint_action,
                    block_reason,
                    current_text,
                    corrected_text,
                    token_status,
                    total_issue_count,
                    covered_issue_count,
                    blocked_issue_count,
                    open_issue_count,
                    open_families_json,
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
                    row["agent_key"],
                    row["issue_kind"],
                    row["subpolicy_name"],
                    row["checkpoint_allowed"],
                    row["checkpoint_action"],
                    row["block_reason"],
                    row["current_text"],
                    row["corrected_text"],
                    row["token_status"],
                    row["total_issue_count"],
                    row["covered_issue_count"],
                    row["blocked_issue_count"],
                    row["open_issue_count"],
                    row["open_families_json"],
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

    print("[issue_surface_boundary_checkpoint] Checkpoint generated")
    print(f"[issue_surface_boundary_checkpoint] Rule version: {RULE_VERSION}")
    print(f"[issue_surface_boundary_checkpoint] Run id: {run_id}")
    print(f"[issue_surface_boundary_checkpoint] Partial coverage run id: {selected_run_id}")
    print(f"[issue_surface_boundary_checkpoint] Candidates: {len(classified):,}")
    print(f"[issue_surface_boundary_checkpoint] Allowed: {counts['allowed']:,}")
    print(f"[issue_surface_boundary_checkpoint] Blocked: {counts['blocked']:,}")
    print(f"[issue_surface_boundary_checkpoint] Report: {txt_path}")
    return {
        "run_id": run_id,
        "partial_coverage_run_id": selected_run_id,
        "candidate_count": len(classified),
        "allowed": counts["allowed"],
        "blocked": counts["blocked"],
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Checkpoint safe surface-boundary repairs and CK3 suffix false positives.")
    parser.add_argument("--partial-coverage-run-id", type=int, default=None)
    args = parser.parse_args()
    main(partial_coverage_run_id=args.partial_coverage_run_id)
