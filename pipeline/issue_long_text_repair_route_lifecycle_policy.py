from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short


RULE_VERSION = "issue_long_text_repair_route_lifecycle_policy_v1"
POLICY_NAME = "long_text_repair_route_shadow_lifecycle_v1"
POLICY_ACTION = "observe_long_text_repair_route_shadow"
POLICY_STATUS = "shadow"
CHECKPOINT_POLICY_NAME = "long_text_repair_route_shadow_v1"
ALLOWED_CHECKPOINT_ACTIONS = {"stage_long_text_repair_route_shadow"}
ALLOWED_TOKEN_STATUS = {"same_structural_tokens", "dynamic_literal_payload_only"}
ALLOWED_REPAIR_ROUTES = {
    "quote_surface_normalization",
    "spanish_select_cstring_literal",
    "glossary_visible_label_translation",
}


def sha256_text(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def latest_checkpoint_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_long_text_repair_route_checkpoint_runs
        WHERE finished_at IS NOT NULL
          AND policy_name = ?
          AND policy_status = 'shadow'
          AND allowed_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (CHECKPOINT_POLICY_NAME,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No ready checkpoint found for {CHECKPOINT_POLICY_NAME!r}.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_repair_route_lifecycle_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            checkpoint_run_id INTEGER NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            policy_action TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            released_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            route_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_repair_route_lifecycle_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            checkpoint_run_id INTEGER NOT NULL,
            checkpoint_item_id INTEGER NOT NULL,
            decision_run_id INTEGER NOT NULL,
            decision_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            repair_route TEXT NOT NULL,
            token_status TEXT NOT NULL,
            policy_action TEXT NOT NULL,
            policy_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            current_text_hash TEXT NOT NULL,
            corrected_text_hash TEXT NOT NULL,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_long_text_repair_route_lifecycle_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], checkpoint_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_long_text_repair_route_lifecycle_checkpoint_run_{checkpoint_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_checkpoint(conn, *, checkpoint_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_long_text_repair_route_checkpoint_runs
        WHERE id = ?
        """,
        (checkpoint_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Checkpoint run not found: {checkpoint_run_id}")
    return dict(row)


def fetch_rows(conn, *, checkpoint_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_long_text_repair_route_checkpoint_items
        WHERE run_id = ?
          AND checkpoint_allowed = 1
        ORDER BY repair_route, relative_path, source_line_number, source_key
        """,
        (checkpoint_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def global_block_reasons(checkpoint: dict[str, Any], rows: list[dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    if checkpoint.get("policy_name") != CHECKPOINT_POLICY_NAME:
        reasons.append("wrong_checkpoint_policy_name")
    if checkpoint.get("policy_status") != "shadow":
        reasons.append("checkpoint_policy_not_shadow")
    if int(checkpoint.get("allowed_count") or 0) <= 0:
        reasons.append("checkpoint_has_no_allowed_items")
    if int(checkpoint.get("allowed_count") or 0) != len(rows):
        reasons.append("checkpoint_allowed_count_mismatch")
    if POLICY_STATUS != "shadow":
        reasons.append("policy_status_must_remain_shadow")
    if len({int(row["id"]) for row in rows}) != len(rows):
        reasons.append("duplicate_checkpoint_items")
    return reasons


def evaluate_row(row: dict[str, Any], *, global_reasons: list[str]) -> tuple[int, str]:
    if global_reasons:
        return 0, "global_gate:" + ",".join(global_reasons)
    if int(row.get("checkpoint_allowed") or 0) != 1:
        return 0, row.get("block_reason") or "checkpoint_item_not_allowed"
    if row.get("checkpoint_action") not in ALLOWED_CHECKPOINT_ACTIONS:
        return 0, "wrong_checkpoint_action"
    if row.get("repair_route") not in ALLOWED_REPAIR_ROUTES:
        return 0, "repair_route_not_allowed_for_lifecycle"
    if row.get("token_status") not in ALLOWED_TOKEN_STATUS:
        return 0, "token_status_not_allowed_for_lifecycle"
    current = row.get("current_text") or ""
    corrected = row.get("corrected_text") or ""
    if not current.strip() or not corrected.strip():
        return 0, "missing_text"
    if current == corrected:
        return 0, "no_text_delta"
    return 1, ""


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
    fieldnames = [
        "policy_item_id",
        "checkpoint_item_id",
        "decision_run_id",
        "decision_id",
        "queue_item_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "repair_route",
        "token_status",
        "policy_action",
        "policy_allowed",
        "block_reason",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                **{field: row.get(field) for field in fieldnames},
                "current_preview": short(row.get("current_text")),
                "corrected_preview": short(row.get("corrected_text")),
                "reasons": row.get("reasons") or [],
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    counts = Counter("released_shadow" if row["policy_allowed"] else row["block_reason"] for row in rows)
    by_route = Counter(row["repair_route"] for row in rows if row["policy_allowed"])
    by_token = Counter(row["token_status"] for row in rows if row["policy_allowed"])
    lines = [
        "Issue long-text repair route lifecycle policy",
        f"Rule version: {RULE_VERSION}",
        f"Policy name: {POLICY_NAME}",
        f"Policy action: {POLICY_ACTION}",
        f"Policy status: {POLICY_STATUS}",
        f"Policy run id: {policy_run_id}",
        f"Checkpoint run id: {checkpoint['id']}",
        f"Checkpoint allowed: {int(checkpoint['allowed_count'] or 0):,}",
        f"Checkpoint blocked: {int(checkpoint['blocked_count'] or 0):,}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        f"- By route: {json.dumps(dict(by_route), ensure_ascii=False, sort_keys=True)}",
        f"- By token status: {json.dumps(dict(by_token), ensure_ascii=False, sort_keys=True)}",
        "",
        "Global blockers:",
        *([f"- {reason}" for reason in global_reasons] or ["- none"]),
        "",
        "Shadow released sample:",
    ]
    for row in [item for item in rows if item["policy_allowed"]][:25]:
        lines.extend(
            [
                f"- {row['relative_path']}:{row.get('source_line_number') or '?'}:{row['source_key']} | {row['repair_route']} | {row['token_status']}",
                f"  corrected={short(row.get('corrected_text'))}",
            ]
        )
    if not any(item["policy_allowed"] for item in rows):
        lines.append("- none")
    lines.extend(["", "Blocked sample:"])
    for row in [item for item in rows if not item["policy_allowed"]][:25]:
        lines.extend(
            [
                f"- {row['block_reason']} | {row['relative_path']}:{row.get('source_line_number') or '?'}:{row['source_key']}",
                f"  corrected={short(row.get('corrected_text'))}",
            ]
        )
    if all(item["policy_allowed"] for item in rows):
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Shadow lifecycle only: no output writes, no confirmation updates, no segment-state closure.",
            "- This records that narrow long-text repairs may be monitored by the learning network.",
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
        if not rows:
            raise RuntimeError(f"Checkpoint run {selected_checkpoint_run_id} has no allowed items.")
        global_reasons = global_block_reasons(checkpoint, rows)
        now = datetime.now().isoformat(timespec="seconds")
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_checkpoint_run_id)
        for row in rows:
            allowed, block_reason = evaluate_row(row, global_reasons=global_reasons)
            row["policy_action"] = POLICY_ACTION
            row["policy_allowed"] = allowed
            row["block_reason"] = block_reason
            row["reasons"] = json.loads(row.get("reasons_json") or "[]")
        counts = Counter("released_shadow" if row["policy_allowed"] else row["block_reason"] for row in rows)
        route_counts = Counter(row["repair_route"] for row in rows if row["policy_allowed"])
        cur = conn.execute(
            """
            INSERT INTO ml_issue_long_text_repair_route_lifecycle_runs (
                rule_version,
                checkpoint_run_id,
                policy_name,
                policy_status,
                policy_action,
                candidate_count,
                released_count,
                blocked_count,
                route_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_checkpoint_run_id,
                POLICY_NAME,
                POLICY_STATUS,
                POLICY_ACTION,
                len(rows),
                counts["released_shadow"],
                len(rows) - counts["released_shadow"],
                json.dumps(dict(route_counts), ensure_ascii=False, sort_keys=True),
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
            conn.execute(
                """
                INSERT INTO ml_issue_long_text_repair_route_lifecycle_items (
                    run_id,
                    checkpoint_run_id,
                    checkpoint_item_id,
                    decision_run_id,
                    decision_id,
                    queue_item_id,
                    ledger_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    repair_route,
                    token_status,
                    policy_action,
                    policy_allowed,
                    block_reason,
                    current_text_hash,
                    corrected_text_hash,
                    reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    policy_run_id,
                    selected_checkpoint_run_id,
                    int(row["id"]),
                    int(row["decision_run_id"]),
                    int(row["decision_id"]),
                    int(row["queue_item_id"]),
                    int(row["ledger_item_id"]),
                    int(row["segment_id"]),
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    row["repair_route"],
                    row["token_status"],
                    POLICY_ACTION,
                    int(row["policy_allowed"]),
                    row["block_reason"],
                    sha256_text(row.get("current_text")),
                    sha256_text(row.get("corrected_text")),
                    json.dumps(row.get("reasons") or [], ensure_ascii=False, sort_keys=True),
                    created_at,
                ),
            )
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

    print("[issue_long_text_repair_route_lifecycle_policy] Lifecycle generated")
    print(f"[issue_long_text_repair_route_lifecycle_policy] Rule version: {RULE_VERSION}")
    print(f"[issue_long_text_repair_route_lifecycle_policy] Policy run id: {policy_run_id}")
    print(f"[issue_long_text_repair_route_lifecycle_policy] Checkpoint run id: {selected_checkpoint_run_id}")
    print(f"[issue_long_text_repair_route_lifecycle_policy] Candidates: {len(rows):,}")
    print(f"[issue_long_text_repair_route_lifecycle_policy] Released: {counts['released_shadow']:,}")
    print(f"[issue_long_text_repair_route_lifecycle_policy] Blocked: {len(rows) - counts['released_shadow']:,}")
    print(f"[issue_long_text_repair_route_lifecycle_policy] Report: {txt_path}")
    return {
        "policy_run_id": policy_run_id,
        "checkpoint_run_id": selected_checkpoint_run_id,
        "candidate_count": len(rows),
        "released_count": counts["released_shadow"],
        "blocked_count": len(rows) - counts["released_shadow"],
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create shadow lifecycle records for allowed long-text repair routes.")
    parser.add_argument("--checkpoint-run-id", type=int, default=None)
    args = parser.parse_args()
    main(checkpoint_run_id=args.checkpoint_run_id)
