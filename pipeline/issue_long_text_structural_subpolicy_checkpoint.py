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


RULE_VERSION = "issue_long_text_structural_subpolicy_checkpoint_v1"
SOURCE_POLICY_NAME = "long_text_structural_subpolicy_shadow_v1"
CHECKPOINT_NAME = "long_text_structural_atomic_repair_checkpoint_v1"
CHECKPOINT_READY_STATUS = "ready_for_shadow_lifecycle_policy"
CHECKPOINT_BLOCKED_STATUS = "blocked_by_checkpoint_guard"
PROMOTION_READY_STATUS = "shadow_candidate"
PROMOTION_BLOCKED_STATUS = "blocked"

ATOMIC_ACTIONS = {
    "long_text_invariant_word_gender_token_removal": {
        "shadow_action": "would_observe_invariant_word_gender_token_removal_shadow",
        "checkpoint_action": "stage_long_text_invariant_word_gender_token_removal_shadow",
    },
    "long_text_object_pronoun_case_repair": {
        "shadow_action": "would_observe_object_pronoun_case_repair_shadow",
        "checkpoint_action": "stage_long_text_object_pronoun_case_repair_shadow",
    },
    "long_text_visible_ele_ela_subject_token": {
        "shadow_action": "would_observe_visible_ele_ela_subject_token_shadow",
        "checkpoint_action": "stage_long_text_visible_ele_ela_subject_token_shadow",
    },
}

GET_SHEHE_TOKEN = re.compile(r"^\[[A-Za-z_][\w.]*\.GetSheHe(?:\|[^\]]+)?\]$")
GET_HERHIM_TOKEN = re.compile(r"^\[[A-Za-z_][\w.]*\.GetHerHim(?:\|[^\]]+)?\]$")
ES_OA_TOKEN = re.compile(r"^\[[^\]]+?\.Custom\('ES_OA'\)(?:\|[^\]]+)?\]$")


def latest_shadow_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_long_text_structural_subpolicy_shadow_runs
        WHERE finished_at IS NOT NULL
          AND policy_name = ?
          AND policy_status = 'shadow'
          AND shadow_ready_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (SOURCE_POLICY_NAME,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No completed structural long-text shadow run found for {SOURCE_POLICY_NAME!r}.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_structural_subpolicy_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            shadow_run_id INTEGER NOT NULL,
            source_checkpoint_run_id INTEGER NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            promotion_status TEXT NOT NULL,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            ready_count INTEGER NOT NULL DEFAULT 0,
            shadow_blocked_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_blocked_count INTEGER NOT NULL DEFAULT 0,
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
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_structural_subpolicy_checkpoint_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
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
            shadow_action TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            current_text_hash TEXT NOT NULL,
            corrected_text_hash TEXT NOT NULL,
            token_delta_json TEXT,
            checkpoint_reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_long_text_structural_subpolicy_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], shadow_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_long_text_structural_subpolicy_checkpoint_shadow_run_{shadow_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def parse_json_obj(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {"raw": value}
    return payload if isinstance(payload, dict) else {"value": payload}


def fetch_shadow_run(conn, *, shadow_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_long_text_structural_subpolicy_shadow_runs
        WHERE id = ?
        """,
        (shadow_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Structural long-text shadow run not found: {shadow_run_id}")
    return dict(row)


def fetch_rows(conn, *, shadow_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_long_text_structural_subpolicy_shadow_items
        WHERE run_id = ?
          AND shadow_ready = 1
        ORDER BY subpolicy_name, relative_path, source_line_number, source_key
        """,
        (shadow_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def global_block_reasons(
    shadow_run: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    expected_ready: int,
    max_shadow_blocked: int,
) -> list[str]:
    reasons: list[str] = []
    if shadow_run.get("policy_name") != SOURCE_POLICY_NAME:
        reasons.append("wrong_source_policy_name")
    if shadow_run.get("policy_status") != "shadow":
        reasons.append("source_policy_not_shadow")
    if int(shadow_run.get("shadow_ready_count") or 0) != len(rows):
        reasons.append("shadow_ready_count_mismatch")
    if expected_ready >= 0 and len(rows) != expected_ready:
        reasons.append("expected_ready_count_mismatch")
    if int(shadow_run.get("blocked_count") or 0) > max_shadow_blocked:
        reasons.append("too_many_shadow_blocked_items")
    if not rows:
        reasons.append("no_shadow_ready_items")
    return reasons


def validate_token_delta(row: dict[str, Any]) -> tuple[bool, str]:
    delta = parse_json_obj(row.get("token_delta_json"))
    added = [str(item) for item in delta.get("added") or []]
    removed = [str(item) for item in delta.get("removed") or []]
    subpolicy = row.get("subpolicy_name") or ""
    if subpolicy == "long_text_invariant_word_gender_token_removal":
        if added:
            return False, "invariant_removal_must_not_add_tokens"
        if len(removed) != 1 or not ES_OA_TOKEN.match(removed[0]):
            return False, "invariant_removal_requires_one_es_oa_token"
        return True, ""
    if subpolicy == "long_text_object_pronoun_case_repair":
        if len(added) != 1 or len(removed) != 1:
            return False, "object_pronoun_case_requires_one_added_one_removed"
        if not GET_SHEHE_TOKEN.match(removed[0]) or not GET_HERHIM_TOKEN.match(added[0]):
            return False, "object_pronoun_case_requires_getshehe_to_getherhim"
        removed_scope = removed[0].split(".", 1)[0].lstrip("[")
        added_scope = added[0].split(".", 1)[0].lstrip("[")
        if removed_scope != added_scope:
            return False, "object_pronoun_scope_changed"
        return True, ""
    if subpolicy == "long_text_visible_ele_ela_subject_token":
        if removed:
            return False, "visible_ele_ela_repair_must_not_remove_structural_tokens"
        if len(added) != 1 or not GET_SHEHE_TOKEN.match(added[0]):
            return False, "visible_ele_ela_repair_requires_one_getshehe_token"
        return True, ""
    return False, "unsupported_atomic_subpolicy"


def row_block_reason(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    subpolicy = row.get("subpolicy_name") or ""
    expected = ATOMIC_ACTIONS.get(subpolicy)
    reasons = {
        "shadow_item_id": int(row["id"]),
        "subpolicy_name": subpolicy,
        "shadow_action": row.get("shadow_action") or "",
        "shadow_status": row.get("shadow_status") or "",
        "token_delta": parse_json_obj(row.get("token_delta_json")),
    }
    if expected is None:
        return "unsupported_atomic_subpolicy", reasons
    if row.get("shadow_status") != "shadow_ready":
        return row.get("block_reason") or "shadow_item_not_ready", reasons
    if int(row.get("shadow_ready") or 0) != 1:
        return "shadow_ready_flag_missing", reasons
    if row.get("block_reason"):
        return "shadow_item_has_block_reason", reasons
    if row.get("shadow_action") != expected["shadow_action"]:
        return "wrong_shadow_action", reasons
    if not row.get("current_text_hash") or not row.get("corrected_text_hash"):
        return "missing_text_hash", reasons
    if row.get("current_text_hash") == row.get("corrected_text_hash"):
        return "no_text_delta", reasons
    ok, token_reason = validate_token_delta(row)
    if not ok:
        return token_reason, reasons
    return "", reasons


def checkpoint_action_for(row: dict[str, Any]) -> str:
    subpolicy = row.get("subpolicy_name") or ""
    return ATOMIC_ACTIONS.get(subpolicy, {}).get("checkpoint_action", "stage_long_text_unknown_atomic_shadow")


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    shadow_run: dict[str, Any],
    rows: list[dict[str, Any]],
    checkpoint_status: str,
    promotion_status: str,
    global_reasons: list[str],
) -> None:
    fields = [
        "checkpoint_item_id",
        "shadow_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "repair_route",
        "subpolicy_name",
        "shadow_action",
        "checkpoint_action",
        "checkpoint_allowed",
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
                "checkpoint_reasons": row.get("checkpoint_reasons") or {},
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    allowed = sum(1 for row in rows if row["checkpoint_allowed"])
    blocked = len(rows) - allowed
    by_subpolicy = Counter(row["subpolicy_name"] for row in rows if row["checkpoint_allowed"])
    by_action = Counter(row["checkpoint_action"] for row in rows if row["checkpoint_allowed"])
    counts = Counter("checkpoint_allowed" if row["checkpoint_allowed"] else row["block_reason"] for row in rows)
    lines = [
        "Issue long-text structural subpolicy checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint name: {CHECKPOINT_NAME}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Checkpoint status: {checkpoint_status}",
        f"Promotion status: {promotion_status}",
        "Production release allowed: 0",
        f"Shadow run id: {shadow_run['id']}",
        f"Source checkpoint run id: {shadow_run['checkpoint_run_id']}",
        f"Source policy: {shadow_run['policy_name']}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Shadow ready source total: {int(shadow_run['shadow_ready_count'] or 0):,}",
        f"- Shadow blocked source total: {int(shadow_run['blocked_count'] or 0):,}",
        f"- Checkpoint allowed: {allowed:,}",
        f"- Checkpoint blocked: {blocked:,}",
        "",
        "Global gate:",
        *(f"- {reason}" for reason in global_reasons),
        *(["- ok"] if not global_reasons else []),
        "",
        "Checkpoint counts:",
        *(f"- {key}: {value:,}" for key, value in counts.most_common()),
        "",
        "Allowed subpolicies:",
        *(f"- {key}: {value:,}" for key, value in by_subpolicy.most_common()),
        "",
        "Allowed actions:",
        *(f"- {key}: {value:,}" for key, value in by_action.most_common()),
        "",
        "Allowed samples:",
    ]
    for row in [item for item in rows if item["checkpoint_allowed"]][:25]:
        lines.append(
            f"- {row['subpolicy_name']} | {row['relative_path']}::{row['source_key']} "
            f"({row['checkpoint_action']})"
        )
    blocked_rows = [item for item in rows if not item["checkpoint_allowed"]]
    if blocked_rows:
        lines.extend(["", "Blocked samples:"])
        for row in blocked_rows[:25]:
            lines.append(f"- {row['block_reason']} | {row['relative_path']}::{row['source_key']}")
    lines.extend(
        [
            "",
            "Safety notes:",
            "- Checkpoint-only: no source/output read, no confirmation promotion, no segment-state closure.",
            "- Production release remains disabled; this only allowlists atomic structural repairs for a future shadow lifecycle.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    shadow_run_id: int | None = None,
    expected_ready: int = 3,
    max_shadow_blocked: int = 8,
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_shadow_run_id = shadow_run_id or latest_shadow_run_id(conn)
        shadow_run = fetch_shadow_run(conn, shadow_run_id=selected_shadow_run_id)
        rows = fetch_rows(conn, shadow_run_id=selected_shadow_run_id)
        global_reasons = global_block_reasons(
            shadow_run,
            rows,
            expected_ready=expected_ready,
            max_shadow_blocked=max_shadow_blocked,
        )
        for row in rows:
            block_reason, reasons = row_block_reason(row)
            if global_reasons:
                block_reason = "global_gate:" + ",".join(global_reasons)
            row["checkpoint_action"] = checkpoint_action_for(row)
            row["checkpoint_allowed"] = 0 if block_reason else 1
            row["block_reason"] = block_reason
            row["checkpoint_reasons"] = reasons

        allowed = sum(1 for row in rows if row["checkpoint_allowed"])
        blocked = len(rows) - allowed
        checkpoint_status = CHECKPOINT_READY_STATUS if rows and blocked == 0 and not global_reasons else CHECKPOINT_BLOCKED_STATUS
        promotion_status = PROMOTION_READY_STATUS if checkpoint_status == CHECKPOINT_READY_STATUS else PROMOTION_BLOCKED_STATUS
        by_subpolicy = Counter(row["subpolicy_name"] for row in rows if row["checkpoint_allowed"])
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_shadow_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_long_text_structural_subpolicy_checkpoint_runs (
                rule_version,
                shadow_run_id,
                source_checkpoint_run_id,
                checkpoint_name,
                checkpoint_status,
                promotion_status,
                total_candidates,
                ready_count,
                shadow_blocked_count,
                checkpoint_allowed_count,
                checkpoint_blocked_count,
                production_release_allowed,
                subpolicy_counts_json,
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
                selected_shadow_run_id,
                int(shadow_run["checkpoint_run_id"]),
                CHECKPOINT_NAME,
                checkpoint_status,
                promotion_status,
                len(rows),
                int(shadow_run["shadow_ready_count"] or 0),
                int(shadow_run["blocked_count"] or 0),
                allowed,
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
        checkpoint_run_id = int(cur.lastrowid)
        created_at = db.utc_now()
        for row in rows:
            item_cur = conn.execute(
                """
                INSERT INTO ml_issue_long_text_structural_subpolicy_checkpoint_items (
                    run_id,
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
                    shadow_action,
                    checkpoint_action,
                    checkpoint_allowed,
                    block_reason,
                    current_text_hash,
                    corrected_text_hash,
                    token_delta_json,
                    checkpoint_reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_run_id,
                    selected_shadow_run_id,
                    int(row["id"]),
                    int(row["checkpoint_run_id"]),
                    int(row["checkpoint_item_id"]),
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
                    row["shadow_action"],
                    row["checkpoint_action"],
                    int(row["checkpoint_allowed"]),
                    row["block_reason"],
                    row["current_text_hash"],
                    row["corrected_text_hash"],
                    row.get("token_delta_json") or "{}",
                    json.dumps(row["checkpoint_reasons"], ensure_ascii=False, sort_keys=True),
                    created_at,
                ),
            )
            row["checkpoint_item_id"] = int(item_cur.lastrowid)
        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            checkpoint_run_id=checkpoint_run_id,
            shadow_run=shadow_run,
            rows=rows,
            checkpoint_status=checkpoint_status,
            promotion_status=promotion_status,
            global_reasons=global_reasons,
        )
        conn.commit()

    print("[issue_long_text_structural_subpolicy_checkpoint] Checkpoint generated")
    print(f"[issue_long_text_structural_subpolicy_checkpoint] Rule version: {RULE_VERSION}")
    print(f"[issue_long_text_structural_subpolicy_checkpoint] Checkpoint run id: {checkpoint_run_id}")
    print(f"[issue_long_text_structural_subpolicy_checkpoint] Shadow run id: {selected_shadow_run_id}")
    print(f"[issue_long_text_structural_subpolicy_checkpoint] Status: {checkpoint_status}")
    print(f"[issue_long_text_structural_subpolicy_checkpoint] Allowed: {allowed:,}")
    print(f"[issue_long_text_structural_subpolicy_checkpoint] Blocked: {blocked:,}")
    print(f"[issue_long_text_structural_subpolicy_checkpoint] Report: {txt_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "shadow_run_id": selected_shadow_run_id,
        "checkpoint_status": checkpoint_status,
        "promotion_status": promotion_status,
        "allowed_count": allowed,
        "blocked_count": blocked,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Checkpoint atomic structural long-text repair shadow items.")
    parser.add_argument("--shadow-run-id", type=int, default=None)
    parser.add_argument("--expected-ready", type=int, default=3)
    parser.add_argument("--max-shadow-blocked", type=int, default=8)
    args = parser.parse_args()
    main(
        shadow_run_id=args.shadow_run_id,
        expected_ready=args.expected_ready,
        max_shadow_blocked=args.max_shadow_blocked,
    )
