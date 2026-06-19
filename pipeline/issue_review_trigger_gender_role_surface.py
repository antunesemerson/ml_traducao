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


RULE_VERSION = "issue_review_trigger_gender_role_surface_v1"
POLICY_NAME = "trigger_gender_role_surface_shadow"
AGENT_KEY = "trigger_gender_role_surface"
SOURCE_AGENT_KEY = "micro_short_label_style"
TRIGGER_PATH = "triggers/character_triggers_l_spanish.yml"

KINSHIP_PAIR_KEYS = {
    "IS_HEIR_OF_CHARACTER_TRIGGER",
    "IS_COUSIN_OF_CHARACTER_TRIGGER",
}
KINSHIP_LEXICAL_KEYS = {
    "IS_TWIN_OF_CHARACTER_TRIGGER",
}
KINSHIP_STEM_KEYS = {
    "IS_GREAT_GRANDPARENT_OF_CHARACTER_TRIGGER",
    "IS_GRANDPARENT_OF_CHARACTER_TRIGGER",
    "IS_GRANDCHILD_OF_CHARACTER_TRIGGER",
    "IS_NIBLING_OF_CHARACTER_TRIGGER",
}
ROLE_ARTICLE_KEYS = {
    "I_AM_A_VASSAL_OF",
    "IS_LANDLESS_ADVENTURER_TRIGGER",
    "NONE_ARE_LANDLESS_ADVENTURER_TRIGGER",
    "IS_A_COURTIER_TRIGGER",
    "THEY_ARE_CLAIMANT_TRIGGER",
}

SUBPOLICY_ACTIONS = {
    "trigger_kinship_pair_form_boundary": "observe_trigger_kinship_pair_form",
    "trigger_kinship_lexical_gender_boundary": "observe_trigger_kinship_lexical_gender",
    "trigger_kinship_stem_fragment_boundary": "observe_trigger_kinship_stem_fragment",
    "trigger_role_article_boundary": "observe_trigger_role_article",
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
    base = reports_dir / f"{stamp}_issue_trigger_gender_role_surface"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def latest_decision_run_id(conn, *, source_agent_key: str, queue_run_id: int | None) -> int:
    params: list[Any] = [source_agent_key, TRIGGER_PATH]
    queue_filter = ""
    if queue_run_id is not None:
        queue_filter = "AND d.queue_run_id = ?"
        params.append(queue_run_id)
    row = conn.execute(
        f"""
        SELECT d.run_id AS id
        FROM ml_issue_review_decisions d
        JOIN ml_issue_review_decision_runs r ON r.id = d.run_id
        WHERE d.agent_key = ?
          AND d.relative_path = ?
          AND d.normalized_decision = 'needs_new_microagent'
          AND d.valid = 1
          AND r.finished_at IS NOT NULL
          AND d.notes LIKE '%trigger_%'
          {queue_filter}
        GROUP BY d.run_id
        ORDER BY d.run_id DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"No trigger gender-role issue-review decision run found for {source_agent_key!r}.")
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
    decision_run_id: int,
    queue_run_id: int | None,
    source_agent_key: str,
) -> list[dict[str, Any]]:
    params: list[Any] = [decision_run_id, source_agent_key, TRIGGER_PATH]
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
            d.agent_key AS source_agent_key,
            d.issue_family,
            d.issue_kind,
            d.queue_bucket,
            d.normalized_decision,
            d.evidence_label,
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
          AND d.relative_path = ?
          AND d.normalized_decision = 'needs_new_microagent'
          AND d.valid = 1
          AND d.notes LIKE '%trigger_%'
          {queue_filter}
        ORDER BY d.source_line_number, d.source_key, d.id
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
    text = (
        row.get("current_confirmed_text")
        or row.get("queue_confirmed_text")
        or row.get("portuguese_text")
        or ""
    )
    text_length = as_int(evidence.get("text_length"), len(text))
    token_count = as_int(evidence.get("token_count"))
    word_count = as_int(evidence.get("word_count"))
    raw_codes = evidence.get("issue_codes")
    issue_codes = [str(item) for item in raw_codes] if isinstance(raw_codes, list) else []
    return text_length, token_count, word_count, sorted(set(issue_codes))


def classify_subpolicy(row: dict[str, Any]) -> tuple[str, str]:
    source_key = str(row.get("source_key") or "").upper()
    if source_key in KINSHIP_PAIR_KEYS:
        return "trigger_kinship_pair_form_boundary", "pair_form_requires_gender_policy"
    if source_key in KINSHIP_LEXICAL_KEYS:
        return "trigger_kinship_lexical_gender_boundary", "lexical_gender_requires_context_policy"
    if source_key in KINSHIP_STEM_KEYS:
        return "trigger_kinship_stem_fragment_boundary", "stem_fragment_requires_gender_policy"
    if source_key in ROLE_ARTICLE_KEYS:
        return "trigger_role_article_boundary", "role_article_requires_target_gender_policy"
    return "trigger_gender_role_unclassified", "unclassified_trigger_gender_role_surface"


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
    if row.get("normalized_decision") != "needs_new_microagent":
        blockers.append("requires_needs_new_microagent_decision")
    if row.get("relative_path") != TRIGGER_PATH:
        blockers.append("wrong_trigger_path")
    if "trigger_" not in str(row.get("decision_notes") or ""):
        blockers.append("missing_trigger_reason_note")
    if row.get("token_impact") == "token_mismatch" or row.get("ledger_token_status") == "mismatch":
        blockers.append("token_mismatch_not_owned_by_surface_microagent")
    if subpolicy_name not in SUBPOLICY_ACTIONS:
        blockers.append("unclassified_trigger_surface")

    if blockers:
        shadow_status = "shadow_blocked"
        shadow_action = "hold_for_manual_policy_review"
        block_reason = ",".join(blockers)
    else:
        shadow_status = "shadow_ready_observation"
        shadow_action = SUBPOLICY_ACTIONS[subpolicy_name]
        block_reason = None

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
            "english_text_preview": short(row.get("english_text"), 240),
            "spanish_text_preview": short(row.get("spanish_text"), 240),
            "confirmed_text_preview": short(
                row.get("current_confirmed_text")
                or row.get("queue_confirmed_text")
                or row.get("portuguese_text"),
                240,
            ),
        },
    }


def global_block_reasons(*, policy_status: str, decision_run: dict[str, Any], rows: list[dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    if policy_status != "shadow":
        reasons.append("policy_status_must_remain_shadow")
    if decision_run.get("agent_key") != SOURCE_AGENT_KEY:
        reasons.append("decision_run_source_agent_mismatch")
    if int(decision_run.get("accepted_count") or 0) <= 0:
        reasons.append("decision_run_has_no_accepted_rows")
    if not rows:
        reasons.append("no_trigger_surface_rows")
    return reasons


def analytics_summary(decision_run: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    total_rows = int(decision_run.get("total_rows") or 0)
    delegated_count = len(rows)
    false_positive_count = int(decision_run.get("false_positive_count") or 0)
    new_microagent_count = int(decision_run.get("new_microagent_count") or 0)
    return {
        "decision_run_total_rows": total_rows,
        "accepted_count": int(decision_run.get("accepted_count") or 0),
        "false_positive_reopen_count": false_positive_count,
        "new_microagent_count": new_microagent_count,
        "trigger_gender_role_delegated_count": delegated_count,
        "trigger_gender_role_delegated_rate": (delegated_count / total_rows) if total_rows else 0,
        "protected_from_generic_positive_count": delegated_count,
        "protected_from_generic_positive_rate": (delegated_count / total_rows) if total_rows else 0,
    }


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    decision_run: dict[str, Any],
    rows: list[dict[str, Any]],
    analytics: dict[str, Any],
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
    delegated_rate = float(analytics.get("trigger_gender_role_delegated_rate") or 0)
    lines = [
        "Issue-review trigger gender-role surface shadow",
        f"Rule version: {RULE_VERSION}",
        f"Policy: {POLICY_NAME} (shadow)",
        f"Run id: {run_id}",
        f"Decision run id: {decision_run['id']}",
        f"Queue run id: {decision_run.get('queue_run_id')}",
        "",
        "Data Analytics impact:",
        f"- Decision run rows: {int(analytics['decision_run_total_rows']):,}",
        f"- Generic positives released elsewhere: {int(analytics['false_positive_reopen_count']):,}",
        f"- Trigger gender-role delegates: {int(analytics['trigger_gender_role_delegated_count']):,} ({delegated_rate:.2%})",
        f"- Protected from generic positive contamination: {int(analytics['protected_from_generic_positive_count']):,}",
        "",
        "Counts:",
        f"- Candidates: {len(rows):,}",
        f"- Shadow ready: {sum(1 for row in rows if row['shadow_status'] == 'shadow_ready_observation'):,}",
        f"- Blocked: {by_status['shadow_blocked']:,}",
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
        "Ready observation samples:",
    ]
    for row in [item for item in rows if item["shadow_status"] == "shadow_ready_observation"][:20]:
        lines.append(
            f"- {row['subpolicy_name']} | {row['relative_path']}:{row['source_line_number']}:{row['source_key']}"
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Shadow only: no model promotion, no confirmations, no source/output writes.",
            "- This microagent observes which trigger phrases need gender/role policy before any production effect.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def upsert_registry_agents(conn, now: str, rows: list[dict[str, Any]]) -> None:
    counts = Counter(row["subpolicy_name"] for row in rows)
    total = sum(counts.values())
    specs = {
        AGENT_KEY: {
            "agent_type": "microagent",
            "parent": "coordinator_ensemble_v1",
            "role": "route_and_observe",
            "scope_group": "issue_review_trigger_gender_role",
            "description": "Routes trigger short labels where kinship gender or role articles need a dedicated policy before release.",
            "evidence_count": total,
            "priority": 42,
        },
        "trigger_kinship_pair_form_boundary": {
            "agent_type": "symbolic_guard",
            "parent": AGENT_KEY,
            "role": "negative_boundary",
            "scope_group": "issue_review_trigger_gender_role",
            "description": "Observes pair-form kinship labels such as herdeiro/a and primo/prima before any automated release.",
            "evidence_count": counts["trigger_kinship_pair_form_boundary"],
            "priority": 43,
        },
        "trigger_kinship_lexical_gender_boundary": {
            "agent_type": "symbolic_guard",
            "parent": AGENT_KEY,
            "role": "negative_boundary",
            "scope_group": "issue_review_trigger_gender_role",
            "description": "Observes kinship labels whose Portuguese noun changes by gender, such as gemeo/gemea.",
            "evidence_count": counts["trigger_kinship_lexical_gender_boundary"],
            "priority": 44,
        },
        "trigger_kinship_stem_fragment_boundary": {
            "agent_type": "symbolic_guard",
            "parent": AGENT_KEY,
            "role": "negative_boundary",
            "scope_group": "issue_review_trigger_gender_role",
            "description": "Observes stem-fragment kinship labels such as av, bisav, net and sobrinh that require gender-safe rendering.",
            "evidence_count": counts["trigger_kinship_stem_fragment_boundary"],
            "priority": 45,
        },
        "trigger_role_article_boundary": {
            "agent_type": "symbolic_guard",
            "parent": AGENT_KEY,
            "role": "negative_boundary",
            "scope_group": "issue_review_trigger_gender_role",
            "description": "Observes trigger role labels with visible articles such as um courtier, um adventurer, um reclamante.",
            "evidence_count": counts["trigger_role_article_boundary"],
            "priority": 46,
        },
    }
    for agent_key, spec in specs.items():
        if spec["evidence_count"] <= 0:
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
                spec["agent_type"],
                spec["parent"],
                "experimental",
                "shadow",
                spec["role"],
                spec["scope_group"],
                spec["description"],
                spec["priority"],
                "Token Gate",
                now,
                now,
                json.dumps(
                    {
                        "source": POLICY_NAME,
                        "evidence_count": spec["evidence_count"],
                        "auto_apply_allowed": 0,
                        "status_note": "Created from trigger gender-role issue-review evidence; shadow only.",
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
    source_agent_key: str = SOURCE_AGENT_KEY,
    policy_status: str = "shadow",
) -> dict[str, Any]:
    settings = db.load_settings()
    txt_path, csv_path, jsonl_path = report_paths(settings)
    started_at = db.utc_now()

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_decision_run_id = decision_run_id or latest_decision_run_id(
            conn,
            source_agent_key=source_agent_key,
            queue_run_id=queue_run_id,
        )
        decision_run = fetch_decision_run(conn, decision_run_id=selected_decision_run_id)
        selected_queue_run_id = queue_run_id or int(decision_run.get("queue_run_id") or 0) or None
        rows = fetch_rows(
            conn,
            decision_run_id=selected_decision_run_id,
            queue_run_id=selected_queue_run_id,
            source_agent_key=source_agent_key,
        )
        global_reasons = global_block_reasons(
            policy_status=policy_status,
            decision_run=decision_run,
            rows=rows,
        )

        evaluated_rows: list[dict[str, Any]] = []
        for row in rows:
            evaluation = evaluate_row(row, global_reasons=global_reasons)
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
                    "text_length": int(evaluation["text_length"]),
                    "token_count": int(evaluation["token_count"]),
                    "word_count": int(evaluation["word_count"]),
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
        ready_count = sum(1 for row in evaluated_rows if row["shadow_status"] == "shadow_ready_observation")
        blocked_count = sum(1 for row in evaluated_rows if row["shadow_status"] == "shadow_blocked")
        kinship_count = sum(1 for row in evaluated_rows if row["subpolicy_name"].startswith("trigger_kinship_"))
        role_article_count = int(by_subpolicy.get("trigger_role_article_boundary", 0))
        analytics = analytics_summary(decision_run, evaluated_rows)
        finished_at = db.utc_now()

        cursor = conn.execute(
            """
            INSERT INTO ml_issue_trigger_gender_role_surface_runs (
                rule_version,
                policy_name,
                policy_status,
                agent_key,
                decision_run_id,
                queue_run_id,
                candidate_count,
                shadow_ready_count,
                blocked_count,
                kinship_count,
                role_article_count,
                subpolicy_counts_json,
                action_counts_json,
                blocker_counts_json,
                analytics_json,
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
                AGENT_KEY,
                selected_decision_run_id,
                selected_queue_run_id,
                len(evaluated_rows),
                ready_count,
                blocked_count,
                kinship_count,
                role_article_count,
                json.dumps(dict(by_subpolicy), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(by_action), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(by_blocker), ensure_ascii=False, sort_keys=True),
                json.dumps(analytics, ensure_ascii=False, sort_keys=True),
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
            item_cursor = conn.execute(
                """
                INSERT INTO ml_issue_trigger_gender_role_surface_items (
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
                    row.get("source_line_number"),
                    row["queue_bucket"],
                    row["issue_family"],
                    row["issue_kind"],
                    row["normalized_decision"],
                    row["evidence_label"],
                    row["subpolicy_name"],
                    row["shadow_status"],
                    row["shadow_action"],
                    row["block_reason"],
                    row.get("token_impact"),
                    row.get("token_status"),
                    int(row.get("text_length") or 0),
                    int(row.get("token_count") or 0),
                    int(row.get("word_count") or 0),
                    row.get("issue_codes_json"),
                    row.get("evidence_json"),
                    row.get("notes"),
                    row.get("current_confirmed_text_hash"),
                    row.get("queue_confirmed_text_hash"),
                    now,
                ),
            )
            row["shadow_item_id"] = int(item_cursor.lastrowid)

        upsert_registry_agents(conn, now, evaluated_rows)
        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            run_id=run_id,
            decision_run=decision_run,
            rows=evaluated_rows,
            analytics=analytics,
        )
        conn.commit()

    print("[issue_review_trigger_gender_role_surface] Shadow generated")
    print(f"[issue_review_trigger_gender_role_surface] Run id: {run_id}")
    print(f"[issue_review_trigger_gender_role_surface] Decision run id: {selected_decision_run_id}")
    print(f"[issue_review_trigger_gender_role_surface] Queue run id: {selected_queue_run_id}")
    print(f"[issue_review_trigger_gender_role_surface] Candidates: {len(evaluated_rows):,}")
    print(f"[issue_review_trigger_gender_role_surface] Ready: {ready_count:,}")
    print(f"[issue_review_trigger_gender_role_surface] Blocked: {blocked_count:,}")
    print(f"[issue_review_trigger_gender_role_surface] Report: {txt_path}")
    print(f"[issue_review_trigger_gender_role_surface] CSV: {csv_path}")
    print(f"[issue_review_trigger_gender_role_surface] JSONL: {jsonl_path}")
    return {
        "run_id": run_id,
        "decision_run_id": selected_decision_run_id,
        "queue_run_id": selected_queue_run_id,
        "candidates": len(evaluated_rows),
        "ready": ready_count,
        "blocked": blocked_count,
        "analytics": analytics,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a shadow router for trigger gender-role surface issue-review evidence.")
    parser.add_argument("--decision-run-id", type=int, default=None)
    parser.add_argument("--queue-run-id", type=int, default=None)
    parser.add_argument("--source-agent-key", default=SOURCE_AGENT_KEY)
    parser.add_argument("--policy-status", choices=["shadow"], default="shadow")
    args = parser.parse_args()
    main(
        decision_run_id=args.decision_run_id,
        queue_run_id=args.queue_run_id,
        source_agent_key=args.source_agent_key,
        policy_status=args.policy_status,
    )
