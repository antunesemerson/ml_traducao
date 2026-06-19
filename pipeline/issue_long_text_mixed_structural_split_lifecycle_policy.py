from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_long_text_mixed_structural_split_lifecycle_policy_v1"
POLICY_NAME = "long_text_mixed_structural_partial_component_shadow_lifecycle_v1"
POLICY_STATUS = "shadow"
POLICY_ACTION = "observe_long_text_mixed_structural_partial_component_shadow"
CHECKPOINT_NAME = "long_text_mixed_structural_split_ready_checkpoint_v1"
CHECKPOINT_READY_STATUS = "ready_for_partial_shadow_lifecycle_policy"
PROMOTION_READY_STATUS = "shadow_candidate"

ALLOWED_CHECKPOINT_ACTIONS = {
    "stage_mixed_split_object_pronoun_partial_shadow",
    "stage_mixed_split_quote_surface_partial_shadow",
    "stage_mixed_split_preposition_surface_partial_shadow",
}


def latest_checkpoint_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_long_text_mixed_structural_split_checkpoint_runs
        WHERE finished_at IS NOT NULL
          AND checkpoint_name = ?
          AND checkpoint_status = ?
          AND promotion_status = ?
          AND checkpoint_allowed_count > 0
          AND checkpoint_blocked_count = 0
          AND production_release_allowed = 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (CHECKPOINT_NAME, CHECKPOINT_READY_STATUS, PROMOTION_READY_STATUS),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No ready partial split checkpoint found for {CHECKPOINT_NAME!r}.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_mixed_structural_split_lifecycle_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            checkpoint_run_id INTEGER NOT NULL,
            split_run_id INTEGER NOT NULL,
            structural_shadow_run_id INTEGER NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            policy_action TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            released_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            partial_component_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            microagent_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_mixed_structural_split_lifecycle_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            checkpoint_run_id INTEGER NOT NULL,
            checkpoint_item_id INTEGER NOT NULL,
            split_run_id INTEGER NOT NULL,
            split_item_id INTEGER NOT NULL,
            structural_shadow_run_id INTEGER NOT NULL,
            structural_shadow_item_id INTEGER NOT NULL,
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
            original_subpolicy_name TEXT NOT NULL,
            repair_route TEXT NOT NULL,
            microagent_key TEXT NOT NULL,
            micro_issue_kind TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            policy_action TEXT NOT NULL,
            policy_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            partial_component_only INTEGER NOT NULL DEFAULT 1,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            current_text_hash TEXT NOT NULL,
            corrected_text_hash TEXT NOT NULL,
            token_delta_json TEXT,
            policy_reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_long_text_mixed_structural_split_lifecycle_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], checkpoint_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_long_text_mixed_structural_split_lifecycle_checkpoint_run_{checkpoint_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_checkpoint(conn, *, checkpoint_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_long_text_mixed_structural_split_checkpoint_runs
        WHERE id = ?
        """,
        (checkpoint_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Partial split checkpoint not found: {checkpoint_run_id}")
    return dict(row)


def fetch_rows(conn, *, checkpoint_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_long_text_mixed_structural_split_checkpoint_items
        WHERE run_id = ?
        ORDER BY checkpoint_allowed DESC, microagent_key, relative_path, source_line_number, source_key
        """,
        (checkpoint_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def global_block_reasons(checkpoint: dict[str, Any], rows: list[dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    if POLICY_STATUS != "shadow":
        reasons.append("policy_status_must_remain_shadow")
    if checkpoint.get("checkpoint_name") != CHECKPOINT_NAME:
        reasons.append("wrong_checkpoint_name")
    if checkpoint.get("checkpoint_status") != CHECKPOINT_READY_STATUS:
        reasons.append("checkpoint_not_ready")
    if checkpoint.get("promotion_status") != PROMOTION_READY_STATUS:
        reasons.append("checkpoint_not_shadow_candidate")
    if int(checkpoint.get("production_release_allowed") or 0) != 0:
        reasons.append("checkpoint_must_not_allow_production")
    if int(checkpoint.get("checkpoint_blocked_count") or 0) != 0:
        reasons.append("checkpoint_has_blocked_rows")
    if int(checkpoint.get("checkpoint_allowed_count") or 0) <= 0:
        reasons.append("no_allowed_checkpoint_items")
    if int(checkpoint.get("checkpoint_allowed_count") or 0) != sum(
        1 for row in rows if int(row.get("checkpoint_allowed") or 0) == 1
    ):
        reasons.append("checkpoint_allowed_count_mismatch")
    if not all(int(row.get("partial_component_only") or 0) == 1 for row in rows):
        reasons.append("all_items_must_be_partial_components")
    if any(int(row.get("production_release_allowed") or 0) != 0 for row in rows):
        reasons.append("item_production_release_must_be_disabled")
    if len({int(row["id"]) for row in rows}) != len(rows):
        reasons.append("duplicate_checkpoint_items")
    if not rows:
        reasons.append("no_lifecycle_items")
    return reasons


def evaluate_row(row: dict[str, Any], *, global_reasons: list[str]) -> tuple[int, str, dict[str, Any]]:
    reasons = {
        "checkpoint_item_id": int(row["id"]),
        "microagent_key": row.get("microagent_key") or "",
        "micro_issue_kind": row.get("micro_issue_kind") or "",
        "checkpoint_action": row.get("checkpoint_action") or "",
        "partial_component_only": int(row.get("partial_component_only") or 0),
        "token_delta": json.loads(row.get("token_delta_json") or "{}"),
    }
    if global_reasons:
        return 0, "global_gate:" + ",".join(global_reasons), reasons
    if int(row.get("checkpoint_allowed") or 0) != 1:
        return 0, row.get("block_reason") or "checkpoint_item_not_allowed", reasons
    if row.get("checkpoint_action") not in ALLOWED_CHECKPOINT_ACTIONS:
        return 0, "wrong_checkpoint_action", reasons
    if row.get("block_reason"):
        return 0, "checkpoint_item_has_block_reason", reasons
    if int(row.get("partial_component_only") or 0) != 1:
        return 0, "not_marked_partial_component_only", reasons
    if int(row.get("production_release_allowed") or 0) != 0:
        return 0, "item_production_release_enabled", reasons
    if not row.get("current_text_hash") or not row.get("corrected_text_hash"):
        return 0, "missing_text_hash", reasons
    if row.get("current_text_hash") == row.get("corrected_text_hash"):
        return 0, "no_text_delta", reasons
    return 1, "", reasons


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    policy_run_id: int,
    checkpoint: dict[str, Any],
    rows: list[dict[str, Any]],
    started_at: datetime,
    global_reasons: list[str],
) -> None:
    fields = [
        "policy_item_id",
        "checkpoint_run_id",
        "checkpoint_item_id",
        "split_run_id",
        "split_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "original_subpolicy_name",
        "repair_route",
        "microagent_key",
        "micro_issue_kind",
        "checkpoint_action",
        "policy_action",
        "policy_allowed",
        "block_reason",
        "partial_component_only",
        "production_release_allowed",
        "token_delta_json",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                **{field: row.get(field) for field in fields},
                "policy_reasons": row.get("policy_reasons") or {},
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    allowed = [row for row in rows if row["policy_allowed"]]
    blocked = [row for row in rows if not row["policy_allowed"]]
    by_microagent = Counter(row["microagent_key"] for row in allowed)
    by_action = Counter(row["checkpoint_action"] for row in allowed)
    by_block = Counter(row["block_reason"] for row in blocked)
    lines = [
        "Issue long-text mixed structural split lifecycle policy",
        f"Rule version: {RULE_VERSION}",
        f"Policy name: {POLICY_NAME}",
        f"Policy action: {POLICY_ACTION}",
        f"Policy status: {POLICY_STATUS}",
        f"Policy run id: {policy_run_id}",
        f"Checkpoint run id: {checkpoint['id']}",
        f"Split run id: {checkpoint['split_run_id']}",
        f"Structural shadow run id: {checkpoint['structural_shadow_run_id']}",
        f"Checkpoint allowed: {int(checkpoint['checkpoint_allowed_count'] or 0):,}",
        f"Checkpoint blocked: {int(checkpoint['checkpoint_blocked_count'] or 0):,}",
        "Production release allowed: 0",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Released shadow: {len(allowed):,}",
        f"- Blocked: {len(blocked):,}",
        f"- Partial components: {sum(1 for row in rows if int(row.get('partial_component_only') or 0) == 1):,}",
        f"- Global reasons: {', '.join(global_reasons) if global_reasons else 'none'}",
        f"- By microagent: {json.dumps(dict(by_microagent), ensure_ascii=False, sort_keys=True)}",
        f"- By action: {json.dumps(dict(by_action), ensure_ascii=False, sort_keys=True)}",
        f"- By block: {json.dumps(dict(by_block), ensure_ascii=False, sort_keys=True)}",
        "",
        "Released shadow components:",
    ]
    for row in allowed:
        lines.extend(
            [
                (
                    f"- {row['microagent_key']} / {row['micro_issue_kind']} | "
                    f"{row['relative_path']}:{row.get('source_line_number') or '?'}:{row['source_key']}"
                ),
                f"  policy_action={row['policy_action']}",
            ]
        )
    if not allowed:
        lines.append("- none")
    lines.extend(["", "Blocked components:"])
    for row in blocked:
        lines.append(
            (
                f"- {row['microagent_key']} / {row['micro_issue_kind']} | block={row['block_reason']} | "
                f"{row['relative_path']}:{row.get('source_line_number') or '?'}:{row['source_key']}"
            )
        )
    if not blocked:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Lifecycle-only: no source/output read, no confirmation promotion, no segment-state closure.",
            "- These are partial components inside mixed rows, not whole-segment release decisions.",
            "- Production release remains disabled until a later composition audit proves the whole segment.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, checkpoint_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_checkpoint_run_id = checkpoint_run_id or latest_checkpoint_run_id(conn)
        checkpoint = fetch_checkpoint(conn, checkpoint_run_id=selected_checkpoint_run_id)
        rows = fetch_rows(conn, checkpoint_run_id=selected_checkpoint_run_id)
        global_reasons = global_block_reasons(checkpoint, rows)

        for row in rows:
            allowed, block_reason, reasons = evaluate_row(row, global_reasons=global_reasons)
            row["policy_action"] = POLICY_ACTION
            row["policy_allowed"] = allowed
            row["block_reason"] = block_reason
            row["partial_component_only"] = 1
            row["production_release_allowed"] = 0
            row["policy_reasons"] = reasons

        released = sum(1 for row in rows if row["policy_allowed"])
        blocked = len(rows) - released
        by_microagent = Counter(row["microagent_key"] for row in rows if row["policy_allowed"])
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_checkpoint_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_long_text_mixed_structural_split_lifecycle_runs (
                rule_version,
                checkpoint_run_id,
                split_run_id,
                structural_shadow_run_id,
                policy_name,
                policy_status,
                policy_action,
                candidate_count,
                released_count,
                blocked_count,
                partial_component_count,
                production_release_allowed,
                microagent_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_checkpoint_run_id,
                int(checkpoint["split_run_id"]),
                int(checkpoint["structural_shadow_run_id"]),
                POLICY_NAME,
                POLICY_STATUS,
                POLICY_ACTION,
                len(rows),
                released,
                blocked,
                sum(1 for row in rows if int(row.get("partial_component_only") or 0) == 1),
                0,
                json.dumps(dict(by_microagent), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at.isoformat(timespec="seconds"),
                now,
                now,
            ),
        )
        policy_run_id = int(cur.lastrowid)
        created_at = db.utc_now()
        for row in rows:
            item_cur = conn.execute(
                """
                INSERT INTO ml_issue_long_text_mixed_structural_split_lifecycle_items (
                    run_id,
                    checkpoint_run_id,
                    checkpoint_item_id,
                    split_run_id,
                    split_item_id,
                    structural_shadow_run_id,
                    structural_shadow_item_id,
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
                    original_subpolicy_name,
                    repair_route,
                    microagent_key,
                    micro_issue_kind,
                    checkpoint_action,
                    policy_action,
                    policy_allowed,
                    block_reason,
                    partial_component_only,
                    production_release_allowed,
                    current_text_hash,
                    corrected_text_hash,
                    token_delta_json,
                    policy_reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    policy_run_id,
                    selected_checkpoint_run_id,
                    int(row["id"]),
                    int(row["split_run_id"]),
                    int(row["split_item_id"]),
                    int(row["structural_shadow_run_id"]),
                    int(row["structural_shadow_item_id"]),
                    int(row["source_checkpoint_run_id"]),
                    int(row["source_checkpoint_item_id"]),
                    int(row["decision_run_id"]),
                    int(row["decision_id"]),
                    int(row["queue_item_id"]),
                    int(row["ledger_item_id"]),
                    int(row["segment_id"]),
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    row["original_subpolicy_name"],
                    row["repair_route"],
                    row["microagent_key"],
                    row["micro_issue_kind"],
                    row["checkpoint_action"],
                    row["policy_action"],
                    int(row["policy_allowed"]),
                    row["block_reason"],
                    1,
                    0,
                    row["current_text_hash"],
                    row["corrected_text_hash"],
                    row.get("token_delta_json") or "{}",
                    json.dumps(row.get("policy_reasons") or {}, ensure_ascii=False, sort_keys=True),
                    created_at,
                ),
            )
            row["policy_item_id"] = int(item_cur.lastrowid)

        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            policy_run_id=policy_run_id,
            checkpoint=checkpoint,
            rows=rows,
            started_at=started_at,
            global_reasons=global_reasons,
        )
        conn.commit()

    print("[issue_long_text_mixed_structural_split_lifecycle_policy] Lifecycle generated")
    print(f"[issue_long_text_mixed_structural_split_lifecycle_policy] Rule version: {RULE_VERSION}")
    print(f"[issue_long_text_mixed_structural_split_lifecycle_policy] Policy run id: {policy_run_id}")
    print(f"[issue_long_text_mixed_structural_split_lifecycle_policy] Checkpoint run id: {selected_checkpoint_run_id}")
    print(f"[issue_long_text_mixed_structural_split_lifecycle_policy] Released shadow: {released:,}")
    print(f"[issue_long_text_mixed_structural_split_lifecycle_policy] Blocked: {blocked:,}")
    print(f"[issue_long_text_mixed_structural_split_lifecycle_policy] Report: {txt_path}")
    return {
        "policy_run_id": policy_run_id,
        "checkpoint_run_id": selected_checkpoint_run_id,
        "released_count": released,
        "blocked_count": blocked,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Lifecycle policy for split-ready mixed structural long-text partial units.")
    parser.add_argument("--checkpoint-run-id", type=int, default=None)
    args = parser.parse_args()
    main(checkpoint_run_id=args.checkpoint_run_id)
