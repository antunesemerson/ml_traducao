from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_select_cstring_same_token_composition_overlay_v1"
OVERLAY_NAME = "select_cstring_same_token_composition_overlay_v1"
OVERLAY_STATUS = "shadow"
OVERLAY_ACTION = "measure_composed_shadow_release"
PRODUCTION_RELEASE_ALLOWED = 0


def table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def latest_lifecycle_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_select_cstring_same_token_lifecycle_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise RuntimeError("No completed Select_CString same-token lifecycle run found.")
    return int(row["id"])


def latest_cleanup_run_id(conn, *, source_lifecycle_run_id: int) -> int | None:
    if not table_exists(conn, "ml_issue_select_cstring_residual_literal_cleanup_checkpoint_runs"):
        return None
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_select_cstring_residual_literal_cleanup_checkpoint_runs
        WHERE finished_at IS NOT NULL
          AND source_lifecycle_run_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (source_lifecycle_run_id,),
    ).fetchone()
    return int(row["id"]) if row else None


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_same_token_composition_overlay_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            overlay_name TEXT NOT NULL,
            overlay_status TEXT NOT NULL,
            overlay_action TEXT NOT NULL,
            source_lifecycle_run_id INTEGER NOT NULL,
            source_cleanup_checkpoint_run_id INTEGER,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            baseline_released_count INTEGER NOT NULL DEFAULT 0,
            cleanup_lift_count INTEGER NOT NULL DEFAULT 0,
            composed_released_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            source_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_same_token_composition_overlay_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            source_lifecycle_run_id INTEGER NOT NULL,
            source_lifecycle_item_id INTEGER NOT NULL,
            source_cleanup_checkpoint_run_id INTEGER,
            source_cleanup_item_id INTEGER,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            source_family TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            token_status TEXT NOT NULL,
            baseline_policy_allowed INTEGER NOT NULL DEFAULT 0,
            cleanup_checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            composition_allowed INTEGER NOT NULL DEFAULT 0,
            composition_source TEXT NOT NULL,
            composition_action TEXT NOT NULL,
            block_reason TEXT,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_select_cstring_same_token_composition_overlay_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], lifecycle_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_select_cstring_same_token_composition_overlay_lifecycle_run_{lifecycle_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_lifecycle_rows(conn, *, lifecycle_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            id AS source_lifecycle_item_id,
            source_family,
            segment_id,
            relative_path,
            source_key,
            source_line_number,
            agent_key,
            subpolicy_name,
            token_status,
            policy_allowed,
            block_reason
        FROM ml_issue_select_cstring_same_token_lifecycle_items
        WHERE run_id = ?
        ORDER BY segment_id, id
        """,
        (lifecycle_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_cleanup_rows(conn, *, cleanup_run_id: int | None) -> dict[int, list[dict[str, Any]]]:
    if cleanup_run_id is None:
        return {}
    rows = conn.execute(
        """
        SELECT
            id AS source_cleanup_item_id,
            source_lifecycle_item_id,
            checkpoint_allowed,
            block_reason,
            token_status,
            subpolicy_name
        FROM ml_issue_select_cstring_residual_literal_cleanup_checkpoint_items
        WHERE run_id = ?
        ORDER BY source_lifecycle_item_id, id
        """,
        (cleanup_run_id,),
    ).fetchall()
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        payload = dict(row)
        grouped[int(payload["source_lifecycle_item_id"])].append(payload)
    return grouped


def compose_rows(
    lifecycle_rows: list[dict[str, Any]],
    cleanup_by_lifecycle_item: dict[int, list[dict[str, Any]]],
    *,
    cleanup_run_id: int | None,
) -> list[dict[str, Any]]:
    composed: list[dict[str, Any]] = []
    for row in lifecycle_rows:
        baseline_allowed = int(row["policy_allowed"] or 0)
        lifecycle_item_id = int(row["source_lifecycle_item_id"])
        cleanup_rows = cleanup_by_lifecycle_item.get(lifecycle_item_id, [])
        safe_cleanup_rows = [
            item
            for item in cleanup_rows
            if int(item.get("checkpoint_allowed") or 0) == 1
            and not item.get("block_reason")
            and item.get("token_status") == "same_structural_tokens"
        ]
        reasons: list[str] = []
        source_cleanup_item_id = None
        cleanup_allowed = 0
        composition_allowed = 0
        composition_source = "blocked"
        block_reason = row.get("block_reason") or "baseline_blocked"

        if baseline_allowed:
            composition_allowed = 1
            composition_source = "baseline_lifecycle"
            block_reason = ""
            reasons.append("baseline_lifecycle_already_allowed")
        elif len(safe_cleanup_rows) == 1:
            cleanup_allowed = 1
            source_cleanup_item_id = int(safe_cleanup_rows[0]["source_cleanup_item_id"])
            composition_allowed = 1
            composition_source = "residual_literal_cleanup"
            block_reason = ""
            reasons.append("blocked_lifecycle_row_recovered_by_residual_literal_cleanup")
        elif len(safe_cleanup_rows) > 1:
            cleanup_allowed = 1
            block_reason = "duplicate_cleanup_rows_require_arbitration"
            reasons.append("multiple_cleanup_rows_matched_same_lifecycle_item")
        else:
            reasons.append("no_safe_cleanup_match_for_blocked_lifecycle_row")

        composed.append(
            {
                **row,
                "source_cleanup_checkpoint_run_id": cleanup_run_id,
                "source_cleanup_item_id": source_cleanup_item_id,
                "baseline_policy_allowed": baseline_allowed,
                "cleanup_checkpoint_allowed": cleanup_allowed,
                "composition_allowed": composition_allowed,
                "composition_source": composition_source,
                "composition_action": OVERLAY_ACTION,
                "block_reason": block_reason,
                "production_release_allowed": PRODUCTION_RELEASE_ALLOWED,
                "reasons": reasons,
            }
        )
    return composed


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    lifecycle_run_id: int,
    cleanup_run_id: int | None,
    rows: list[dict[str, Any]],
    source_counts: Counter[str],
    block_counts: Counter[str],
) -> None:
    fields = [
        "source_lifecycle_item_id",
        "source_cleanup_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_family",
        "subpolicy_name",
        "token_status",
        "baseline_policy_allowed",
        "cleanup_checkpoint_allowed",
        "composition_allowed",
        "composition_source",
        "block_reason",
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

    baseline = sum(1 for row in rows if int(row["baseline_policy_allowed"] or 0) == 1)
    cleanup_lift = sum(1 for row in rows if row["composition_source"] == "residual_literal_cleanup")
    composed_released = sum(1 for row in rows if int(row["composition_allowed"] or 0) == 1)
    blocked = len(rows) - composed_released
    lines = [
        "Issue Select_CString same-token composition overlay",
        f"Rule version: {RULE_VERSION}",
        f"Overlay name: {OVERLAY_NAME}",
        f"Overlay status: {OVERLAY_STATUS}",
        f"Overlay run id: {run_id}",
        f"Source lifecycle run id: {lifecycle_run_id}",
        f"Source cleanup checkpoint run id: {cleanup_run_id or ''}",
        f"Production release allowed: {PRODUCTION_RELEASE_ALLOWED}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Baseline released shadow: {baseline:,}",
        f"- Cleanup lift: {cleanup_lift:,}",
        f"- Composed released shadow: {composed_released:,}",
        f"- Blocked after composition: {blocked:,}",
        "",
        "Composition sources:",
        *[f"- {key}: {value:,}" for key, value in source_counts.most_common()],
        "",
        "Blocks:",
        *([f"- {key}: {value:,}" for key, value in block_counts.most_common()] or ["- none"]),
        "",
        "Recovered samples:",
    ]
    recovered = [row for row in rows if row["composition_source"] == "residual_literal_cleanup"]
    for row in recovered[:40]:
        lines.append(
            f"- {row['segment_id']} {row['relative_path']}::{row['source_key']} "
            f"token={row['token_status']} source={row['composition_source']}"
        )
    lines.extend(
        [
            "",
            "Safety:",
            "- Shadow-only composition overlay; no source/output files read and no output is written.",
            "- The overlay measures lifecycle lift after a second-layer cleanup checkpoint.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(*, lifecycle_run_id: int | None = None, cleanup_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_lifecycle_run_id = lifecycle_run_id or latest_lifecycle_run_id(conn)
        selected_cleanup_run_id = cleanup_run_id
        if selected_cleanup_run_id is None:
            selected_cleanup_run_id = latest_cleanup_run_id(
                conn,
                source_lifecycle_run_id=selected_lifecycle_run_id,
            )
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_lifecycle_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_select_cstring_same_token_composition_overlay_runs (
                rule_version,
                overlay_name,
                overlay_status,
                overlay_action,
                source_lifecycle_run_id,
                source_cleanup_checkpoint_run_id,
                production_release_allowed,
                started_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                OVERLAY_NAME,
                OVERLAY_STATUS,
                OVERLAY_ACTION,
                selected_lifecycle_run_id,
                selected_cleanup_run_id,
                PRODUCTION_RELEASE_ALLOWED,
                now,
                now,
            ),
        )
        run_id = int(cursor.lastrowid)

        lifecycle_rows = fetch_lifecycle_rows(conn, lifecycle_run_id=selected_lifecycle_run_id)
        cleanup_rows = fetch_cleanup_rows(conn, cleanup_run_id=selected_cleanup_run_id)
        rows = compose_rows(
            lifecycle_rows,
            cleanup_rows,
            cleanup_run_id=selected_cleanup_run_id,
        )

        source_counts: Counter[str] = Counter(row["composition_source"] for row in rows)
        block_counts: Counter[str] = Counter(
            row["block_reason"] for row in rows if not int(row["composition_allowed"] or 0)
        )
        baseline = sum(1 for row in rows if int(row["baseline_policy_allowed"] or 0) == 1)
        cleanup_lift = source_counts.get("residual_literal_cleanup", 0)
        composed_released = sum(1 for row in rows if int(row["composition_allowed"] or 0) == 1)
        blocked = len(rows) - composed_released

        for row in rows:
            conn.execute(
                """
                INSERT INTO ml_issue_select_cstring_same_token_composition_overlay_items (
                    run_id,
                    source_lifecycle_run_id,
                    source_lifecycle_item_id,
                    source_cleanup_checkpoint_run_id,
                    source_cleanup_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    source_family,
                    agent_key,
                    subpolicy_name,
                    token_status,
                    baseline_policy_allowed,
                    cleanup_checkpoint_allowed,
                    composition_allowed,
                    composition_source,
                    composition_action,
                    block_reason,
                    production_release_allowed,
                    reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_lifecycle_run_id,
                    row["source_lifecycle_item_id"],
                    selected_cleanup_run_id,
                    row["source_cleanup_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["source_family"],
                    row["agent_key"],
                    row["subpolicy_name"],
                    row["token_status"],
                    row["baseline_policy_allowed"],
                    row["cleanup_checkpoint_allowed"],
                    row["composition_allowed"],
                    row["composition_source"],
                    row["composition_action"],
                    row["block_reason"],
                    row["production_release_allowed"],
                    json.dumps(row["reasons"], ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )

        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            lifecycle_run_id=selected_lifecycle_run_id,
            cleanup_run_id=selected_cleanup_run_id,
            rows=rows,
            source_counts=source_counts,
            block_counts=block_counts,
        )
        finished_at = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            UPDATE ml_issue_select_cstring_same_token_composition_overlay_runs
            SET candidate_count = ?,
                baseline_released_count = ?,
                cleanup_lift_count = ?,
                composed_released_count = ?,
                blocked_count = ?,
                source_counts_json = ?,
                block_counts_json = ?,
                report_path = ?,
                csv_path = ?,
                jsonl_path = ?,
                finished_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                len(rows),
                baseline,
                cleanup_lift,
                composed_released,
                blocked,
                json.dumps(dict(source_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(block_counts), ensure_ascii=False, sort_keys=True),
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
        "source_lifecycle_run_id": selected_lifecycle_run_id,
        "source_cleanup_checkpoint_run_id": selected_cleanup_run_id,
        "candidate_count": len(rows),
        "baseline_released_count": baseline,
        "cleanup_lift_count": cleanup_lift,
        "composed_released_count": composed_released,
        "blocked_count": blocked,
        "production_release_allowed": PRODUCTION_RELEASE_ALLOWED,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return payload


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Measure composed same-token lifecycle lift after cleanup checkpoints.")
    parser.add_argument("--source-lifecycle-run-id", type=int, default=None)
    parser.add_argument("--source-cleanup-checkpoint-run-id", type=int, default=None)
    args = parser.parse_args()
    return run(
        lifecycle_run_id=args.source_lifecycle_run_id,
        cleanup_run_id=args.source_cleanup_checkpoint_run_id,
    )


if __name__ == "__main__":
    main()
