from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short
from auto_confirmation_reopen_text_shadow_policy import POLICIES


RULE_VERSION = "auto_confirmation_reopen_text_policy_checkpoint_v1"
DEFAULT_POLICY_KEY = "weak_auto_static_token_only"
CHECKPOINT_ACTION = "guarded_lifecycle_candidate_static_token_only"
CHECKPOINT_NAME = "weak_auto_static_token_only_guarded_checkpoint_v1"


def latest_shadow_policy_run_id(conn, *, policy_name: str, agent_key: str) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM auto_confirmation_reopen_text_shadow_policy_runs
        WHERE finished_at IS NOT NULL
          AND policy_name = ?
          AND agent_key = ?
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (policy_name, agent_key),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No completed shadow policy run found for {policy_name!r}/{agent_key!r}.")
    return int(row["id"])


def report_paths(settings: dict[str, Any], *, agent_key: str) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_auto_confirmation_reopen_text_policy_checkpoint_{agent_key}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def fetch_shadow_run(conn, *, shadow_policy_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM auto_confirmation_reopen_text_shadow_policy_runs
        WHERE id = ?
        """,
        (shadow_policy_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Shadow policy run not found: {shadow_policy_run_id}")
    return dict(row)


def fetch_shadow_items(conn, *, shadow_policy_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            item.*,
            source.english_text,
            source.spanish_text,
            output.portuguese_text,
            confirmation.confirmed_text,
            decision.evidence_label,
            decision.decision AS review_decision
        FROM auto_confirmation_reopen_text_shadow_policy_items item
        JOIN source_segments source ON source.id = item.segment_id
        LEFT JOIN output_segments output ON output.segment_id = item.segment_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.id = (
              SELECT c.id
              FROM segment_confirmations c
              WHERE c.segment_id = item.segment_id
              ORDER BY c.updated_at DESC, c.id DESC
              LIMIT 1
          )
        LEFT JOIN auto_confirmation_reopen_text_review_decisions decision
          ON decision.id = item.review_decision_id
        WHERE item.run_id = ?
        ORDER BY item.relative_path, item.source_line_number, item.source_key
        """,
        (shadow_policy_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def row_block_reason(row: dict[str, Any], *, expected_agent_key: str, expected_action: str) -> str:
    if row.get("agent_key") != expected_agent_key:
        return "wrong_agent_key"
    if row.get("shadow_status") != "shadow_ready":
        return row.get("block_reason") or "shadow_not_ready"
    if row.get("shadow_action") != expected_action:
        return "wrong_shadow_action"
    if int(row.get("pattern_match") or 0) != 1:
        return "pattern_not_matched"
    if int(row.get("positive_evidence") or 0) != 1:
        return "missing_positive_evidence"
    if int(row.get("negative_evidence") or 0) != 0:
        return "negative_evidence"
    if int(row.get("issue_count") or 0) != 0:
        return "issue_signal_present"
    if not row.get("current_confirmed_text_hash"):
        return "missing_confirmed_hash"
    return ""


def global_block_reasons(
    *,
    shadow_run: dict[str, Any],
    expected_policy_name: str,
    expected_agent_key: str,
    expected_parent_agent_key: str,
    expected_action: str,
    total_items: int,
    allowed_count: int,
    min_ready_required: int,
    max_blocked_allowed: int,
) -> list[str]:
    reasons: list[str] = []
    if shadow_run.get("policy_status") != "shadow":
        reasons.append("shadow_run_not_shadow_status")
    if shadow_run.get("policy_name") != expected_policy_name:
        reasons.append("wrong_policy_name")
    if shadow_run.get("agent_key") != expected_agent_key:
        reasons.append("wrong_agent_key")
    if shadow_run.get("parent_agent_key") != expected_parent_agent_key:
        reasons.append("wrong_parent_agent_key")
    if int(shadow_run.get("total_candidates") or 0) != total_items:
        reasons.append("shadow_item_count_mismatch")
    if int(shadow_run.get("shadow_ready_count") or 0) < min_ready_required:
        reasons.append("min_ready_not_met")
    if int(shadow_run.get("blocked_count") or 0) > max_blocked_allowed:
        reasons.append("too_many_shadow_blocks")
    if int(shadow_run.get("blocked_count") or 0) != 0:
        reasons.append("shadow_has_blocked_rows")
    if int(shadow_run.get("negative_evidence_count") or 0) != 0:
        reasons.append("shadow_has_negative_evidence")
    if allowed_count != total_items:
        reasons.append("checkpoint_item_blocks_present")
    if not expected_action:
        reasons.append("missing_expected_action")
    return reasons


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    checkpoint_run_id: int,
    shadow_run: dict[str, Any],
    rows: list[dict[str, Any]],
    started_at: datetime,
    checkpoint_status: str,
    promotion_status: str,
    min_ready_required: int,
    max_blocked_allowed: int,
    global_reasons: list[str],
) -> None:
    fieldnames = [
        "checkpoint_item_id",
        "shadow_policy_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "agent_key",
        "text_subfamily",
        "shadow_status",
        "shadow_action",
        "checkpoint_action",
        "checkpoint_allowed",
        "block_reason",
        "positive_evidence",
        "negative_evidence",
        "issue_count",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = {
                **{field: row.get(field) for field in fieldnames},
                "english_preview": short(row.get("english_text")),
                "spanish_preview": short(row.get("spanish_text")),
                "output_preview": short(row.get("portuguese_text")),
                "confirmed_preview": short(row.get("confirmed_text")),
                "shadow_reasons": row.get("shadow_reasons"),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    counts = Counter("checkpoint_allowed" if row["checkpoint_allowed"] else row["block_reason"] for row in rows)
    lines = [
        "Auto-confirmation text policy checkpoint",
        f"Rule version: {RULE_VERSION}",
        f"Checkpoint name: {CHECKPOINT_NAME}",
        f"Checkpoint run id: {checkpoint_run_id}",
        f"Checkpoint status: {checkpoint_status}",
        f"Promotion status: {promotion_status}",
        "Production release allowed: 0",
        f"Shadow policy run id: {shadow_run['id']}",
        f"Shadow policy name: {shadow_run['policy_name']}",
        f"Agent: {shadow_run['agent_key']}",
        f"Parent agent: {shadow_run['parent_agent_key'] or ''}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Governance gates:",
        f"- Min ready required: {min_ready_required:,}",
        f"- Max blocked allowed: {max_blocked_allowed:,}",
        f"- Shadow total: {int(shadow_run['total_candidates'] or 0):,}",
        f"- Shadow ready: {int(shadow_run['shadow_ready_count'] or 0):,}",
        f"- Shadow blocked: {int(shadow_run['blocked_count'] or 0):,}",
        f"- Shadow positive evidence: {int(shadow_run['positive_evidence_count'] or 0):,}",
        f"- Shadow negative evidence: {int(shadow_run['negative_evidence_count'] or 0):,}",
        "",
        "Checkpoint items:",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        "",
        "Global blockers:",
        *([f"- {reason}" for reason in global_reasons] or ["- none"]),
        "",
        "Allowed sample:",
    ]
    allowed = [row for row in rows if row["checkpoint_allowed"]]
    for row in allowed[:20]:
        lines.extend(
            [
                f"- {row['relative_path']}:{row['source_line_number']}:{row['source_key']}",
                f"  action={row['checkpoint_action']}; evidence={row.get('evidence_label')}",
            ]
        )
    if not allowed:
        lines.append("- none")
    lines.extend(["", "Blocked sample:"])
    blocked = [row for row in rows if not row["checkpoint_allowed"]]
    for row in blocked[:30]:
        lines.extend(
            [
                f"- {row['block_reason']} | {row['relative_path']}:{row['source_line_number']}:{row['source_key']}",
                f"  shadow={row.get('shadow_status')}; action={row.get('shadow_action')}",
                f"  confirmed={short(row.get('confirmed_text'))}",
            ]
        )
    if not blocked:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- This checkpoint records that the strict static-token-only branch passed governance.",
            "- It does not alter confirmations, segment-state, source files, or output files.",
            "- Embedded literal tokens, source-visible semantic deltas, and ES custom-localization helpers remain outside this checkpoint.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    shadow_policy_run_id: int | None = None,
    policy_key: str = DEFAULT_POLICY_KEY,
    min_ready_required: int = 100,
    max_blocked_allowed: int = 0,
) -> dict[str, Any]:
    if policy_key != DEFAULT_POLICY_KEY:
        raise ValueError("Only weak_auto_static_token_only is eligible for this guarded checkpoint.")
    policy = POLICIES[policy_key]
    expected_policy_name = policy["policy_name"]
    expected_agent_key = policy["agent_key"]
    expected_parent_agent_key = policy["parent_agent_key"]
    expected_action = policy["shadow_action"]
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_shadow_policy_run_id = shadow_policy_run_id or latest_shadow_policy_run_id(
            conn,
            policy_name=expected_policy_name,
            agent_key=expected_agent_key,
        )
        shadow_run = fetch_shadow_run(conn, shadow_policy_run_id=selected_shadow_policy_run_id)
        rows = fetch_shadow_items(conn, shadow_policy_run_id=selected_shadow_policy_run_id)
        if not rows:
            raise RuntimeError(f"Shadow policy run {selected_shadow_policy_run_id} has no items.")

        for row in rows:
            block_reason = row_block_reason(
                row,
                expected_agent_key=expected_agent_key,
                expected_action=expected_action,
            )
            row["checkpoint_action"] = CHECKPOINT_ACTION
            row["checkpoint_allowed"] = 0 if block_reason else 1
            row["block_reason"] = block_reason
            try:
                row["shadow_reasons"] = json.loads(row.get("reasons_json") or "[]")
            except json.JSONDecodeError:
                row["shadow_reasons"] = []

        preliminary_allowed_count = sum(row["checkpoint_allowed"] for row in rows)
        global_reasons = global_block_reasons(
            shadow_run=shadow_run,
            expected_policy_name=expected_policy_name,
            expected_agent_key=expected_agent_key,
            expected_parent_agent_key=expected_parent_agent_key,
            expected_action=expected_action,
            total_items=len(rows),
            allowed_count=preliminary_allowed_count,
            min_ready_required=min_ready_required,
            max_blocked_allowed=max_blocked_allowed,
        )
        if global_reasons:
            for row in rows:
                if row["checkpoint_allowed"]:
                    row["checkpoint_allowed"] = 0
                    row["block_reason"] = "global_gate:" + ",".join(global_reasons)

        allowed_count = sum(row["checkpoint_allowed"] for row in rows)
        blocked_count = len(rows) - allowed_count
        checkpoint_status = (
            "ready_for_guarded_lifecycle_policy"
            if allowed_count == len(rows) and not global_reasons
            else "blocked_by_checkpoint_guard"
        )
        promotion_status = "guarded_candidate" if checkpoint_status == "ready_for_guarded_lifecycle_policy" else "blocked"
        txt_path, csv_path, jsonl_path = report_paths(settings, agent_key=expected_agent_key)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_text_policy_checkpoint_runs (
                rule_version,
                shadow_policy_run_id,
                policy_name,
                checkpoint_name,
                checkpoint_status,
                agent_key,
                parent_agent_key,
                diagnostic_run_id,
                specialist_audit_run_id,
                min_ready_required,
                max_blocked_allowed,
                total_candidates,
                ready_count,
                blocked_count,
                checkpoint_allowed_count,
                checkpoint_blocked_count,
                positive_evidence_count,
                negative_evidence_count,
                release_action,
                promotion_status,
                production_release_allowed,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_shadow_policy_run_id,
                expected_policy_name,
                CHECKPOINT_NAME,
                checkpoint_status,
                expected_agent_key,
                expected_parent_agent_key,
                shadow_run.get("diagnostic_run_id"),
                shadow_run.get("specialist_audit_run_id"),
                min_ready_required,
                max_blocked_allowed,
                len(rows),
                int(shadow_run.get("shadow_ready_count") or 0),
                int(shadow_run.get("blocked_count") or 0),
                allowed_count,
                blocked_count,
                int(shadow_run.get("positive_evidence_count") or 0),
                int(shadow_run.get("negative_evidence_count") or 0),
                expected_action,
                promotion_status,
                0,
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at.isoformat(timespec="seconds"),
                now,
                now,
            ),
        )
        checkpoint_run_id = int(cursor.lastrowid)

        for row in rows:
            item_cursor = conn.execute(
                """
                INSERT INTO auto_confirmation_reopen_text_policy_checkpoint_items (
                    checkpoint_run_id,
                    shadow_policy_run_id,
                    shadow_policy_item_id,
                    diagnostic_item_id,
                    review_decision_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    agent_key,
                    text_subfamily,
                    shadow_status,
                    shadow_action,
                    checkpoint_action,
                    checkpoint_allowed,
                    block_reason,
                    pattern_match,
                    positive_evidence,
                    negative_evidence,
                    issue_count,
                    select_cstring_count,
                    concept_link_count,
                    spanish_literal_hint_count,
                    current_confirmed_text_hash,
                    reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_run_id,
                    selected_shadow_policy_run_id,
                    row["id"],
                    row.get("diagnostic_item_id"),
                    row.get("review_decision_id"),
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    row["agent_key"],
                    row["text_subfamily"],
                    row["shadow_status"],
                    row["shadow_action"],
                    row["checkpoint_action"],
                    row["checkpoint_allowed"],
                    row["block_reason"],
                    int(row.get("pattern_match") or 0),
                    int(row.get("positive_evidence") or 0),
                    int(row.get("negative_evidence") or 0),
                    int(row.get("issue_count") or 0),
                    int(row.get("select_cstring_count") or 0),
                    int(row.get("concept_link_count") or 0),
                    int(row.get("spanish_literal_hint_count") or 0),
                    row.get("current_confirmed_text_hash"),
                    json.dumps(row.get("shadow_reasons") or [], ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            row["checkpoint_item_id"] = int(item_cursor.lastrowid)
            row["shadow_policy_item_id"] = row["id"]

        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            checkpoint_run_id=checkpoint_run_id,
            shadow_run=shadow_run,
            rows=rows,
            started_at=started_at,
            checkpoint_status=checkpoint_status,
            promotion_status=promotion_status,
            min_ready_required=min_ready_required,
            max_blocked_allowed=max_blocked_allowed,
            global_reasons=global_reasons,
        )
        conn.commit()

    print("[auto_confirmation_reopen_text_policy_checkpoint] Checkpoint generated")
    print(f"[auto_confirmation_reopen_text_policy_checkpoint] Policy: {policy_key}")
    print(f"[auto_confirmation_reopen_text_policy_checkpoint] Checkpoint run id: {checkpoint_run_id}")
    print(f"[auto_confirmation_reopen_text_policy_checkpoint] Shadow policy run id: {selected_shadow_policy_run_id}")
    print(f"[auto_confirmation_reopen_text_policy_checkpoint] Status: {checkpoint_status}")
    print(f"[auto_confirmation_reopen_text_policy_checkpoint] Allowed: {allowed_count:,}/{len(rows):,}")
    print("[auto_confirmation_reopen_text_policy_checkpoint] Production release allowed: 0")
    print(f"[auto_confirmation_reopen_text_policy_checkpoint] Report: {txt_path}")
    print(f"[auto_confirmation_reopen_text_policy_checkpoint] CSV: {csv_path}")
    print(f"[auto_confirmation_reopen_text_policy_checkpoint] JSONL: {jsonl_path}")
    return {
        "checkpoint_run_id": checkpoint_run_id,
        "shadow_policy_run_id": selected_shadow_policy_run_id,
        "checkpoint_status": checkpoint_status,
        "promotion_status": promotion_status,
        "allowed": allowed_count,
        "blocked": blocked_count,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a guarded checkpoint from a clean text shadow policy.")
    parser.add_argument("--shadow-policy-run-id", type=int, default=None)
    parser.add_argument("--policy", choices=[DEFAULT_POLICY_KEY], default=DEFAULT_POLICY_KEY)
    parser.add_argument("--min-ready", type=int, default=100)
    parser.add_argument("--max-blocked", type=int, default=0)
    args = parser.parse_args()
    main(
        shadow_policy_run_id=args.shadow_policy_run_id,
        policy_key=args.policy,
        min_ready_required=args.min_ready,
        max_blocked_allowed=args.max_blocked,
    )
