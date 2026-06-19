from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_long_text_structural_subpolicy_lifecycle_policy_v1"
POLICY_NAME = "long_text_structural_atomic_repair_shadow_lifecycle_v1"
POLICY_ACTION = "observe_long_text_structural_atomic_repair_shadow"
POLICY_STATUS = "shadow"
CHECKPOINT_NAME = "long_text_structural_atomic_repair_checkpoint_v1"
CHECKPOINT_READY_STATUS = "ready_for_shadow_lifecycle_policy"
PROMOTION_READY_STATUS = "shadow_candidate"

ALLOWED_CHECKPOINT_ACTIONS = {
    "stage_long_text_invariant_word_gender_token_removal_shadow",
    "stage_long_text_object_pronoun_case_repair_shadow",
    "stage_long_text_visible_ele_ela_subject_token_shadow",
}


def latest_checkpoint_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_long_text_structural_subpolicy_checkpoint_runs
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
        raise RuntimeError(f"No ready checkpoint found for {CHECKPOINT_NAME!r}.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_structural_subpolicy_lifecycle_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            checkpoint_run_id INTEGER NOT NULL,
            shadow_run_id INTEGER NOT NULL,
            source_checkpoint_run_id INTEGER NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            policy_action TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            released_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            subpolicy_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_structural_subpolicy_lifecycle_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            checkpoint_run_id INTEGER NOT NULL,
            checkpoint_item_id INTEGER NOT NULL,
            shadow_run_id INTEGER NOT NULL,
            shadow_item_id INTEGER NOT NULL,
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
            repair_route TEXT NOT NULL,
            subpolicy_name TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            policy_action TEXT NOT NULL,
            policy_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            current_text_hash TEXT NOT NULL,
            corrected_text_hash TEXT NOT NULL,
            token_delta_json TEXT,
            policy_reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_long_text_structural_subpolicy_lifecycle_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], checkpoint_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_long_text_structural_subpolicy_lifecycle_checkpoint_run_{checkpoint_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_checkpoint(conn, *, checkpoint_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_long_text_structural_subpolicy_checkpoint_runs
        WHERE id = ?
        """,
        (checkpoint_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Atomic long-text checkpoint not found: {checkpoint_run_id}")
    return dict(row)


def fetch_rows(conn, *, checkpoint_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_long_text_structural_subpolicy_checkpoint_items
        WHERE run_id = ?
        ORDER BY checkpoint_allowed DESC, subpolicy_name, relative_path, source_line_number, source_key
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
    if len({int(row["id"]) for row in rows}) != len(rows):
        reasons.append("duplicate_checkpoint_items")
    if not rows:
        reasons.append("no_lifecycle_items")
    return reasons


def evaluate_row(row: dict[str, Any], *, global_reasons: list[str]) -> tuple[int, str, dict[str, Any]]:
    reasons = {
        "checkpoint_item_id": int(row["id"]),
        "subpolicy_name": row.get("subpolicy_name") or "",
        "checkpoint_action": row.get("checkpoint_action") or "",
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
        "shadow_run_id",
        "shadow_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "repair_route",
        "subpolicy_name",
        "checkpoint_action",
        "policy_action",
        "policy_allowed",
        "block_reason",
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

    counts = Counter("released_shadow" if row["policy_allowed"] else row["block_reason"] for row in rows)
    by_subpolicy = Counter(row["subpolicy_name"] for row in rows if row["policy_allowed"])
    by_action = Counter(row["checkpoint_action"] for row in rows if row["policy_allowed"])
    lines = [
        "Issue long-text structural subpolicy lifecycle policy",
        f"Rule version: {RULE_VERSION}",
        f"Policy name: {POLICY_NAME}",
        f"Policy action: {POLICY_ACTION}",
        f"Policy status: {POLICY_STATUS}",
        f"Policy run id: {policy_run_id}",
        f"Checkpoint run id: {checkpoint['id']}",
        f"Checkpoint allowed: {int(checkpoint['checkpoint_allowed_count'] or 0):,}",
        f"Checkpoint blocked: {int(checkpoint['checkpoint_blocked_count'] or 0):,}",
        "Production release allowed: 0",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        f"- By subpolicy: {json.dumps(dict(by_subpolicy), ensure_ascii=False, sort_keys=True)}",
        f"- By action: {json.dumps(dict(by_action), ensure_ascii=False, sort_keys=True)}",
        "",
        "Global blockers:",
        *([f"- {reason}" for reason in global_reasons] or ["- none"]),
        "",
        "Shadow monitored samples:",
    ]
    for row in [item for item in rows if item["policy_allowed"]][:30]:
        lines.append(
            f"- {row['subpolicy_name']} | {row['relative_path']}:{row.get('source_line_number') or '?'}:{row['source_key']}"
        )
    if not any(row["policy_allowed"] for row in rows):
        lines.append("- none")
    blocked = [item for item in rows if not item["policy_allowed"]]
    lines.extend(["", "Blocked samples:"])
    if blocked:
        for row in blocked[:30]:
            lines.append(
                f"- {row['block_reason']} | {row['relative_path']}:{row.get('source_line_number') or '?'}:{row['source_key']}"
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Shadow lifecycle only: no output writes, no confirmation updates, no segment-state closure.",
            "- This records atomic structural long-text repairs as monitored network knowledge.",
            "- Production release remains disabled until a separate production-side policy explicitly consumes this evidence.",
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
            row["policy_reasons"] = reasons

        released = sum(1 for row in rows if row["policy_allowed"])
        blocked = len(rows) - released
        by_subpolicy = Counter(row["subpolicy_name"] for row in rows if row["policy_allowed"])
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_checkpoint_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_long_text_structural_subpolicy_lifecycle_runs (
                rule_version,
                checkpoint_run_id,
                shadow_run_id,
                source_checkpoint_run_id,
                policy_name,
                policy_status,
                policy_action,
                candidate_count,
                released_count,
                blocked_count,
                production_release_allowed,
                subpolicy_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_checkpoint_run_id,
                int(checkpoint["shadow_run_id"]),
                int(checkpoint["source_checkpoint_run_id"]),
                POLICY_NAME,
                POLICY_STATUS,
                POLICY_ACTION,
                len(rows),
                released,
                blocked,
                0,
                json.dumps(dict(by_subpolicy), ensure_ascii=False, sort_keys=True),
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
                INSERT INTO ml_issue_long_text_structural_subpolicy_lifecycle_items (
                    run_id,
                    checkpoint_run_id,
                    checkpoint_item_id,
                    shadow_run_id,
                    shadow_item_id,
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
                    repair_route,
                    subpolicy_name,
                    checkpoint_action,
                    policy_action,
                    policy_allowed,
                    block_reason,
                    current_text_hash,
                    corrected_text_hash,
                    token_delta_json,
                    policy_reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    policy_run_id,
                    selected_checkpoint_run_id,
                    int(row["id"]),
                    int(row["shadow_run_id"]),
                    int(row["shadow_item_id"]),
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
                    row["repair_route"],
                    row["subpolicy_name"],
                    row["checkpoint_action"],
                    POLICY_ACTION,
                    int(row["policy_allowed"]),
                    row["block_reason"],
                    row["current_text_hash"],
                    row["corrected_text_hash"],
                    row.get("token_delta_json") or "{}",
                    json.dumps(row["policy_reasons"], ensure_ascii=False, sort_keys=True),
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

    print("[issue_long_text_structural_subpolicy_lifecycle_policy] Lifecycle generated")
    print(f"[issue_long_text_structural_subpolicy_lifecycle_policy] Rule version: {RULE_VERSION}")
    print(f"[issue_long_text_structural_subpolicy_lifecycle_policy] Policy run id: {policy_run_id}")
    print(f"[issue_long_text_structural_subpolicy_lifecycle_policy] Checkpoint run id: {selected_checkpoint_run_id}")
    print(f"[issue_long_text_structural_subpolicy_lifecycle_policy] Released shadow: {released:,}")
    print(f"[issue_long_text_structural_subpolicy_lifecycle_policy] Blocked: {blocked:,}")
    print(f"[issue_long_text_structural_subpolicy_lifecycle_policy] Report: {txt_path}")
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
    parser = argparse.ArgumentParser(description="Create lifecycle shadow records for atomic structural long-text repairs.")
    parser.add_argument("--checkpoint-run-id", type=int, default=None)
    args = parser.parse_args()
    main(checkpoint_run_id=args.checkpoint_run_id)
