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


RULE_VERSION = "issue_review_gender_subpolicy_shadow_v1"
POLICY_NAME = "gender_issue_review_subpolicy_shadow"
AGENT_KEY = "micro_gender_token"

NEGATIVE_SUBPOLICIES = {
    "gender_token_mismatch_guard",
    "gender_token_extra_prefix_guard",
    "select_cstring_residual_spanish_guard",
    "gender_visible_spanish_residual_guard",
    "gender_surface_boundary_guard",
}


def stable_hash(value: str | None) -> str:
    return hashlib.sha1((value or "").encode("utf-8")).hexdigest()


def parse_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_gender_subpolicy_shadow"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_decision_run_id(conn, *, agent_key: str, queue_run_id: int | None) -> int:
    params: list[Any] = [agent_key]
    queue_filter = ""
    if queue_run_id is not None:
        queue_filter = "AND queue_run_id = ?"
        params.append(queue_run_id)
    row = conn.execute(
        f"""
        SELECT id
        FROM ml_issue_review_decision_runs
        WHERE agent_key = ?
          AND finished_at IS NOT NULL
          AND accepted_count > 0
          {queue_filter}
        ORDER BY id DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No completed issue-review decision run found for {agent_key!r}.")
    return int(row["id"])


def fetch_decision_run(conn, *, decision_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_review_decision_runs
        WHERE id = ?
        """,
        (decision_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Issue-review decision run not found: {decision_run_id}")
    return dict(row)


def fetch_rows(
    conn,
    *,
    agent_key: str,
    decision_run_id: int,
    queue_run_id: int | None,
) -> list[dict[str, Any]]:
    params: list[Any] = [decision_run_id, agent_key]
    queue_filter = ""
    if queue_run_id is not None:
        queue_filter = "AND d.queue_run_id = ?"
        params.append(queue_run_id)
    rows = conn.execute(
        f"""
        SELECT
            d.id AS decision_id,
            d.run_id AS decision_run_id,
            d.queue_run_id,
            d.queue_item_id,
            d.ledger_run_id,
            d.ledger_item_id,
            d.segment_id,
            d.relative_path,
            d.source_key,
            d.source_line_number,
            d.agent_key,
            d.issue_family,
            d.issue_kind,
            d.queue_bucket,
            d.normalized_decision,
            d.evidence_label,
            d.corrected_text AS decision_corrected_text,
            d.notes AS decision_notes,
            d.valid AS decision_valid,
            d.validation_status AS decision_validation_status,
            q.review_status AS queue_review_status,
            q.reviewer_decision AS queue_reviewer_decision,
            q.confirmed_text AS queue_confirmed_text,
            q.evidence_json AS queue_evidence_json,
            l.token_impact,
            l.token_status AS ledger_token_status,
            l.evidence_json AS ledger_evidence_json,
            c.confirmed_text AS current_confirmed_text,
            c.locked AS confirmation_locked,
            source.english_text,
            source.spanish_text,
            output.portuguese_text
        FROM ml_issue_review_decisions d
        JOIN ml_issue_review_queue_items q ON q.id = d.queue_item_id
        JOIN ml_issue_ledger_items l ON l.id = d.ledger_item_id
        JOIN source_segments source ON source.id = d.segment_id
        LEFT JOIN output_segments output ON output.segment_id = d.segment_id
        LEFT JOIN segment_confirmations c
          ON c.id = (
              SELECT c2.id
              FROM segment_confirmations c2
              WHERE c2.segment_id = d.segment_id
              ORDER BY c2.updated_at DESC, c2.id DESC
              LIMIT 1
          )
        WHERE d.run_id = ?
          AND d.agent_key = ?
          AND d.valid = 1
          {queue_filter}
        ORDER BY d.relative_path, d.source_line_number, d.source_key, d.id
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def evidence_payload(row: dict[str, Any]) -> dict[str, Any]:
    queue_evidence = parse_json(row.get("queue_evidence_json"), {})
    ledger_evidence = parse_json(row.get("ledger_evidence_json"), {})
    payload: dict[str, Any] = {}
    if isinstance(ledger_evidence, dict):
        payload.update(ledger_evidence)
    if isinstance(queue_evidence, dict):
        payload.update(queue_evidence)
    return payload


def evidence_numbers(row: dict[str, Any]) -> tuple[int, int, int, list[str]]:
    evidence = evidence_payload(row)
    text = row.get("current_confirmed_text") or row.get("queue_confirmed_text") or row.get("portuguese_text") or ""
    text_length = as_int(evidence.get("text_length"), len(text))
    token_count = as_int(evidence.get("token_count"))
    word_count = as_int(evidence.get("word_count"))
    raw_codes = evidence.get("issue_codes")
    issue_codes = [str(item) for item in raw_codes] if isinstance(raw_codes, list) else []
    return text_length, token_count, word_count, sorted(set(issue_codes))


def classify_subpolicy(row: dict[str, Any]) -> tuple[str, str]:
    notes = str(row.get("decision_notes") or "").lower()
    issue_kind = str(row.get("issue_kind") or "")
    text = " ".join(
        str(row.get(key) or "")
        for key in ("current_confirmed_text", "queue_confirmed_text", "portuguese_text")
    )

    if row.get("normalized_decision") == "needs_new_microagent":
        if "gender_token_mismatch" in notes:
            return "gender_token_mismatch_guard", "token_mismatch_boundary"
        if "gender_token_extra_prefix" in notes:
            return "gender_token_extra_prefix_guard", "extra_prefix_boundary"
        return "gender_new_microagent_router", "unclassified_new_microagent"

    if row.get("normalized_decision") == "needs_repair":
        if "spanish_residual" in notes:
            if issue_kind == "select_cstring_gender_literal" or "select_cstring" in text.lower():
                return "select_cstring_residual_spanish_guard", "select_cstring_spanish_residual"
            return "gender_visible_spanish_residual_guard", "visible_gender_spanish_residual"
        if "surface_boundary" in notes:
            return "gender_surface_boundary_guard", "surface_boundary"
        return "gender_repair_router", "unclassified_repair"

    if row.get("normalized_decision") == "false_positive_reopen":
        return "gender_false_reopen_safe_candidate", "active_safe_false_reopen"

    if row.get("normalized_decision") == "needs_domain_context":
        return "gender_context_router", "context_required"

    return "gender_unclassified_review", "unclassified"


def evaluate_row(row: dict[str, Any], *, global_reasons: list[str]) -> dict[str, Any]:
    subpolicy_name, reason = classify_subpolicy(row)
    text_length, token_count, word_count, issue_codes = evidence_numbers(row)
    blockers = list(global_reasons)

    if row.get("queue_review_status") != "reviewed":
        blockers.append("queue_item_not_reviewed")
    if row.get("queue_reviewer_decision") != row.get("normalized_decision"):
        blockers.append("queue_decision_mismatch")
    if row.get("decision_validation_status") != "accepted":
        blockers.append("decision_not_accepted")

    normalized = row.get("normalized_decision")
    if subpolicy_name in NEGATIVE_SUBPOLICIES and normalized not in {"needs_repair", "needs_new_microagent"}:
        blockers.append("negative_subpolicy_requires_negative_decision")
    if subpolicy_name == "gender_false_reopen_safe_candidate" and normalized != "false_positive_reopen":
        blockers.append("positive_subpolicy_requires_false_positive_reopen")
    if subpolicy_name == "gender_context_router" and normalized != "needs_domain_context":
        blockers.append("context_router_requires_context_decision")
    if subpolicy_name.startswith("gender_unclassified") or subpolicy_name.endswith("_router") and reason.startswith("unclassified"):
        blockers.append("unclassified_subpolicy")

    if blockers:
        shadow_status = "shadow_blocked"
        shadow_action = "hold_for_manual_policy_review"
        block_reason = ",".join(blockers)
    elif subpolicy_name in NEGATIVE_SUBPOLICIES:
        shadow_status = "shadow_ready_boundary"
        shadow_action = "would_block_auto_safe_shadow"
        block_reason = None
    elif subpolicy_name == "gender_false_reopen_safe_candidate":
        shadow_status = "shadow_ready_positive"
        shadow_action = "would_release_false_reopen_shadow"
        block_reason = None
    elif subpolicy_name == "gender_context_router":
        shadow_status = "shadow_ready_context"
        shadow_action = "would_route_context_shadow"
        block_reason = None
    else:
        shadow_status = "shadow_blocked"
        shadow_action = "hold_for_manual_policy_review"
        block_reason = "unsupported_subpolicy"

    return {
        "subpolicy_name": subpolicy_name,
        "shadow_status": shadow_status,
        "shadow_action": shadow_action,
        "block_reason": block_reason,
        "text_length": text_length,
        "token_count": token_count,
        "word_count": word_count,
        "issue_codes": issue_codes,
        "evidence": {
            "classification_reason": reason,
            "issue_codes": issue_codes,
            "text_length": text_length,
            "token_count": token_count,
            "word_count": word_count,
            "decision_notes": row.get("decision_notes"),
            "english_text_preview": (row.get("english_text") or "")[:240],
            "spanish_text_preview": (row.get("spanish_text") or "")[:240],
            "confirmed_text_preview": (
                row.get("current_confirmed_text")
                or row.get("queue_confirmed_text")
                or row.get("portuguese_text")
                or ""
            )[:240],
        },
    }


def global_block_reasons(*, policy_status: str, decision_run: dict[str, Any], rows: list[dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    if policy_status != "shadow":
        reasons.append("policy_status_must_remain_shadow")
    if decision_run.get("agent_key") != AGENT_KEY:
        reasons.append("decision_run_agent_mismatch")
    if int(decision_run.get("accepted_count") or 0) <= 0:
        reasons.append("decision_run_has_no_accepted_rows")
    if not rows:
        reasons.append("no_candidate_rows")
    return reasons


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    decision_run: dict[str, Any],
    rows: list[dict[str, Any]],
) -> None:
    fieldnames = [
        "shadow_item_id",
        "segment_id",
        "relative_path",
        "source_line_number",
        "source_key",
        "normalized_decision",
        "issue_kind",
        "subpolicy_name",
        "shadow_status",
        "shadow_action",
        "block_reason",
        "token_status",
        "token_impact",
        "text_length",
        "token_count",
        "word_count",
        "issue_codes",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    by_status = Counter(row["shadow_status"] for row in rows)
    by_subpolicy = Counter(row["subpolicy_name"] for row in rows)
    by_action = Counter(row["shadow_action"] for row in rows)
    by_blocker = Counter(row["block_reason"] or "none" for row in rows)
    lines = [
        "Issue-review gender subpolicy shadow",
        f"Rule version: {RULE_VERSION}",
        f"Policy: {POLICY_NAME} (shadow)",
        f"Run id: {run_id}",
        f"Decision run id: {decision_run['id']}",
        f"Queue run id: {decision_run.get('queue_run_id')}",
        "",
        "Counts:",
        f"- Candidates: {len(rows):,}",
        f"- Shadow ready: {sum(1 for row in rows if str(row['shadow_status']).startswith('shadow_ready')):,}",
        f"- Blocked: {by_status['shadow_blocked']:,}",
        "",
        "By status:",
        *[f"- {key}: {value:,}" for key, value in by_status.most_common()],
        "",
        "By subpolicy:",
        *[f"- {key}: {value:,}" for key, value in by_subpolicy.most_common()],
        "",
        "By action:",
        *[f"- {key}: {value:,}" for key, value in by_action.most_common()],
        "",
        "By blocker:",
        *[f"- {key}: {value:,}" for key, value in by_blocker.most_common()],
        "",
        "Ready boundary samples:",
    ]
    for row in [item for item in rows if item["shadow_status"] == "shadow_ready_boundary"][:20]:
        lines.append(
            f"- {row['subpolicy_name']} | {row['relative_path']}:{row['source_line_number']}:{row['source_key']}"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Shadow only: no model promotion, no confirmations, no source/output writes.",
            "- Boundary rows may block or route future automation only after checkpoint/lifecycle integration.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def upsert_registry_agents(conn, now: str, rows: list[dict[str, Any]]) -> None:
    counts = Counter(row["subpolicy_name"] for row in rows)
    specs = {
        "gender_token_mismatch_guard": {
            "parent": "gender_form_swap_subpolicy",
            "role": "negative_boundary",
            "description": "Shadow guard for gender-token mismatches such as missing/changed CK3 gender Custom tokens.",
        },
        "gender_token_extra_prefix_guard": {
            "parent": "gender_form_swap_subpolicy",
            "role": "negative_boundary",
            "description": "Shadow guard for redundant visible gender prefixes glued to CK3 gender tokens.",
        },
        "select_cstring_residual_spanish_guard": {
            "parent": "select_cstring_ui_subpolicy",
            "role": "negative_boundary",
            "description": "Shadow guard for Select_CString literals that still contain Spanish words.",
        },
        "gender_visible_spanish_residual_guard": {
            "parent": "gender_form_swap_subpolicy",
            "role": "negative_boundary",
            "description": "Shadow guard for visible Spanish residue around CK3 gender helper tokens.",
        },
        "gender_false_reopen_safe_candidate": {
            "parent": "gender_form_swap_subpolicy",
            "role": "guarded_release_candidate",
            "description": "Shadow positive candidate for reviewed gender-token reopens that are already safe.",
        },
        "gender_context_router": {
            "parent": "coordinator_ensemble_v1",
            "role": "route_and_arbitrate",
            "description": "Routes unresolved gender-token cases to context/domain specialists instead of auto-releasing.",
        },
    }
    for agent_key, spec in specs.items():
        if counts[agent_key] <= 0:
            continue
        conn.execute(
            """
            INSERT INTO ml_agent_registry (
                agent_key,
                agent_type,
                parent_agent_key,
                model_kind,
                status,
                operational_state,
                decision_role,
                scope_group,
                scope_sql,
                scope_description,
                default_threshold,
                priority,
                dashboard_group,
                created_at,
                updated_at,
                notes_json
            )
            VALUES (?, ?, ?, NULL, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_key) DO UPDATE SET
                agent_type = excluded.agent_type,
                parent_agent_key = excluded.parent_agent_key,
                status = excluded.status,
                operational_state = excluded.operational_state,
                decision_role = excluded.decision_role,
                scope_group = excluded.scope_group,
                scope_description = excluded.scope_description,
                priority = excluded.priority,
                dashboard_group = excluded.dashboard_group,
                updated_at = excluded.updated_at,
                notes_json = excluded.notes_json
            """,
            (
                agent_key,
                "symbolic_guard" if spec["role"] == "negative_boundary" else "symbolic_subpolicy",
                spec["parent"],
                "experimental",
                "shadow",
                spec["role"],
                "issue_review_gender",
                spec["description"],
                43,
                "Token Gate",
                now,
                now,
                json.dumps(
                    {
                        "source": POLICY_NAME,
                        "evidence_count": counts[agent_key],
                        "auto_apply_allowed": 0,
                        "status_note": "Created from issue-review gender queue evidence; shadow only.",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )


def main(
    *,
    decision_run_id: int | None = None,
    queue_run_id: int | None = None,
    agent_key: str = AGENT_KEY,
    policy_status: str = "shadow",
) -> dict[str, Any]:
    settings = db.load_settings()
    txt_path, csv_path, jsonl_path = report_paths(settings)
    started_at = db.utc_now()

    with db.connect(settings) as conn:
        selected_decision_run_id = decision_run_id or latest_decision_run_id(
            conn,
            agent_key=agent_key,
            queue_run_id=queue_run_id,
        )
        decision_run = fetch_decision_run(conn, decision_run_id=selected_decision_run_id)
        selected_queue_run_id = queue_run_id or int(decision_run.get("queue_run_id") or 0) or None
        rows = fetch_rows(
            conn,
            agent_key=agent_key,
            decision_run_id=selected_decision_run_id,
            queue_run_id=selected_queue_run_id,
        )
        global_reasons = global_block_reasons(policy_status=policy_status, decision_run=decision_run, rows=rows)

        evaluated_rows: list[dict[str, Any]] = []
        for row in rows:
            evaluation = evaluate_row(row, global_reasons=global_reasons)
            text_length = int(evaluation["text_length"])
            token_count = int(evaluation["token_count"])
            word_count = int(evaluation["word_count"])
            issue_codes = evaluation["issue_codes"]
            evaluated_rows.append(
                {
                    "decision_id": row["decision_id"],
                    "decision_run_id": row["decision_run_id"],
                    "queue_run_id": row["queue_run_id"],
                    "queue_item_id": row["queue_item_id"],
                    "ledger_run_id": row["ledger_run_id"],
                    "ledger_item_id": row["ledger_item_id"],
                    "segment_id": row["segment_id"],
                    "relative_path": row["relative_path"],
                    "source_key": row["source_key"],
                    "source_line_number": row["source_line_number"],
                    "queue_bucket": row["queue_bucket"],
                    "issue_family": row["issue_family"],
                    "issue_kind": row["issue_kind"],
                    "normalized_decision": row["normalized_decision"],
                    "evidence_label": row["evidence_label"],
                    "subpolicy_name": evaluation["subpolicy_name"],
                    "shadow_status": evaluation["shadow_status"],
                    "shadow_action": evaluation["shadow_action"],
                    "block_reason": evaluation["block_reason"],
                    "token_impact": row.get("token_impact"),
                    "token_status": row.get("ledger_token_status"),
                    "text_length": text_length,
                    "token_count": token_count,
                    "word_count": word_count,
                    "issue_codes": issue_codes,
                    "issue_codes_json": json.dumps(issue_codes, ensure_ascii=False, sort_keys=True),
                    "evidence_json": json.dumps(evaluation["evidence"], ensure_ascii=False, sort_keys=True),
                    "notes": row.get("decision_notes"),
                    "current_confirmed_text_hash": stable_hash(row.get("current_confirmed_text")),
                    "queue_confirmed_text_hash": stable_hash(row.get("queue_confirmed_text")),
                }
            )

        by_subpolicy = Counter(row["subpolicy_name"] for row in evaluated_rows)
        by_action = Counter(row["shadow_action"] for row in evaluated_rows)
        by_blocker = Counter(row["block_reason"] or "none" for row in evaluated_rows)
        ready_count = sum(1 for row in evaluated_rows if str(row["shadow_status"]).startswith("shadow_ready"))
        blocked_count = sum(1 for row in evaluated_rows if row["shadow_status"] == "shadow_blocked")
        boundary_count = sum(1 for row in evaluated_rows if row["subpolicy_name"] in NEGATIVE_SUBPOLICIES)
        context_count = sum(1 for row in evaluated_rows if row["subpolicy_name"] == "gender_context_router")
        positive_count = sum(1 for row in evaluated_rows if row["subpolicy_name"] == "gender_false_reopen_safe_candidate")
        finished_at = db.utc_now()

        cursor = conn.execute(
            """
            INSERT INTO ml_issue_gender_subpolicy_shadow_runs (
                rule_version,
                policy_name,
                policy_status,
                agent_key,
                decision_run_id,
                queue_run_id,
                candidate_count,
                shadow_ready_count,
                blocked_count,
                boundary_count,
                context_count,
                positive_count,
                subpolicy_counts_json,
                action_counts_json,
                blocker_counts_json,
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
                POLICY_NAME,
                policy_status,
                agent_key,
                selected_decision_run_id,
                selected_queue_run_id,
                len(evaluated_rows),
                ready_count,
                blocked_count,
                boundary_count,
                context_count,
                positive_count,
                json.dumps(dict(by_subpolicy), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(by_action), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(by_blocker), ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                finished_at,
                finished_at,
            ),
        )
        run_id = int(cursor.lastrowid)
        now = db.utc_now()
        for row in evaluated_rows:
            cursor = conn.execute(
                """
                INSERT INTO ml_issue_gender_subpolicy_shadow_items (
                    run_id,
                    decision_id,
                    decision_run_id,
                    queue_run_id,
                    queue_item_id,
                    ledger_run_id,
                    ledger_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    queue_bucket,
                    issue_family,
                    issue_kind,
                    normalized_decision,
                    evidence_label,
                    subpolicy_name,
                    shadow_status,
                    shadow_action,
                    block_reason,
                    token_impact,
                    token_status,
                    text_length,
                    token_count,
                    word_count,
                    issue_codes_json,
                    evidence_json,
                    notes,
                    current_confirmed_text_hash,
                    queue_confirmed_text_hash,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    row["decision_id"],
                    row["decision_run_id"],
                    row["queue_run_id"],
                    row["queue_item_id"],
                    row["ledger_run_id"],
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["queue_bucket"],
                    row["issue_family"],
                    row["issue_kind"],
                    row["normalized_decision"],
                    row["evidence_label"],
                    row["subpolicy_name"],
                    row["shadow_status"],
                    row["shadow_action"],
                    row["block_reason"],
                    row["token_impact"],
                    row["token_status"],
                    row["text_length"],
                    row["token_count"],
                    row["word_count"],
                    row["issue_codes_json"],
                    row["evidence_json"],
                    row["notes"],
                    row["current_confirmed_text_hash"],
                    row["queue_confirmed_text_hash"],
                    now,
                ),
            )
            row["shadow_item_id"] = int(cursor.lastrowid)
        upsert_registry_agents(conn, now, evaluated_rows)
        conn.commit()

    write_outputs(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        run_id=run_id,
        decision_run=decision_run,
        rows=evaluated_rows,
    )
    print("[issue_review_gender_subpolicy_shadow] Shadow policy generated")
    print(f"[issue_review_gender_subpolicy_shadow] Run id: {run_id}")
    print(f"[issue_review_gender_subpolicy_shadow] Decision run id: {selected_decision_run_id}")
    print(f"[issue_review_gender_subpolicy_shadow] Queue run id: {selected_queue_run_id}")
    print(f"[issue_review_gender_subpolicy_shadow] Candidates: {len(evaluated_rows):,}")
    print(f"[issue_review_gender_subpolicy_shadow] Shadow ready: {ready_count:,}")
    print(f"[issue_review_gender_subpolicy_shadow] Blocked: {blocked_count:,}")
    print(f"[issue_review_gender_subpolicy_shadow] Report: {txt_path}")
    return {
        "run_id": run_id,
        "decision_run_id": selected_decision_run_id,
        "queue_run_id": selected_queue_run_id,
        "candidate_count": len(evaluated_rows),
        "shadow_ready_count": ready_count,
        "blocked_count": blocked_count,
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a shadow issue-review gender subpolicy from reviewed queue decisions.")
    parser.add_argument("--decision-run-id", type=int, default=None)
    parser.add_argument("--queue-run-id", type=int, default=None)
    parser.add_argument("--agent-key", default=AGENT_KEY)
    parser.add_argument("--status", choices=["shadow"], default="shadow")
    args = parser.parse_args()
    main(
        decision_run_id=args.decision_run_id,
        queue_run_id=args.queue_run_id,
        agent_key=args.agent_key,
        policy_status=args.status,
    )
