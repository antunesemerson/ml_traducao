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


RULE_VERSION = "auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_policy_v1"
POLICY_NAME = "boundary_token_subpolicy_shadow_lifecycle_v1"
POLICY_ACTION = "observe_boundary_token_subpolicy_shadow"
EXPECTED_SUBPOLICIES = {
    "select_cstring_invariant_ptbr_verb",
    "glossary_visible_label_ptbr_translation",
    "es_deldela_literal_de_ptbr_repair",
    "dynamic_scope_ptbr_helper_neutralization",
    "select_cstring_direct_name_reference",
    "localplayerstring_war_join_neutralization",
    "es_helper_narrative_neutralization",
    "hostage_context_neutralization",
    "short_dynamic_spanish_verb_neutralization",
    "hunt_activity_select_cstring_neutralization",
    "coronation_title_es_helper_neutralization",
    "nickname_whisperer_select_cstring_neutralization",
    "single_combat_victor_name_pronoun_neutralization",
    "tour_title_gendered_possessive_neutralization",
    "ep3_travel_title_adjective_alignment",
}
ALLOWED_CHECKPOINT_ACTIONS = {
    "stage_select_cstring_invariant_ptbr_shadow",
    "stage_glossary_visible_label_ptbr_shadow",
    "stage_es_deldela_literal_de_ptbr_shadow",
    "stage_dynamic_scope_ptbr_helper_neutralization_shadow",
    "stage_select_cstring_direct_name_reference_shadow",
    "stage_localplayerstring_war_join_neutralization_shadow",
    "stage_es_helper_narrative_neutralization_shadow",
    "stage_hostage_context_neutralization_shadow",
    "stage_short_dynamic_spanish_verb_neutralization_shadow",
    "stage_hunt_activity_select_cstring_neutralization_shadow",
    "stage_coronation_title_es_helper_neutralization_shadow",
    "stage_nickname_whisperer_select_cstring_neutralization_shadow",
    "stage_single_combat_victor_name_pronoun_neutralization_shadow",
    "stage_tour_title_gendered_possessive_neutralization_shadow",
    "stage_ep3_travel_title_adjective_alignment_shadow",
}


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_policy"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_checkpoint_runs(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT run.*
        FROM auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint_runs run
        JOIN (
            SELECT checkpoint_name, subpolicy_name, MAX(id) AS id
            FROM auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint_runs
            WHERE finished_at IS NOT NULL
              AND checkpoint_status = 'ready_for_shadow_lifecycle_policy'
              AND promotion_status = 'shadow_candidate'
              AND checkpoint_allowed_count > 0
              AND checkpoint_blocked_count = 0
              AND validation_issue_count = 0
            GROUP BY checkpoint_name, subpolicy_name
        ) latest
          ON latest.id = run.id
        ORDER BY run.subpolicy_name, run.id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def selected_checkpoint_runs(conn, *, checkpoint_run_ids: list[int] | None) -> list[dict[str, Any]]:
    if not checkpoint_run_ids:
        return latest_checkpoint_runs(conn)
    placeholders = ",".join("?" for _ in checkpoint_run_ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint_runs
        WHERE id IN ({placeholders})
        ORDER BY subpolicy_name, id
        """,
        tuple(checkpoint_run_ids),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_rows(conn, *, checkpoint_run_ids: list[int]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in checkpoint_run_ids)
    rows = conn.execute(
        f"""
        SELECT
            item.id AS checkpoint_item_id,
            item.checkpoint_run_id,
            item.subpolicy_shadow_run_id,
            item.subpolicy_shadow_item_id,
            item.bridge_run_id,
            item.bridge_item_id,
            item.repair_queue_item_id,
            item.boundary_policy_item_id,
            item.review_decision_id,
            item.segment_id,
            item.relative_path,
            item.source_key,
            item.source_line_number,
            item.boundary_agent_key,
            item.boundary_policy,
            item.policy_bucket,
            item.risk_level,
            item.subpolicy_status,
            item.checkpoint_action,
            item.checkpoint_allowed,
            item.block_reason AS checkpoint_block_reason,
            item.current_confirmed_text_hash,
            item.corrected_text_hash,
            item.evidence_json,
            run.checkpoint_name,
            run.checkpoint_status,
            run.promotion_status,
            run.subpolicy_name,
            decision.corrected_text,
            confirmation.confirmed_text
        FROM auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint_items item
        JOIN auto_confirmation_reopen_text_boundary_token_subpolicy_checkpoint_runs run
          ON run.id = item.checkpoint_run_id
        JOIN auto_confirmation_reopen_text_review_decisions decision
          ON decision.id = item.review_decision_id
        LEFT JOIN segment_confirmations confirmation
          ON confirmation.id = (
              SELECT c.id
              FROM segment_confirmations c
              WHERE c.segment_id = item.segment_id
              ORDER BY c.updated_at DESC, c.id DESC
              LIMIT 1
          )
        WHERE item.checkpoint_run_id IN ({placeholders})
        ORDER BY item.policy_bucket, item.relative_path, item.source_line_number, item.source_key
        """,
        tuple(checkpoint_run_ids),
    ).fetchall()
    return [dict(row) for row in rows]


def global_block_reasons(
    checkpoints: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    policy_status: str,
    min_checkpoints: int,
    expected_total: int | None,
    expected_subpolicies: set[str] | None,
) -> list[str]:
    reasons: list[str] = []
    if policy_status != "shadow":
        reasons.append("policy_status_must_remain_shadow")
    if len(checkpoints) < min_checkpoints:
        reasons.append("min_checkpoint_count_not_met")
    subpolicies = {str(row["subpolicy_name"]) for row in checkpoints}
    if expected_subpolicies is not None:
        missing_subpolicies = sorted(expected_subpolicies - subpolicies)
        if missing_subpolicies:
            reasons.append("missing_expected_subpolicies:" + ",".join(missing_subpolicies))
    if expected_total is not None and len(rows) != expected_total:
        reasons.append("expected_total_mismatch")
    for checkpoint in checkpoints:
        if checkpoint.get("checkpoint_status") != "ready_for_shadow_lifecycle_policy":
            reasons.append(f"checkpoint_not_ready:{checkpoint['id']}")
        if checkpoint.get("promotion_status") != "shadow_candidate":
            reasons.append(f"checkpoint_not_shadow_candidate:{checkpoint['id']}")
        if int(checkpoint.get("checkpoint_blocked_count") or 0) != 0:
            reasons.append(f"checkpoint_has_blocked_rows:{checkpoint['id']}")
        if int(checkpoint.get("validation_issue_count") or 0) != 0:
            reasons.append(f"checkpoint_has_validation_issues:{checkpoint['id']}")
    allowed_total = sum(int(checkpoint.get("checkpoint_allowed_count") or 0) for checkpoint in checkpoints)
    row_allowed_total = sum(1 for row in rows if int(row.get("checkpoint_allowed") or 0) == 1)
    if allowed_total != row_allowed_total:
        reasons.append("checkpoint_allowed_count_mismatch")
    if len({int(row["checkpoint_item_id"]) for row in rows}) != len(rows):
        reasons.append("duplicate_checkpoint_items")
    return reasons


def evaluate_row(row: dict[str, Any], *, global_reasons: list[str]) -> tuple[int, str]:
    if global_reasons:
        return 0, "global_gate:" + ",".join(global_reasons)
    if int(row.get("checkpoint_allowed") or 0) != 1:
        return 0, row.get("checkpoint_block_reason") or "checkpoint_item_not_allowed"
    if row.get("checkpoint_action") not in ALLOWED_CHECKPOINT_ACTIONS:
        return 0, "wrong_checkpoint_action"
    if row.get("subpolicy_status") != "shadow_ready":
        return 0, "subpolicy_not_shadow_ready"
    if row.get("checkpoint_status") != "ready_for_shadow_lifecycle_policy":
        return 0, "checkpoint_not_ready"
    if row.get("promotion_status") != "shadow_candidate":
        return 0, "checkpoint_not_shadow_candidate"
    if not row.get("current_confirmed_text_hash") or not row.get("corrected_text_hash"):
        return 0, "missing_text_hash"
    if row.get("current_confirmed_text_hash") == row.get("corrected_text_hash"):
        return 0, "no_text_delta"
    if not row.get("evidence_json"):
        return 0, "missing_evidence"
    return 1, ""


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    policy_run_id: int,
    checkpoints: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    started_at: datetime,
    policy_status: str,
    global_reasons: list[str],
) -> None:
    fieldnames = [
        "policy_item_id",
        "checkpoint_run_id",
        "checkpoint_item_id",
        "subpolicy_shadow_run_id",
        "subpolicy_shadow_item_id",
        "bridge_item_id",
        "repair_queue_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "boundary_policy",
        "policy_bucket",
        "subpolicy_name",
        "checkpoint_name",
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
                "confirmed_preview": short(row.get("confirmed_text")),
                "corrected_preview": short(row.get("corrected_text")),
                "evidence": json.loads(row["evidence_json"]) if row.get("evidence_json") else {},
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    counts = Counter("released_shadow" if row["policy_allowed"] else row["block_reason"] for row in rows)
    by_subpolicy = Counter(row["subpolicy_name"] for row in rows if row["policy_allowed"])
    by_bucket = Counter(row["policy_bucket"] for row in rows if row["policy_allowed"])
    by_boundary = Counter(row["boundary_policy"] for row in rows if row["policy_allowed"])
    lines = [
        "Auto-confirmation boundary token subpolicy lifecycle policy",
        f"Rule version: {RULE_VERSION}",
        f"Policy name: {POLICY_NAME}",
        f"Policy action: {POLICY_ACTION}",
        f"Policy status: {policy_status}",
        f"Policy run id: {policy_run_id}",
        f"Checkpoint runs: {json.dumps([int(row['id']) for row in checkpoints], ensure_ascii=False)}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        *[f"- {key}: {value:,}" for key, value in counts.most_common()],
        f"- By subpolicy: {json.dumps(dict(by_subpolicy), ensure_ascii=False, sort_keys=True)}",
        f"- By bucket: {json.dumps(dict(by_bucket), ensure_ascii=False, sort_keys=True)}",
        f"- By boundary policy: {json.dumps(dict(by_boundary), ensure_ascii=False, sort_keys=True)}",
        "",
        "Global blockers:",
        *([f"- {reason}" for reason in global_reasons] or ["- none"]),
        "",
        "Shadow released sample:",
    ]
    for row in [item for item in rows if item["policy_allowed"]][:30]:
        lines.extend(
            [
                f"- {row['relative_path']}:{row['source_line_number']}:{row['source_key']} | {row['subpolicy_name']}",
                f"  corrected={short(row.get('corrected_text'))}",
            ]
        )
    if not any(item["policy_allowed"] for item in rows):
        lines.append("- none")
    lines.extend(["", "Blocked sample:"])
    blocked = [item for item in rows if not item["policy_allowed"]]
    if blocked:
        for row in blocked[:30]:
            lines.extend(
                [
                    f"- {row['block_reason']} | {row['relative_path']}:{row['source_line_number']}:{row['source_key']}",
                    f"  corrected={short(row.get('corrected_text'))}",
                ]
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Shadow lifecycle only: no output writes, no confirmation updates, no segment-state closure.",
            "- This aggregates narrow token-boundary subpolicy checkpoints for learning governance.",
            "- Production release remains disabled until a separate production-side allowlist consumes this evidence.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    checkpoint_run_ids: list[int] | None = None,
    policy_status: str = "shadow",
    min_checkpoints: int = 4,
    expected_total: int | None = None,
) -> dict[str, Any]:
    if policy_status != "shadow":
        raise ValueError("Boundary token subpolicy lifecycle is intentionally shadow-only for now.")
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        checkpoints = selected_checkpoint_runs(conn, checkpoint_run_ids=checkpoint_run_ids)
        if not checkpoints:
            raise RuntimeError("No ready token subpolicy checkpoints found.")
        ids = [int(row["id"]) for row in checkpoints]
        rows = fetch_rows(conn, checkpoint_run_ids=ids)
        if not rows:
            raise RuntimeError("Selected token subpolicy checkpoints have no items.")
        expected_subpolicies = (
            EXPECTED_SUBPOLICIES
            if checkpoint_run_ids is None
            else {str(row["subpolicy_name"]) for row in checkpoints}
        )

        reasons = global_block_reasons(
            checkpoints,
            rows,
            policy_status=policy_status,
            min_checkpoints=min_checkpoints,
            expected_total=expected_total,
            expected_subpolicies=expected_subpolicies,
        )
        for row in rows:
            allowed, block_reason = evaluate_row(row, global_reasons=reasons)
            row["policy_action"] = POLICY_ACTION
            row["policy_allowed"] = allowed
            row["block_reason"] = block_reason

        counts = Counter("released_shadow" if row["policy_allowed"] else row["block_reason"] for row in rows)
        txt_path, csv_path, jsonl_path = report_paths(settings)
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_runs (
                rule_version,
                checkpoint_run_ids_json,
                policy_name,
                policy_status,
                policy_action,
                candidate_count,
                released_count,
                blocked_count,
                checkpoint_count,
                subpolicy_count,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                json.dumps(ids, ensure_ascii=False),
                POLICY_NAME,
                policy_status,
                POLICY_ACTION,
                len(rows),
                counts["released_shadow"],
                len(rows) - counts["released_shadow"],
                len(checkpoints),
                len({row["subpolicy_name"] for row in checkpoints}),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at.isoformat(timespec="seconds"),
                now,
                now,
            ),
        )
        policy_run_id = int(cursor.lastrowid)
        for row in rows:
            item_cursor = conn.execute(
                """
                INSERT INTO auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_items (
                    run_id,
                    checkpoint_run_id,
                    checkpoint_item_id,
                    subpolicy_shadow_run_id,
                    subpolicy_shadow_item_id,
                    bridge_run_id,
                    bridge_item_id,
                    repair_queue_item_id,
                    boundary_policy_item_id,
                    review_decision_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    boundary_agent_key,
                    boundary_policy,
                    policy_bucket,
                    risk_level,
                    subpolicy_name,
                    checkpoint_name,
                    policy_action,
                    policy_allowed,
                    block_reason,
                    current_confirmed_text_hash,
                    corrected_text_hash,
                    evidence_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    policy_run_id,
                    row["checkpoint_run_id"],
                    row["checkpoint_item_id"],
                    row["subpolicy_shadow_run_id"],
                    row["subpolicy_shadow_item_id"],
                    row["bridge_run_id"],
                    row["bridge_item_id"],
                    row["repair_queue_item_id"],
                    row["boundary_policy_item_id"],
                    row["review_decision_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row.get("source_line_number"),
                    row["boundary_agent_key"],
                    row["boundary_policy"],
                    row["policy_bucket"],
                    row["risk_level"],
                    row["subpolicy_name"],
                    row["checkpoint_name"],
                    row["policy_action"],
                    int(row["policy_allowed"]),
                    row["block_reason"],
                    row.get("current_confirmed_text_hash"),
                    row.get("corrected_text_hash"),
                    row.get("evidence_json"),
                    now,
                ),
            )
            row["policy_item_id"] = int(item_cursor.lastrowid)

        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            policy_run_id=policy_run_id,
            checkpoints=checkpoints,
            rows=rows,
            started_at=started_at,
            policy_status=policy_status,
            global_reasons=reasons,
        )
        conn.commit()

    print("[auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_policy] Policy generated")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_policy] Run id: {policy_run_id}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_policy] Checkpoint runs: {ids}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_policy] Status: {policy_status}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_policy] Released shadow: {counts['released_shadow']:,}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_policy] Blocked: {len(rows) - counts['released_shadow']:,}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_policy] Report: {txt_path}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_policy] CSV: {csv_path}")
    print(f"[auto_confirmation_reopen_text_boundary_token_subpolicy_lifecycle_policy] JSONL: {jsonl_path}")
    return {
        "run_id": policy_run_id,
        "checkpoint_run_ids": ids,
        "policy_status": policy_status,
        "released_shadow": counts["released_shadow"],
        "blocked": len(rows) - counts["released_shadow"],
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


def parse_ids(value: str | None) -> list[int] | None:
    if not value:
        return None
    return [int(part.strip()) for part in value.split(",") if part.strip()]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a shadow lifecycle policy for token-boundary subpolicy checkpoints.")
    parser.add_argument("--checkpoint-run-ids", default=None, help="Comma-separated checkpoint run IDs. Default: latest ready checkpoint per subpolicy.")
    parser.add_argument("--status", choices=["shadow"], default="shadow")
    parser.add_argument("--min-checkpoints", type=int, default=4)
    parser.add_argument("--expected-total", type=int, default=None)
    args = parser.parse_args()
    main(
        checkpoint_run_ids=parse_ids(args.checkpoint_run_ids),
        policy_status=args.status,
        min_checkpoints=args.min_checkpoints,
        expected_total=args.expected_total,
    )
