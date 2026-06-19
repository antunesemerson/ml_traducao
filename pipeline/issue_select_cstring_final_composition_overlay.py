from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from issue_select_cstring_multistage_composition_overlay import (
    fetch_allowed_cleanup_by_lifecycle_item,
    fetch_allowed_dynamic_by_lifecycle_item,
    fetch_lifecycle_rows,
)


RULE_VERSION = "issue_select_cstring_final_composition_overlay_v1"
OVERLAY_NAME = "select_cstring_final_composition_overlay_v1"
OVERLAY_STATUS = "shadow"
OVERLAY_ACTION = "measure_final_composed_shadow_release"
PRODUCTION_RELEASE_ALLOWED = 0


def table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def latest_id_for(conn, table: str, extra_where: str = "1 = 1", params: tuple[Any, ...] = ()) -> int | None:
    row = conn.execute(
        f"""
        SELECT id
        FROM {table}
        WHERE finished_at IS NOT NULL
          AND ({extra_where})
        ORDER BY id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    return int(row["id"]) if row else None


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_final_composition_overlay_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            overlay_name TEXT NOT NULL,
            overlay_status TEXT NOT NULL,
            overlay_action TEXT NOT NULL,
            source_lifecycle_run_id INTEGER NOT NULL,
            source_cleanup_checkpoint_run_id INTEGER,
            source_dynamic_payload_checkpoint_run_id INTEGER,
            source_local_pronoun_checkpoint_run_id INTEGER,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            baseline_released_count INTEGER NOT NULL DEFAULT 0,
            cleanup_lift_count INTEGER NOT NULL DEFAULT 0,
            dynamic_payload_lift_count INTEGER NOT NULL DEFAULT 0,
            local_pronoun_lift_count INTEGER NOT NULL DEFAULT 0,
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
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_final_composition_overlay_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            source_lifecycle_run_id INTEGER NOT NULL,
            source_lifecycle_item_id INTEGER NOT NULL,
            source_cleanup_checkpoint_run_id INTEGER,
            source_cleanup_item_id INTEGER,
            source_dynamic_payload_checkpoint_run_id INTEGER,
            source_dynamic_payload_item_id INTEGER,
            source_local_pronoun_checkpoint_run_id INTEGER,
            source_local_pronoun_item_id INTEGER,
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
            dynamic_payload_checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            local_pronoun_checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            composition_allowed INTEGER NOT NULL DEFAULT 0,
            composition_source TEXT NOT NULL,
            composition_action TEXT NOT NULL,
            block_reason TEXT,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_select_cstring_final_composition_overlay_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], lifecycle_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_select_cstring_final_composition_overlay_lifecycle_run_{lifecycle_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_allowed_local_pronoun_by_lifecycle_item(conn, *, run_id: int | None) -> dict[int, dict[str, Any]]:
    if run_id is None:
        return {}
    rows = conn.execute(
        """
        SELECT id AS source_local_pronoun_item_id, source_lifecycle_item_id
        FROM ml_issue_select_cstring_local_player_pronoun_checkpoint_items
        WHERE run_id = ?
          AND checkpoint_allowed = 1
          AND COALESCE(block_reason, '') = ''
          AND token_status = 'local_player_pronoun_literal_validated'
        ORDER BY source_lifecycle_item_id, id
        """,
        (run_id,),
    ).fetchall()
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        payload = dict(row)
        grouped[int(payload["source_lifecycle_item_id"])].append(payload)
    return {key: items[0] for key, items in grouped.items() if len(items) == 1}


def compose_rows(
    lifecycle_rows: list[dict[str, Any]],
    cleanup_by_lifecycle_item: dict[int, dict[str, Any]],
    dynamic_by_lifecycle_item: dict[int, dict[str, Any]],
    local_by_lifecycle_item: dict[int, dict[str, Any]],
    *,
    cleanup_run_id: int | None,
    dynamic_run_id: int | None,
    local_run_id: int | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for base in lifecycle_rows:
        lifecycle_item_id = int(base["source_lifecycle_item_id"])
        baseline_allowed = int(base["policy_allowed"] or 0)
        cleanup_item = cleanup_by_lifecycle_item.get(lifecycle_item_id)
        dynamic_item = dynamic_by_lifecycle_item.get(lifecycle_item_id)
        local_item = local_by_lifecycle_item.get(lifecycle_item_id)
        reasons: list[str] = []
        cleanup_allowed = 0
        dynamic_allowed = 0
        local_allowed = 0
        composition_allowed = 0
        composition_source = "blocked"
        block_reason = base.get("block_reason") or "baseline_blocked"
        source_cleanup_item_id = None
        source_dynamic_item_id = None
        source_local_item_id = None

        if baseline_allowed:
            composition_allowed = 1
            composition_source = "baseline_lifecycle"
            block_reason = ""
            reasons.append("baseline_lifecycle_already_allowed")
        elif cleanup_item is not None:
            cleanup_allowed = 1
            composition_allowed = 1
            composition_source = "residual_literal_cleanup"
            block_reason = ""
            source_cleanup_item_id = int(cleanup_item["source_cleanup_item_id"])
            reasons.append("recovered_by_residual_literal_cleanup")
        elif dynamic_item is not None:
            dynamic_allowed = 1
            composition_allowed = 1
            composition_source = "dynamic_literal_payload_delta"
            block_reason = ""
            source_dynamic_item_id = int(dynamic_item["source_dynamic_payload_item_id"])
            reasons.append("recovered_by_dynamic_literal_payload_delta")
        elif local_item is not None:
            local_allowed = 1
            composition_allowed = 1
            composition_source = "local_player_pronoun_literal"
            block_reason = ""
            source_local_item_id = int(local_item["source_local_pronoun_item_id"])
            reasons.append("recovered_by_local_player_pronoun_literal")
        else:
            reasons.append("no_safe_composed_policy_match")

        rows.append(
            {
                **base,
                "source_cleanup_checkpoint_run_id": cleanup_run_id,
                "source_cleanup_item_id": source_cleanup_item_id,
                "source_dynamic_payload_checkpoint_run_id": dynamic_run_id,
                "source_dynamic_payload_item_id": source_dynamic_item_id,
                "source_local_pronoun_checkpoint_run_id": local_run_id,
                "source_local_pronoun_item_id": source_local_item_id,
                "baseline_policy_allowed": baseline_allowed,
                "cleanup_checkpoint_allowed": cleanup_allowed,
                "dynamic_payload_checkpoint_allowed": dynamic_allowed,
                "local_pronoun_checkpoint_allowed": local_allowed,
                "composition_allowed": composition_allowed,
                "composition_source": composition_source,
                "composition_action": OVERLAY_ACTION,
                "block_reason": block_reason,
                "production_release_allowed": PRODUCTION_RELEASE_ALLOWED,
                "reasons": reasons,
            }
        )
    return rows


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    lifecycle_run_id: int,
    cleanup_run_id: int | None,
    dynamic_run_id: int | None,
    local_run_id: int | None,
    rows: list[dict[str, Any]],
    source_counts: Counter[str],
    block_counts: Counter[str],
) -> None:
    fields = [
        "source_lifecycle_item_id",
        "source_cleanup_item_id",
        "source_dynamic_payload_item_id",
        "source_local_pronoun_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_family",
        "subpolicy_name",
        "token_status",
        "baseline_policy_allowed",
        "cleanup_checkpoint_allowed",
        "dynamic_payload_checkpoint_allowed",
        "local_pronoun_checkpoint_allowed",
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

    composed_released = sum(1 for row in rows if int(row["composition_allowed"] or 0) == 1)
    blocked = len(rows) - composed_released
    lines = [
        "Issue Select_CString final composition overlay",
        f"Rule version: {RULE_VERSION}",
        f"Overlay name: {OVERLAY_NAME}",
        f"Overlay status: {OVERLAY_STATUS}",
        f"Overlay run id: {run_id}",
        f"Source lifecycle run id: {lifecycle_run_id}",
        f"Source cleanup checkpoint run id: {cleanup_run_id or ''}",
        f"Source dynamic payload checkpoint run id: {dynamic_run_id or ''}",
        f"Source local pronoun checkpoint run id: {local_run_id or ''}",
        f"Production release allowed: {PRODUCTION_RELEASE_ALLOWED}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Baseline released shadow: {source_counts.get('baseline_lifecycle', 0):,}",
        f"- Cleanup lift: {source_counts.get('residual_literal_cleanup', 0):,}",
        f"- Dynamic payload lift: {source_counts.get('dynamic_literal_payload_delta', 0):,}",
        f"- Local pronoun lift: {source_counts.get('local_player_pronoun_literal', 0):,}",
        f"- Composed released shadow: {composed_released:,}",
        f"- Blocked after composition: {blocked:,}",
        "",
        "Composition sources:",
        *[f"- {key}: {value:,}" for key, value in source_counts.most_common()],
        "",
        "Blocks:",
        *([f"- {key}: {value:,}" for key, value in block_counts.most_common()] or ["- none"]),
        "",
        "Safety:",
        "- Shadow-only final composition overlay; no source/output files read and no output is written.",
        "- This measures combined coverage across the Select_CString micro-neuron chain before any production bridge.",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(
    *,
    lifecycle_run_id: int | None = None,
    cleanup_run_id: int | None = None,
    dynamic_run_id: int | None = None,
    local_run_id: int | None = None,
) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_lifecycle_run_id = lifecycle_run_id or latest_id_for(conn, "ml_issue_select_cstring_same_token_lifecycle_runs")
        if selected_lifecycle_run_id is None:
            raise RuntimeError("No completed same-token lifecycle run found.")
        selected_cleanup_run_id = cleanup_run_id
        if selected_cleanup_run_id is None and table_exists(conn, "ml_issue_select_cstring_residual_literal_cleanup_checkpoint_runs"):
            selected_cleanup_run_id = latest_id_for(
                conn,
                "ml_issue_select_cstring_residual_literal_cleanup_checkpoint_runs",
                "source_lifecycle_run_id = ?",
                (selected_lifecycle_run_id,),
            )
        selected_dynamic_run_id = dynamic_run_id
        if selected_dynamic_run_id is None and table_exists(conn, "ml_issue_select_cstring_dynamic_literal_payload_checkpoint_runs"):
            selected_dynamic_run_id = latest_id_for(conn, "ml_issue_select_cstring_dynamic_literal_payload_checkpoint_runs")
        selected_local_run_id = local_run_id
        if selected_local_run_id is None and table_exists(conn, "ml_issue_select_cstring_local_player_pronoun_checkpoint_runs"):
            selected_local_run_id = latest_id_for(
                conn,
                "ml_issue_select_cstring_local_player_pronoun_checkpoint_runs",
                "source_dynamic_payload_checkpoint_run_id = ?",
                (selected_dynamic_run_id,),
            )

        txt_path, csv_path, jsonl_path = report_paths(settings, selected_lifecycle_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_select_cstring_final_composition_overlay_runs (
                rule_version,
                overlay_name,
                overlay_status,
                overlay_action,
                source_lifecycle_run_id,
                source_cleanup_checkpoint_run_id,
                source_dynamic_payload_checkpoint_run_id,
                source_local_pronoun_checkpoint_run_id,
                production_release_allowed,
                started_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                OVERLAY_NAME,
                OVERLAY_STATUS,
                OVERLAY_ACTION,
                selected_lifecycle_run_id,
                selected_cleanup_run_id,
                selected_dynamic_run_id,
                selected_local_run_id,
                PRODUCTION_RELEASE_ALLOWED,
                now,
                now,
            ),
        )
        run_id = int(cursor.lastrowid)

        lifecycle_rows = fetch_lifecycle_rows(conn, lifecycle_run_id=selected_lifecycle_run_id)
        rows = compose_rows(
            lifecycle_rows,
            fetch_allowed_cleanup_by_lifecycle_item(conn, run_id=selected_cleanup_run_id),
            fetch_allowed_dynamic_by_lifecycle_item(conn, run_id=selected_dynamic_run_id),
            fetch_allowed_local_pronoun_by_lifecycle_item(conn, run_id=selected_local_run_id),
            cleanup_run_id=selected_cleanup_run_id,
            dynamic_run_id=selected_dynamic_run_id,
            local_run_id=selected_local_run_id,
        )
        source_counts: Counter[str] = Counter(row["composition_source"] for row in rows)
        block_counts: Counter[str] = Counter(
            row["block_reason"] for row in rows if not int(row["composition_allowed"] or 0)
        )
        composed_released = sum(1 for row in rows if int(row["composition_allowed"] or 0) == 1)
        blocked = len(rows) - composed_released

        for row in rows:
            conn.execute(
                """
                INSERT INTO ml_issue_select_cstring_final_composition_overlay_items (
                    run_id,
                    source_lifecycle_run_id,
                    source_lifecycle_item_id,
                    source_cleanup_checkpoint_run_id,
                    source_cleanup_item_id,
                    source_dynamic_payload_checkpoint_run_id,
                    source_dynamic_payload_item_id,
                    source_local_pronoun_checkpoint_run_id,
                    source_local_pronoun_item_id,
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
                    dynamic_payload_checkpoint_allowed,
                    local_pronoun_checkpoint_allowed,
                    composition_allowed,
                    composition_source,
                    composition_action,
                    block_reason,
                    production_release_allowed,
                    reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_lifecycle_run_id,
                    row["source_lifecycle_item_id"],
                    selected_cleanup_run_id,
                    row["source_cleanup_item_id"],
                    selected_dynamic_run_id,
                    row["source_dynamic_payload_item_id"],
                    selected_local_run_id,
                    row["source_local_pronoun_item_id"],
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
                    row["dynamic_payload_checkpoint_allowed"],
                    row["local_pronoun_checkpoint_allowed"],
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
            dynamic_run_id=selected_dynamic_run_id,
            local_run_id=selected_local_run_id,
            rows=rows,
            source_counts=source_counts,
            block_counts=block_counts,
        )
        finished_at = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            UPDATE ml_issue_select_cstring_final_composition_overlay_runs
            SET candidate_count = ?,
                baseline_released_count = ?,
                cleanup_lift_count = ?,
                dynamic_payload_lift_count = ?,
                local_pronoun_lift_count = ?,
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
                source_counts.get("baseline_lifecycle", 0),
                source_counts.get("residual_literal_cleanup", 0),
                source_counts.get("dynamic_literal_payload_delta", 0),
                source_counts.get("local_player_pronoun_literal", 0),
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
        "source_dynamic_payload_checkpoint_run_id": selected_dynamic_run_id,
        "source_local_pronoun_checkpoint_run_id": selected_local_run_id,
        "candidate_count": len(rows),
        "baseline_released_count": source_counts.get("baseline_lifecycle", 0),
        "cleanup_lift_count": source_counts.get("residual_literal_cleanup", 0),
        "dynamic_payload_lift_count": source_counts.get("dynamic_literal_payload_delta", 0),
        "local_pronoun_lift_count": source_counts.get("local_player_pronoun_literal", 0),
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
    parser = argparse.ArgumentParser(description="Measure final Select_CString composition coverage.")
    parser.add_argument("--source-lifecycle-run-id", type=int, default=None)
    parser.add_argument("--source-cleanup-checkpoint-run-id", type=int, default=None)
    parser.add_argument("--source-dynamic-payload-checkpoint-run-id", type=int, default=None)
    parser.add_argument("--source-local-pronoun-checkpoint-run-id", type=int, default=None)
    args = parser.parse_args()
    return run(
        lifecycle_run_id=args.source_lifecycle_run_id,
        cleanup_run_id=args.source_cleanup_checkpoint_run_id,
        dynamic_run_id=args.source_dynamic_payload_checkpoint_run_id,
        local_run_id=args.source_local_pronoun_checkpoint_run_id,
    )


if __name__ == "__main__":
    main()
