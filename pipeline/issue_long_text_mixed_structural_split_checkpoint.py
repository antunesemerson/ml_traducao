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


RULE_VERSION = "issue_long_text_mixed_structural_split_checkpoint_v1"
SOURCE_POLICY_NAME = "long_text_mixed_structural_split_shadow_v1"
CHECKPOINT_NAME = "long_text_mixed_structural_split_ready_checkpoint_v1"
CHECKPOINT_READY_STATUS = "ready_for_partial_shadow_lifecycle_policy"
CHECKPOINT_BLOCKED_STATUS = "blocked_by_checkpoint_guard"
PROMOTION_READY_STATUS = "shadow_candidate"
PROMOTION_BLOCKED_STATUS = "blocked"

OBJECT_PRONOUN_AGENT = "long_text_object_pronoun_case_microagent"
QUOTE_SURFACE_AGENT = "long_text_quote_surface_microagent"
PREPOSITION_SURFACE_AGENT = "long_text_preposition_surface_microagent"

ALLOWED_COMPONENTS = {
    OBJECT_PRONOUN_AGENT: {
        "micro_issue_kind": "object_pronoun_case_repair",
        "split_action": "route_to_existing_object_pronoun_case_policy",
        "checkpoint_action": "stage_mixed_split_object_pronoun_partial_shadow",
    },
    QUOTE_SURFACE_AGENT: {
        "micro_issue_kind": "quote_surface_normalization",
        "split_action": "observe_quote_surface_normalization_shadow",
        "checkpoint_action": "stage_mixed_split_quote_surface_partial_shadow",
    },
    PREPOSITION_SURFACE_AGENT: {
        "micro_issue_kind": "preposition_surface_normalization",
        "split_action": "observe_preposition_surface_repair_shadow",
        "checkpoint_action": "stage_mixed_split_preposition_surface_partial_shadow",
    },
}

GET_SHEHE_TOKEN = re.compile(r"^\[[A-Za-z_][\w.]*\.GetSheHe(?:\|[^\]]+)?\]$")
GET_HERHIM_TOKEN = re.compile(r"^\[[A-Za-z_][\w.]*\.GetHerHim(?:\|[^\]]+)?\]$")


def latest_split_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_long_text_mixed_structural_split_runs
        WHERE finished_at IS NOT NULL
          AND policy_name = ?
          AND policy_status = 'shadow'
          AND split_ready_count > 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (SOURCE_POLICY_NAME,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No split-ready run found for {SOURCE_POLICY_NAME!r}.")
    return int(row["id"])


def parse_json_obj(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {"raw": value}
    return payload if isinstance(payload, dict) else {"value": payload}


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_mixed_structural_split_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            split_run_id INTEGER NOT NULL,
            structural_shadow_run_id INTEGER NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            promotion_status TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            source_split_ready_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_blocked_count INTEGER NOT NULL DEFAULT 0,
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
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_mixed_structural_split_checkpoint_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
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
            split_action TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            partial_component_only INTEGER NOT NULL DEFAULT 1,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            current_text_hash TEXT NOT NULL,
            corrected_text_hash TEXT NOT NULL,
            token_delta_json TEXT,
            checkpoint_reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_long_text_mixed_structural_split_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], split_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_long_text_mixed_structural_split_checkpoint_split_run_{split_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_split_run(conn, *, split_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_long_text_mixed_structural_split_runs
        WHERE id = ?
        """,
        (split_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Mixed structural split run not found: {split_run_id}")
    return dict(row)


def fetch_rows(conn, *, split_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_long_text_mixed_structural_split_items
        WHERE run_id = ?
          AND split_ready = 1
        ORDER BY priority DESC, microagent_key, relative_path, source_line_number, source_key
        """,
        (split_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def global_block_reasons(split_run: dict[str, Any], rows: list[dict[str, Any]], *, expected_ready: int) -> list[str]:
    reasons: list[str] = []
    if split_run.get("policy_name") != SOURCE_POLICY_NAME:
        reasons.append("wrong_source_policy_name")
    if split_run.get("policy_status") != "shadow":
        reasons.append("source_policy_not_shadow")
    if int(split_run.get("split_ready_count") or 0) != len(rows):
        reasons.append("source_split_ready_count_mismatch")
    if expected_ready >= 0 and len(rows) != expected_ready:
        reasons.append("expected_ready_count_mismatch")
    if int(split_run.get("split_unit_count") or 0) < len(rows):
        reasons.append("split_unit_count_less_than_ready_rows")
    if not rows:
        reasons.append("no_split_ready_items")
    return reasons


def validate_object_pronoun_delta(row: dict[str, Any]) -> tuple[bool, str]:
    delta = parse_json_obj(row.get("token_delta_json"))
    added = [str(item) for item in delta.get("added") or []]
    removed = [str(item) for item in delta.get("removed") or []]
    if not added or not removed:
        return False, "object_pronoun_delta_missing_added_or_removed"
    if not all(GET_HERHIM_TOKEN.match(token) for token in added):
        return False, "object_pronoun_added_must_be_getherhim"
    if not all(GET_SHEHE_TOKEN.match(token) for token in removed):
        return False, "object_pronoun_removed_must_be_getshehe"
    removed_scopes = {token.split(".", 1)[0].lstrip("[") for token in removed}
    added_scopes = {token.split(".", 1)[0].lstrip("[") for token in added}
    if removed_scopes != added_scopes:
        return False, "object_pronoun_scope_changed"
    return True, ""


def row_block_reason(row: dict[str, Any], *, global_reasons: list[str]) -> tuple[str, dict[str, Any]]:
    expected = ALLOWED_COMPONENTS.get(row.get("microagent_key") or "")
    reasons = {
        "split_item_id": int(row["id"]),
        "microagent_key": row.get("microagent_key") or "",
        "micro_issue_kind": row.get("micro_issue_kind") or "",
        "split_status": row.get("split_status") or "",
        "split_action": row.get("split_action") or "",
        "token_delta": parse_json_obj(row.get("token_delta_json")),
        "partial_component_only": True,
    }
    if global_reasons:
        return "global_gate:" + ",".join(global_reasons), reasons
    if expected is None:
        return "unsupported_split_microagent", reasons
    if row.get("micro_issue_kind") != expected["micro_issue_kind"]:
        return "wrong_micro_issue_kind", reasons
    if row.get("split_status") != "split_ready":
        return "split_item_not_ready", reasons
    if int(row.get("split_ready") or 0) != 1:
        return "split_ready_flag_missing", reasons
    if row.get("split_action") != expected["split_action"]:
        return "wrong_split_action", reasons
    if not row.get("current_text_hash") or not row.get("corrected_text_hash"):
        return "missing_text_hash", reasons
    if row.get("current_text_hash") == row.get("corrected_text_hash"):
        return "no_text_delta", reasons
    if row.get("microagent_key") == OBJECT_PRONOUN_AGENT:
        ok, token_reason = validate_object_pronoun_delta(row)
        if not ok:
            return token_reason, reasons
    return "", reasons


def checkpoint_action_for(row: dict[str, Any]) -> str:
    expected = ALLOWED_COMPONENTS.get(row.get("microagent_key") or "")
    return expected["checkpoint_action"] if expected else "stage_unknown_mixed_split_partial_shadow"


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    split_run: dict[str, Any],
    rows: list[dict[str, Any]],
    checkpoint_status: str,
    promotion_status: str,
    global_reasons: list[str],
) -> None:
    fields = [
        "checkpoint_item_id",
        "split_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "original_subpolicy_name",
        "repair_route",
        "microagent_key",
        "micro_issue_kind",
        "split_action",
        "checkpoint_action",
        "checkpoint_allowed",
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
                "checkpoint_reasons": row.get("checkpoint_reasons") or {},
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    allowed = [row for row in rows if row["checkpoint_allowed"]]
    blocked = [row for row in rows if not row["checkpoint_allowed"]]
    by_microagent = Counter(row["microagent_key"] for row in allowed)
    by_block = Counter(row["block_reason"] for row in blocked)
    lines = [
        "Issue long-text mixed structural split checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint: {CHECKPOINT_NAME}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Split run id: {split_run['id']}",
        f"Structural shadow run id: {split_run['structural_shadow_run_id']}",
        f"Status: {checkpoint_status}",
        f"Promotion status: {promotion_status}",
        f"Production release allowed: 0",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Source split-ready count: {int(split_run['split_ready_count'] or 0):,}",
        f"- Checkpoint allowed: {len(allowed):,}",
        f"- Checkpoint blocked: {len(blocked):,}",
        f"- Global reasons: {', '.join(global_reasons) if global_reasons else 'none'}",
        f"- By microagent allowed: {json.dumps(dict(by_microagent), ensure_ascii=False, sort_keys=True)}",
        f"- By block reason: {json.dumps(dict(by_block), ensure_ascii=False, sort_keys=True)}",
        "",
        "Allowed partial components:",
    ]
    for row in allowed:
        lines.extend(
            [
                (
                    f"- {row['microagent_key']} / {row['micro_issue_kind']} | "
                    f"{row['relative_path']}:{row.get('source_line_number') or '?'}:{row['source_key']}"
                ),
                f"  action={row['checkpoint_action']}",
            ]
        )
    if not allowed:
        lines.append("- none")
    lines.extend(["", "Blocked components:"])
    for row in blocked:
        lines.extend(
            [
                (
                    f"- {row['microagent_key']} / {row['micro_issue_kind']} | "
                    f"block={row['block_reason']} | {row['relative_path']}:{row.get('source_line_number') or '?'}:{row['source_key']}"
                ),
            ]
        )
    if not blocked:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Checkpoint-only: no source/output read, no confirmation promotion, no segment-state closure.",
            "- Allowed items are partial components inside mixed rows, not whole-segment release decisions.",
            "- Production release remains disabled until a later governed lifecycle and composition audit.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, split_run_id: int | None = None, expected_ready: int = 4) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_split_run_id = split_run_id or latest_split_run_id(conn)
        split_run = fetch_split_run(conn, split_run_id=selected_split_run_id)
        rows = fetch_rows(conn, split_run_id=selected_split_run_id)
        global_reasons = global_block_reasons(split_run, rows, expected_ready=expected_ready)

        for row in rows:
            block_reason, reasons = row_block_reason(row, global_reasons=global_reasons)
            row["checkpoint_action"] = checkpoint_action_for(row)
            row["checkpoint_allowed"] = 0 if block_reason else 1
            row["block_reason"] = block_reason
            row["partial_component_only"] = 1
            row["production_release_allowed"] = 0
            row["checkpoint_reasons"] = reasons

        allowed = sum(1 for row in rows if row["checkpoint_allowed"])
        blocked = len(rows) - allowed
        checkpoint_status = CHECKPOINT_READY_STATUS if allowed and not blocked else CHECKPOINT_BLOCKED_STATUS
        promotion_status = PROMOTION_READY_STATUS if allowed and not blocked else PROMOTION_BLOCKED_STATUS
        by_microagent = Counter(row["microagent_key"] for row in rows if row["checkpoint_allowed"])
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_split_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_long_text_mixed_structural_split_checkpoint_runs (
                rule_version,
                split_run_id,
                structural_shadow_run_id,
                checkpoint_name,
                checkpoint_status,
                promotion_status,
                candidate_count,
                source_split_ready_count,
                checkpoint_allowed_count,
                checkpoint_blocked_count,
                production_release_allowed,
                microagent_counts_json,
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
                selected_split_run_id,
                int(split_run["structural_shadow_run_id"]),
                CHECKPOINT_NAME,
                checkpoint_status,
                promotion_status,
                len(rows),
                int(split_run["split_ready_count"] or 0),
                allowed,
                blocked,
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
        checkpoint_run_id = int(cur.lastrowid)
        created_at = db.utc_now()
        for row in rows:
            item_cur = conn.execute(
                """
                INSERT INTO ml_issue_long_text_mixed_structural_split_checkpoint_items (
                    run_id,
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
                    split_action,
                    checkpoint_action,
                    checkpoint_allowed,
                    block_reason,
                    partial_component_only,
                    production_release_allowed,
                    current_text_hash,
                    corrected_text_hash,
                    token_delta_json,
                    checkpoint_reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_run_id,
                    selected_split_run_id,
                    int(row["id"]),
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
                    row["split_action"],
                    row["checkpoint_action"],
                    int(row["checkpoint_allowed"]),
                    row["block_reason"],
                    1,
                    0,
                    row["current_text_hash"],
                    row["corrected_text_hash"],
                    row.get("token_delta_json") or "{}",
                    json.dumps(row.get("checkpoint_reasons") or {}, ensure_ascii=False, sort_keys=True),
                    created_at,
                ),
            )
            row["checkpoint_item_id"] = int(item_cur.lastrowid)
        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            checkpoint_run_id=checkpoint_run_id,
            split_run=split_run,
            rows=rows,
            checkpoint_status=checkpoint_status,
            promotion_status=promotion_status,
            global_reasons=global_reasons,
        )
        conn.commit()

    print("[issue_long_text_mixed_structural_split_checkpoint] Checkpoint generated")
    print(f"[issue_long_text_mixed_structural_split_checkpoint] Rule version: {RULE_VERSION}")
    print(f"[issue_long_text_mixed_structural_split_checkpoint] Checkpoint run id: {checkpoint_run_id}")
    print(f"[issue_long_text_mixed_structural_split_checkpoint] Split run id: {selected_split_run_id}")
    print(f"[issue_long_text_mixed_structural_split_checkpoint] Status: {checkpoint_status}")
    print(f"[issue_long_text_mixed_structural_split_checkpoint] Allowed: {allowed:,}")
    print(f"[issue_long_text_mixed_structural_split_checkpoint] Blocked: {blocked:,}")
    print(f"[issue_long_text_mixed_structural_split_checkpoint] Report: {txt_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "split_run_id": selected_split_run_id,
        "checkpoint_status": checkpoint_status,
        "promotion_status": promotion_status,
        "allowed": allowed,
        "blocked": blocked,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Checkpoint split-ready mixed structural long-text partial units.")
    parser.add_argument("--split-run-id", type=int, default=None)
    parser.add_argument("--expected-ready", type=int, default=4)
    args = parser.parse_args()
    main(split_run_id=args.split_run_id, expected_ready=args.expected_ready)
