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


RULE_VERSION = "issue_long_text_mixed_structural_token_policy_checkpoint_v1"
SOURCE_POLICY_NAME = "long_text_mixed_structural_token_policy_shadow_v1"
CHECKPOINT_NAME = "long_text_mixed_structural_token_policy_partial_checkpoint_v1"
CHECKPOINT_READY_STATUS = "ready_for_partial_token_policy_lifecycle"
CHECKPOINT_BLOCKED_STATUS = "blocked_by_token_policy_checkpoint"
PROMOTION_READY_STATUS = "shadow_candidate"
PROMOTION_BLOCKED_STATUS = "blocked"

ARTICLE_AGENT = "long_text_article_gender_token_microagent"
LEXICAL_GENDER_AGENT = "long_text_lexical_gender_select_cstring_microagent"

ACTION_ARTICLE_SWAP = "observe_article_es_oa_to_es_xa_partial_shadow"
ACTION_LEXICAL_SELECT = "observe_single_select_cstring_lexical_gender_phrase_shadow"

CHECKPOINT_ACTIONS = {
    ARTICLE_AGENT: {
        "micro_issue_kind": "article_gender_token_swap",
        "token_policy_action": ACTION_ARTICLE_SWAP,
        "checkpoint_action": "stage_article_es_oa_to_es_xa_partial_token_shadow",
    },
    LEXICAL_GENDER_AGENT: {
        "micro_issue_kind": "lexical_gender_select_cstring_build",
        "token_policy_action": ACTION_LEXICAL_SELECT,
        "checkpoint_action": "stage_single_select_cstring_lexical_gender_phrase_partial_shadow",
    },
}

CUSTOM_TOKEN_RE = re.compile(r"^\[([A-Za-z_][\w.]*)\.Custom\('([^']+)'\)(?:\|[^\]]+)?\]$")
SELECT_CSTRING_RE = re.compile(r"^\[Select_CString\(\s*([A-Za-z_][\w.]*)\.IsFemale,\s*'<TEXT>',\s*'<TEXT>'\s*\)\]$")


def parse_json_obj(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {"raw": value}
    return payload if isinstance(payload, dict) else {"value": payload}


def latest_shadow_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_long_text_mixed_structural_token_policy_shadow_runs
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
        raise RuntimeError(f"No token-policy shadow-ready run found for {SOURCE_POLICY_NAME!r}.")
    return int(row["id"])


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_mixed_structural_token_policy_checkpoint_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            token_policy_shadow_run_id INTEGER NOT NULL,
            split_run_id INTEGER NOT NULL,
            structural_shadow_run_id INTEGER NOT NULL,
            checkpoint_name TEXT NOT NULL,
            checkpoint_status TEXT NOT NULL,
            promotion_status TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            source_shadow_ready_count INTEGER NOT NULL DEFAULT 0,
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
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_mixed_structural_token_policy_checkpoint_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            token_policy_shadow_run_id INTEGER NOT NULL,
            token_policy_shadow_item_id INTEGER NOT NULL,
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
            token_policy_action TEXT NOT NULL,
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
            FOREIGN KEY(run_id) REFERENCES ml_issue_long_text_mixed_structural_token_policy_checkpoint_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], shadow_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_long_text_mixed_structural_token_policy_checkpoint_shadow_run_{shadow_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_shadow_run(conn, *, shadow_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_long_text_mixed_structural_token_policy_shadow_runs
        WHERE id = ?
        """,
        (shadow_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Token-policy shadow run not found: {shadow_run_id}")
    return dict(row)


def fetch_rows(conn, *, shadow_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_long_text_mixed_structural_token_policy_shadow_items
        WHERE run_id = ?
          AND shadow_ready = 1
        ORDER BY microagent_key, relative_path, source_line_number, source_key
        """,
        (shadow_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def custom_scope_and_name(token: str) -> tuple[str, str] | None:
    match = CUSTOM_TOKEN_RE.match(token)
    if not match:
        return None
    return match.group(1), match.group(2)


def token_parts(delta: dict[str, Any]) -> tuple[list[str], list[str]]:
    return [str(item) for item in delta.get("added") or []], [str(item) for item in delta.get("removed") or []]


def validate_article_swap(row: dict[str, Any]) -> tuple[bool, str]:
    delta = parse_json_obj(row.get("token_delta_json"))
    added, removed = token_parts(delta)
    if len(added) != 1 or len(removed) != 1:
        return False, "article_swap_requires_one_added_one_removed"
    added_custom = custom_scope_and_name(added[0])
    removed_custom = custom_scope_and_name(removed[0])
    if not added_custom or not removed_custom:
        return False, "article_swap_requires_custom_tokens"
    if added_custom[0] != removed_custom[0]:
        return False, "article_swap_scope_changed"
    if removed_custom[1] != "ES_OA" or added_custom[1] != "ES_XA":
        return False, "article_swap_requires_es_oa_to_es_xa"
    return True, ""


def validate_lexical_select_cstring(row: dict[str, Any]) -> tuple[bool, str]:
    delta = parse_json_obj(row.get("token_delta_json"))
    added, removed = token_parts(delta)
    if len(added) != 1 or len(removed) < 1:
        return False, "lexical_select_requires_one_added_and_removed_customs"
    select = SELECT_CSTRING_RE.match(added[0])
    if not select:
        return False, "lexical_select_added_must_be_select_cstring_placeholder"
    scope = select.group(1)
    removed_customs = [custom_scope_and_name(token) for token in removed]
    if any(item is None for item in removed_customs):
        return False, "lexical_select_removed_must_be_custom_tokens"
    if any(item != (scope, "ES_OA") for item in removed_customs if item is not None):
        return False, "lexical_select_removed_scope_or_custom_changed"
    return True, ""


def global_block_reasons(
    shadow_run: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    expected_ready: int,
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
    if not rows:
        reasons.append("no_token_policy_shadow_ready_items")
    return reasons


def row_block_reason(row: dict[str, Any], *, global_reasons: list[str]) -> tuple[str, dict[str, Any]]:
    microagent = row.get("microagent_key") or ""
    expected = CHECKPOINT_ACTIONS.get(microagent)
    reasons = {
        "token_policy_shadow_item_id": int(row["id"]),
        "microagent_key": microagent,
        "micro_issue_kind": row.get("micro_issue_kind") or "",
        "token_policy_status": row.get("token_policy_status") or "",
        "token_policy_action": row.get("token_policy_action") or "",
        "token_delta": parse_json_obj(row.get("token_delta_json")),
        "partial_component_only": True,
    }
    if global_reasons:
        return "global_gate:" + ",".join(global_reasons), reasons
    if expected is None:
        return "unsupported_token_policy_microagent", reasons
    if row.get("micro_issue_kind") != expected["micro_issue_kind"]:
        return "wrong_micro_issue_kind", reasons
    if row.get("token_policy_status") != "token_shadow_ready":
        return "token_policy_item_not_ready", reasons
    if int(row.get("shadow_ready") or 0) != 1:
        return "shadow_ready_flag_missing", reasons
    if row.get("block_reason"):
        return "shadow_item_has_block_reason", reasons
    if row.get("token_policy_action") != expected["token_policy_action"]:
        return "wrong_token_policy_action", reasons
    if not row.get("current_text_hash") or not row.get("corrected_text_hash"):
        return "missing_text_hash", reasons
    if row.get("current_text_hash") == row.get("corrected_text_hash"):
        return "no_text_delta", reasons
    if microagent == ARTICLE_AGENT:
        ok, token_reason = validate_article_swap(row)
        if not ok:
            return token_reason, reasons
    if microagent == LEXICAL_GENDER_AGENT:
        ok, token_reason = validate_lexical_select_cstring(row)
        if not ok:
            return token_reason, reasons
    return "", reasons


def checkpoint_action_for(row: dict[str, Any]) -> str:
    expected = CHECKPOINT_ACTIONS.get(row.get("microagent_key") or "")
    return expected["checkpoint_action"] if expected else "stage_unknown_token_policy_partial_shadow"


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
        "token_policy_shadow_item_id",
        "split_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "original_subpolicy_name",
        "repair_route",
        "microagent_key",
        "micro_issue_kind",
        "token_policy_action",
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
    by_action = Counter(row["checkpoint_action"] for row in allowed)
    by_block = Counter(row["block_reason"] for row in blocked)
    lines = [
        "Issue long-text mixed structural token-policy checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint: {CHECKPOINT_NAME}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Token-policy shadow run id: {shadow_run['id']}",
        f"Split run id: {shadow_run['split_run_id']}",
        f"Structural shadow run id: {shadow_run['structural_shadow_run_id']}",
        f"Status: {checkpoint_status}",
        f"Promotion status: {promotion_status}",
        f"Production release allowed: 0",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Source shadow-ready count: {int(shadow_run['shadow_ready_count'] or 0):,}",
        f"- Checkpoint allowed: {len(allowed):,}",
        f"- Checkpoint blocked: {len(blocked):,}",
        f"- Partial component only: {len(rows):,}",
        f"- Global reasons: {', '.join(global_reasons) if global_reasons else 'none'}",
        f"- By microagent allowed: {json.dumps(dict(by_microagent), ensure_ascii=False, sort_keys=True)}",
        f"- By checkpoint action: {json.dumps(dict(by_action), ensure_ascii=False, sort_keys=True)}",
        f"- By block reason: {json.dumps(dict(by_block), ensure_ascii=False, sort_keys=True)}",
        "",
        "Allowed partial token components:",
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
        lines.append(
            (
                f"- {row['microagent_key']} / {row['micro_issue_kind']} | "
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
            "- Production release remains disabled until a later governed lifecycle and composition audit.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, shadow_run_id: int | None = None, expected_ready: int = 2) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_shadow_run_id = shadow_run_id or latest_shadow_run_id(conn)
        shadow_run = fetch_shadow_run(conn, shadow_run_id=selected_shadow_run_id)
        rows = fetch_rows(conn, shadow_run_id=selected_shadow_run_id)
        global_reasons = global_block_reasons(shadow_run, rows, expected_ready=expected_ready)

        for row in rows:
            block_reason, reasons = row_block_reason(row, global_reasons=global_reasons)
            row["token_policy_shadow_item_id"] = int(row["id"])
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
        by_action = Counter(row["checkpoint_action"] for row in rows if row["checkpoint_allowed"])
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_shadow_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_long_text_mixed_structural_token_policy_checkpoint_runs (
                rule_version,
                token_policy_shadow_run_id,
                split_run_id,
                structural_shadow_run_id,
                checkpoint_name,
                checkpoint_status,
                promotion_status,
                candidate_count,
                source_shadow_ready_count,
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_shadow_run_id,
                int(shadow_run["split_run_id"]),
                int(shadow_run["structural_shadow_run_id"]),
                CHECKPOINT_NAME,
                checkpoint_status,
                promotion_status,
                len(rows),
                int(shadow_run["shadow_ready_count"] or 0),
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
                INSERT INTO ml_issue_long_text_mixed_structural_token_policy_checkpoint_items (
                    run_id,
                    token_policy_shadow_run_id,
                    token_policy_shadow_item_id,
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
                    token_policy_action,
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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_run_id,
                    selected_shadow_run_id,
                    int(row["token_policy_shadow_item_id"]),
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
                    row["token_policy_action"],
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
            shadow_run=shadow_run,
            rows=rows,
            checkpoint_status=checkpoint_status,
            promotion_status=promotion_status,
            global_reasons=global_reasons,
        )
        conn.commit()

    print("[issue_long_text_mixed_structural_token_policy_checkpoint] Checkpoint generated")
    print(f"[issue_long_text_mixed_structural_token_policy_checkpoint] Rule version: {RULE_VERSION}")
    print(f"[issue_long_text_mixed_structural_token_policy_checkpoint] Checkpoint run id: {checkpoint_run_id}")
    print(f"[issue_long_text_mixed_structural_token_policy_checkpoint] Token-policy shadow run id: {selected_shadow_run_id}")
    print(f"[issue_long_text_mixed_structural_token_policy_checkpoint] Status: {checkpoint_status}")
    print(f"[issue_long_text_mixed_structural_token_policy_checkpoint] Allowed: {allowed:,}")
    print(f"[issue_long_text_mixed_structural_token_policy_checkpoint] Blocked: {blocked:,}")
    print(f"[issue_long_text_mixed_structural_token_policy_checkpoint] Report: {txt_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "token_policy_shadow_run_id": selected_shadow_run_id,
        "checkpoint_status": checkpoint_status,
        "promotion_status": promotion_status,
        "allowed": allowed,
        "blocked": blocked,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Checkpoint token-policy shadow-ready long-text partial units.")
    parser.add_argument("--shadow-run-id", type=int, default=None)
    parser.add_argument("--expected-ready", type=int, default=2)
    args = parser.parse_args()
    main(shadow_run_id=args.shadow_run_id, expected_ready=args.expected_ready)
