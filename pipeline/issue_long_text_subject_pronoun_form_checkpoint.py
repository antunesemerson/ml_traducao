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


RULE_VERSION = "issue_long_text_subject_pronoun_form_checkpoint_v1"
SOURCE_POLICY_NAME = "long_text_mixed_structural_token_policy_blocker_subsplit_v1"
CHECKPOINT_NAME = "long_text_subject_pronoun_form_partial_checkpoint_v1"
CHECKPOINT_READY_STATUS = "ready_for_subject_pronoun_lifecycle"
CHECKPOINT_BLOCKED_STATUS = "blocked_by_subject_pronoun_checkpoint"
PROMOTION_READY_STATUS = "shadow_candidate"
PROMOTION_BLOCKED_STATUS = "blocked"

SUBJECT_PRONOUN_AGENT = "long_text_subject_pronoun_form_microagent"
SOURCE_STATUS = "subsplit_ready"
SOURCE_ACTION = "extract_subject_getwomanman_to_getshehe_same_scope_shadow"
CHECKPOINT_ACTION = "stage_subject_getwomanman_to_getshehe_same_scope_partial_shadow"

GET_WOMANMAN_RE = re.compile(r"^\[([A-Za-z_][\w.]*)\.GetWomanMan(?:\|[^\]]+)?\]$")
GET_SHEHE_RE = re.compile(r"^\[([A-Za-z_][\w.]*)\.GetSheHe(?:\|[^\]]+)?\]$")


def parse_json_obj(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {"raw": value}
    return payload if isinstance(payload, dict) else {"value": payload}


def parse_json_list(value: str | None) -> list[Any]:
    if not value:
        return []
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return [value]
    return payload if isinstance(payload, list) else [payload]


def latest_subsplit_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_long_text_mixed_structural_token_policy_blocker_subsplit_runs
        WHERE finished_at IS NOT NULL
          AND policy_name = ?
          AND policy_status = 'shadow'
          AND subsplit_ready_count > 0
          AND production_release_allowed = 0
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (SOURCE_POLICY_NAME,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No ready blocker-subsplit run found for {SOURCE_POLICY_NAME!r}.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_subject_pronoun_form_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            subsplit_run_id INTEGER NOT NULL,
            token_policy_shadow_run_id INTEGER NOT NULL,
            split_run_id INTEGER NOT NULL,
            structural_shadow_run_id INTEGER NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            promotion_status TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            source_subsplit_ready_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_allowed_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_blocked_count INTEGER NOT NULL DEFAULT 0,
            partial_component_only_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            microagent_counts_json TEXT,
            checkpoint_action_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_subject_pronoun_form_checkpoint_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            subsplit_run_id INTEGER NOT NULL,
            subsplit_item_id INTEGER NOT NULL,
            token_policy_shadow_run_id INTEGER NOT NULL,
            token_policy_shadow_item_ids_json TEXT NOT NULL,
            split_run_id INTEGER NOT NULL,
            split_item_ids_json TEXT NOT NULL,
            structural_shadow_run_id INTEGER NOT NULL,
            structural_shadow_item_ids_json TEXT NOT NULL,
            source_checkpoint_run_ids_json TEXT NOT NULL,
            source_checkpoint_item_ids_json TEXT NOT NULL,
            decision_run_ids_json TEXT NOT NULL,
            decision_ids_json TEXT NOT NULL,
            queue_item_ids_json TEXT NOT NULL,
            ledger_item_ids_json TEXT NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            source_microagents_json TEXT NOT NULL,
            source_block_reasons_json TEXT NOT NULL,
            microagent_key TEXT NOT NULL,
            micro_issue_kind TEXT NOT NULL,
            subcomponent_kind TEXT NOT NULL,
            subsplit_action TEXT NOT NULL,
            checkpoint_action TEXT NOT NULL,
            checkpoint_allowed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            review_route TEXT,
            partial_component_only INTEGER NOT NULL DEFAULT 1,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            current_text_hash TEXT NOT NULL,
            corrected_text_hash TEXT NOT NULL,
            extracted_token_delta_json TEXT,
            checkpoint_reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_long_text_subject_pronoun_form_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], subsplit_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_long_text_subject_pronoun_form_checkpoint_subsplit_run_{subsplit_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_subsplit_run(conn, *, subsplit_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_long_text_mixed_structural_token_policy_blocker_subsplit_runs
        WHERE id = ?
        """,
        (subsplit_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Blocker subsplit run not found: {subsplit_run_id}")
    return dict(row)


def fetch_rows(conn, *, subsplit_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_long_text_mixed_structural_token_policy_blocker_subsplit_items
        WHERE run_id = ?
          AND subsplit_ready = 1
        ORDER BY microagent_key, relative_path, source_line_number, source_key
        """,
        (subsplit_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def token_parts(delta: dict[str, Any]) -> tuple[list[str], list[str]]:
    return [str(item) for item in delta.get("added") or []], [str(item) for item in delta.get("removed") or []]


def validate_extracted_delta(row: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    delta = parse_json_obj(row.get("extracted_token_delta_json"))
    added, removed = token_parts(delta)
    details = {"extracted_token_delta": delta}
    if len(added) != 1 or len(removed) != 1:
        return False, "subject_pronoun_requires_one_added_one_removed", details
    added_match = GET_SHEHE_RE.match(added[0])
    removed_match = GET_WOMANMAN_RE.match(removed[0])
    if not added_match or not removed_match:
        return False, "subject_pronoun_requires_getwomanman_to_getshehe", details
    if added_match.group(1) != removed_match.group(1):
        return False, "subject_pronoun_scope_changed", details
    details["scope"] = added_match.group(1)
    return True, "", details


def global_block_reasons(
    subsplit_run: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    expected_ready: int,
) -> list[str]:
    reasons: list[str] = []
    if subsplit_run.get("policy_name") != SOURCE_POLICY_NAME:
        reasons.append("wrong_source_policy_name")
    if subsplit_run.get("policy_status") != "shadow":
        reasons.append("source_policy_not_shadow")
    if int(subsplit_run.get("subsplit_ready_count") or 0) != len(rows):
        reasons.append("subsplit_ready_count_mismatch")
    if expected_ready >= 0 and len(rows) != expected_ready:
        reasons.append("expected_ready_count_mismatch")
    if int(subsplit_run.get("production_release_allowed") or 0) != 0:
        reasons.append("source_must_not_allow_production")
    if not rows:
        reasons.append("no_subsplit_ready_items")
    return reasons


def row_block_reason(row: dict[str, Any], *, global_reasons: list[str]) -> tuple[str, dict[str, Any]]:
    reasons = {
        "subsplit_item_id": int(row["id"]),
        "microagent_key": row.get("microagent_key") or "",
        "micro_issue_kind": row.get("micro_issue_kind") or "",
        "subcomponent_kind": row.get("subcomponent_kind") or "",
        "subsplit_status": row.get("subsplit_status") or "",
        "subsplit_action": row.get("subsplit_action") or "",
        "review_route": row.get("review_route") or "",
        "partial_component_only": int(row.get("partial_component_only") or 0),
    }
    if global_reasons:
        return "global_gate:" + ",".join(global_reasons), reasons
    if row.get("microagent_key") != SUBJECT_PRONOUN_AGENT:
        return "unsupported_subsplit_microagent", reasons
    if row.get("micro_issue_kind") != "subject_pronoun_form_swap":
        return "wrong_micro_issue_kind", reasons
    if row.get("subcomponent_kind") != "getwomanman_to_getshehe_same_scope":
        return "wrong_subcomponent_kind", reasons
    if row.get("subsplit_status") != SOURCE_STATUS:
        return "subsplit_item_not_ready", reasons
    if int(row.get("subsplit_ready") or 0) != 1:
        return "subsplit_ready_flag_missing", reasons
    if row.get("subsplit_action") != SOURCE_ACTION:
        return "wrong_subsplit_action", reasons
    if row.get("block_reason"):
        return "subsplit_item_has_block_reason", reasons
    if row.get("review_route") != "token_policy_checkpoint_candidate":
        return "wrong_review_route", reasons
    if int(row.get("partial_component_only") or 0) != 1:
        return "not_partial_component_only", reasons
    if int(row.get("production_release_allowed") or 0) != 0:
        return "item_production_release_enabled", reasons
    if not row.get("current_text_hash") or not row.get("corrected_text_hash"):
        return "missing_text_hash", reasons
    if row.get("current_text_hash") == row.get("corrected_text_hash"):
        return "no_text_delta", reasons
    ok, token_reason, details = validate_extracted_delta(row)
    reasons.update(details)
    if not ok:
        return token_reason, reasons
    return "", reasons


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    subsplit_run: dict[str, Any],
    rows: list[dict[str, Any]],
    checkpoint_status: str,
    promotion_status: str,
    global_reasons: list[str],
) -> None:
    fields = [
        "checkpoint_item_id",
        "subsplit_item_id",
        "token_policy_shadow_item_ids_json",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "source_microagents_json",
        "microagent_key",
        "micro_issue_kind",
        "subcomponent_kind",
        "subsplit_action",
        "checkpoint_action",
        "checkpoint_allowed",
        "block_reason",
        "review_route",
        "partial_component_only",
        "production_release_allowed",
        "extracted_token_delta_json",
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
    by_action = Counter(row["checkpoint_action"] for row in allowed)
    by_block = Counter(row["block_reason"] for row in blocked)
    lines = [
        "Issue long-text subject pronoun form checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint: {CHECKPOINT_NAME}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Subsplit run id: {subsplit_run['id']}",
        f"Token-policy shadow run id: {subsplit_run['token_policy_shadow_run_id']}",
        f"Split run id: {subsplit_run['split_run_id']}",
        f"Structural shadow run id: {subsplit_run['structural_shadow_run_id']}",
        f"Status: {checkpoint_status}",
        f"Promotion status: {promotion_status}",
        "Production release allowed: 0",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Source subsplit-ready count: {int(subsplit_run['subsplit_ready_count'] or 0):,}",
        f"- Checkpoint allowed: {len(allowed):,}",
        f"- Checkpoint blocked: {len(blocked):,}",
        f"- Partial component only: {len(rows):,}",
        f"- Global reasons: {', '.join(global_reasons) if global_reasons else 'none'}",
        f"- By microagent allowed: {json.dumps(dict(by_microagent), ensure_ascii=False, sort_keys=True)}",
        f"- By checkpoint action: {json.dumps(dict(by_action), ensure_ascii=False, sort_keys=True)}",
        f"- By block reason: {json.dumps(dict(by_block), ensure_ascii=False, sort_keys=True)}",
        "",
        "Allowed partial components:",
    ]
    for row in allowed:
        lines.extend(
            [
                (
                    f"- {row['microagent_key']} / {row['subcomponent_kind']} | "
                    f"{row['relative_path']}:{row.get('source_line_number') or '?'}:{row['source_key']}"
                ),
                f"  action={row['checkpoint_action']}",
            ]
        )
    if not allowed:
        lines.append("- none")
    lines.extend(["", "Blocked components:"])
    for row in blocked:
        lines.append(
            (
                f"- {row['microagent_key']} / {row['subcomponent_kind']} | "
                f"block={row['block_reason']} | {row['relative_path']}:{row.get('source_line_number') or '?'}:{row['source_key']}"
            )
        )
    if not blocked:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Checkpoint-only: no source/output read, no confirmation promotion, no segment-state closure.",
            "- Allowed items are partial token components inside mixed long-text rows, not whole-segment release decisions.",
            "- Production release remains disabled until a governed lifecycle and later composition audit.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, subsplit_run_id: int | None = None, expected_ready: int = 1) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_subsplit_run_id = subsplit_run_id or latest_subsplit_run_id(conn)
        subsplit_run = fetch_subsplit_run(conn, subsplit_run_id=selected_subsplit_run_id)
        rows = fetch_rows(conn, subsplit_run_id=selected_subsplit_run_id)
        global_reasons = global_block_reasons(subsplit_run, rows, expected_ready=expected_ready)

        for row in rows:
            block_reason, reasons = row_block_reason(row, global_reasons=global_reasons)
            row["subsplit_item_id"] = int(row["id"])
            row["checkpoint_action"] = CHECKPOINT_ACTION
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
        by_action = Counter(row["checkpoint_action"] for row in rows if row["checkpoint_allowed"])
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_subsplit_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_long_text_subject_pronoun_form_checkpoint_runs (
                rule_version,
                subsplit_run_id,
                token_policy_shadow_run_id,
                split_run_id,
                structural_shadow_run_id,
                checkpoint_name,
                checkpoint_status,
                promotion_status,
                candidate_count,
                source_subsplit_ready_count,
                checkpoint_allowed_count,
                checkpoint_blocked_count,
                partial_component_only_count,
                production_release_allowed,
                microagent_counts_json,
                checkpoint_action_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_subsplit_run_id,
                int(subsplit_run["token_policy_shadow_run_id"]),
                int(subsplit_run["split_run_id"]),
                int(subsplit_run["structural_shadow_run_id"]),
                CHECKPOINT_NAME,
                checkpoint_status,
                promotion_status,
                len(rows),
                int(subsplit_run["subsplit_ready_count"] or 0),
                allowed,
                blocked,
                len(rows),
                0,
                json.dumps(dict(by_microagent), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(by_action), ensure_ascii=False, sort_keys=True),
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
                INSERT INTO ml_issue_long_text_subject_pronoun_form_checkpoint_items (
                    run_id,
                    subsplit_run_id,
                    subsplit_item_id,
                    token_policy_shadow_run_id,
                    token_policy_shadow_item_ids_json,
                    split_run_id,
                    split_item_ids_json,
                    structural_shadow_run_id,
                    structural_shadow_item_ids_json,
                    source_checkpoint_run_ids_json,
                    source_checkpoint_item_ids_json,
                    decision_run_ids_json,
                    decision_ids_json,
                    queue_item_ids_json,
                    ledger_item_ids_json,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    source_microagents_json,
                    source_block_reasons_json,
                    microagent_key,
                    micro_issue_kind,
                    subcomponent_kind,
                    subsplit_action,
                    checkpoint_action,
                    checkpoint_allowed,
                    block_reason,
                    review_route,
                    partial_component_only,
                    production_release_allowed,
                    current_text_hash,
                    corrected_text_hash,
                    extracted_token_delta_json,
                    checkpoint_reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_run_id,
                    selected_subsplit_run_id,
                    int(row["subsplit_item_id"]),
                    int(row["token_policy_shadow_run_id"]),
                    row["token_policy_shadow_item_ids_json"],
                    int(row["split_run_id"]),
                    row["split_item_ids_json"],
                    int(row["structural_shadow_run_id"]),
                    row["structural_shadow_item_ids_json"],
                    row["source_checkpoint_run_ids_json"],
                    row["source_checkpoint_item_ids_json"],
                    row["decision_run_ids_json"],
                    row["decision_ids_json"],
                    row["queue_item_ids_json"],
                    row["ledger_item_ids_json"],
                    int(row["segment_id"]),
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    row["source_microagents_json"],
                    row["source_block_reasons_json"],
                    row["microagent_key"],
                    row["micro_issue_kind"],
                    row["subcomponent_kind"],
                    row["subsplit_action"],
                    row["checkpoint_action"],
                    int(row["checkpoint_allowed"]),
                    row["block_reason"],
                    row["review_route"],
                    1,
                    0,
                    row["current_text_hash"],
                    row["corrected_text_hash"],
                    row.get("extracted_token_delta_json") or "{}",
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
            subsplit_run=subsplit_run,
            rows=rows,
            checkpoint_status=checkpoint_status,
            promotion_status=promotion_status,
            global_reasons=global_reasons,
        )
        conn.commit()

    print("[issue_long_text_subject_pronoun_form_checkpoint] Checkpoint generated")
    print(f"[issue_long_text_subject_pronoun_form_checkpoint] Rule version: {RULE_VERSION}")
    print(f"[issue_long_text_subject_pronoun_form_checkpoint] Checkpoint run id: {checkpoint_run_id}")
    print(f"[issue_long_text_subject_pronoun_form_checkpoint] Subsplit run id: {selected_subsplit_run_id}")
    print(f"[issue_long_text_subject_pronoun_form_checkpoint] Status: {checkpoint_status}")
    print(f"[issue_long_text_subject_pronoun_form_checkpoint] Allowed: {allowed:,}")
    print(f"[issue_long_text_subject_pronoun_form_checkpoint] Blocked: {blocked:,}")
    print(f"[issue_long_text_subject_pronoun_form_checkpoint] Report: {txt_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "subsplit_run_id": selected_subsplit_run_id,
        "checkpoint_status": checkpoint_status,
        "promotion_status": promotion_status,
        "allowed": allowed,
        "blocked": blocked,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Checkpoint subject-pronoun form partial components from long-text blocker subsplit.")
    parser.add_argument("--subsplit-run-id", type=int, default=None)
    parser.add_argument("--expected-ready", type=int, default=1)
    args = parser.parse_args()
    main(subsplit_run_id=args.subsplit_run_id, expected_ready=args.expected_ready)
