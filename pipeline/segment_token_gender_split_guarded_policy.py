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
from segment_token_gender_simple_evidence_queue import simple_agent_for
from segment_token_gender_split_subpolicy import enrich_split_row, non_empty_scopes, token_method, token_scope
from segment_token_gender_subpolicy import classify_row, fetch_rows
from segment_token_policy_review_queue import latest_policy_run_id, split_csv


RULE_VERSION = "segment_token_gender_split_guarded_policy_v1"
READY_STATUS = "ready_for_guarded_policy_review"
DEFAULT_AGENTS = (
    "same_scope_object_article_to_pronoun_context",
    "same_scope_single_gender_suffix_to_subject_pronoun",
    "simple_neutral_noun_gender_removed",
    "simple_single_object_pronoun_added",
)
GUARD_PROFILES = {
    "same_scope_object_article_to_pronoun_context": {
        "target_bucket": "guarded_gender_object_article_to_pronoun_context",
        "target_risk": "low",
        "missing_methods": {"Custom('ES_LoLa')"},
        "extra_methods": {"GetHerHim", "GetHerHis", "GetSheHe"},
        "required_extra_any": {"GetHerHim", "GetHerHis"},
        "missing_count": None,
        "scope_policy": "same_non_empty_scope_equal",
        "target_action": "would_lower_to_low_review",
    },
    "same_scope_single_gender_suffix_to_subject_pronoun": {
        "target_bucket": "guarded_gender_single_suffix_to_subject_pronoun",
        "target_risk": "low",
        "missing_methods": {"Custom('ES_OA')"},
        "extra_methods": {"GetSheHe"},
        "required_extra_any": {"GetSheHe"},
        "missing_count": 1,
        "scope_policy": "same_non_empty_scope_equal",
        "target_action": "would_lower_to_low_review",
    },
    "simple_neutral_noun_gender_removed": {
        "target_bucket": "guarded_gender_neutral_noun_token_removed",
        "target_risk": "low",
        "missing_methods": {"Custom('ES_OA')", "Custom('ES_XA')"},
        "extra_methods": set(),
        "required_extra_any": set(),
        "missing_count": None,
        "extra_count": 0,
        "allow_empty_extra": True,
        "scope_policy": "missing_single_scope_only",
        "target_action": "would_lower_to_low_review",
    },
    "simple_single_object_pronoun_added": {
        "target_bucket": "guarded_gender_single_object_pronoun_added",
        "target_risk": "low",
        "missing_methods": set(),
        "extra_methods": {"GetHerHim"},
        "required_extra_any": {"GetHerHim"},
        "missing_count": 0,
        "extra_count": 1,
        "allow_empty_missing": True,
        "scope_policy": "extra_single_scope_only",
        "target_action": "would_lower_to_low_review",
    },
}


def latest_audit_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM segment_token_gender_split_promotion_audit_runs
        WHERE finished_at IS NOT NULL
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished segment_token_gender_split_promotion_audit_runs entry found.")
    return int(row["id"])


def fetch_audit_run(conn, audit_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM segment_token_gender_split_promotion_audit_runs
        WHERE id = ?
        """,
        (audit_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Gender split promotion audit run {audit_run_id} was not found.")
    return dict(row)


def fetch_ready_agents(conn, *, audit_run_id: int, agents: set[str]) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM segment_token_gender_split_promotion_audit_items
        WHERE run_id = ?
          AND promotion_status = ?
        """,
        (audit_run_id, READY_STATUS),
    ).fetchall()
    ready: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = dict(row)
        if agents and payload["split_agent"] not in agents:
            continue
        ready[payload["split_agent"]] = payload
    return ready


def same_non_empty_scope(tokens: list[str]) -> str:
    scopes = {token_scope(token) for token in tokens if token_scope(token)}
    return next(iter(scopes)) if len(scopes) == 1 else ""


def method_signature(row: dict[str, Any]) -> str:
    missing = ",".join(row.get("missing_methods") or [])
    extra = ",".join(row.get("extra_methods") or [])
    return f"{missing} -> {extra}"


def enrich_simple_row(row: dict[str, Any]) -> dict[str, Any]:
    simple_agent, maturity, next_action, reasons = simple_agent_for(row)
    missing = row["missing_tokens"]
    extra = row["extra_tokens"]
    return {
        **row,
        "split_agent": simple_agent,
        "split_maturity": maturity,
        "split_next_action": next_action,
        "split_reasons": reasons,
        "missing_scopes": sorted(non_empty_scopes(missing)),
        "extra_scopes": sorted(non_empty_scopes(extra)),
        "missing_methods": [token_method(token) for token in missing],
        "extra_methods": [token_method(token) for token in extra],
        "apply_allowed": 0,
    }


def guard_row(row: dict[str, Any], ready_agents: dict[str, dict[str, Any]]) -> dict[str, Any]:
    failures: list[str] = []
    profile = GUARD_PROFILES.get(row["split_agent"])
    reasons: list[str] = [
        f"rule:{RULE_VERSION}",
        f"split_agent:{row['split_agent']}",
        "apply_allowed:0",
    ]
    ready = ready_agents.get(row["split_agent"])
    if not ready:
        failures.append("agent_not_ready_for_guarded_policy_review")
    if not profile:
        failures.append("no_guard_profile_for_agent")
    if row["split_maturity"] != "candidate":
        failures.append("split_maturity_not_candidate")

    missing_methods = set(row.get("missing_methods") or [])
    extra_methods = set(row.get("extra_methods") or [])
    if profile:
        allowed_missing = profile["missing_methods"]
        allowed_extra = profile["extra_methods"]
        required_extra = profile.get("required_extra_any") or set()
        missing_count = profile["missing_count"]
        extra_count = profile.get("extra_count")
        if missing_methods:
            if not missing_methods <= allowed_missing:
                failures.append("missing_methods_not_allowed_by_profile")
        elif not profile.get("allow_empty_missing", False):
            failures.append("missing_methods_not_allowed_by_profile")
        if extra_methods:
            if not extra_methods <= allowed_extra:
                failures.append("extra_methods_not_allowed_by_profile")
        elif not profile.get("allow_empty_extra", False):
            failures.append("extra_methods_not_allowed_by_profile")
        if required_extra and not (extra_methods & required_extra):
            failures.append("required_extra_pronoun_missing")
        if missing_count is not None and len(row.get("missing_tokens") or []) != missing_count:
            failures.append("missing_token_count_not_allowed_by_profile")
        if extra_count is not None and len(row.get("extra_tokens") or []) != extra_count:
            failures.append("extra_token_count_not_allowed_by_profile")

    missing_scope = same_non_empty_scope(row.get("missing_tokens") or [])
    extra_scope = same_non_empty_scope(row.get("extra_tokens") or [])
    scope_policy = profile.get("scope_policy", "same_non_empty_scope_equal") if profile else "same_non_empty_scope_equal"
    guarded_scope = missing_scope
    if scope_policy == "same_non_empty_scope_equal":
        if not missing_scope or missing_scope != extra_scope:
            failures.append("scope_not_single_and_equal")
        guarded_scope = missing_scope
    elif scope_policy == "extra_single_scope_only":
        if row.get("missing_tokens"):
            failures.append("missing_tokens_not_allowed_by_scope_policy")
        if not extra_scope:
            failures.append("extra_scope_not_single")
        guarded_scope = extra_scope
    elif scope_policy == "missing_single_scope_only":
        if row.get("extra_tokens"):
            failures.append("extra_tokens_not_allowed_by_scope_policy")
        if not missing_scope:
            failures.append("missing_scope_not_single")
        guarded_scope = missing_scope
    else:
        failures.append(f"unknown_scope_policy:{scope_policy}")

    if row.get("text_hygiene_flags"):
        failures.append("confirmed_text_hygiene_flags")
    if row.get("suspicious_text"):
        failures.append("confirmed_text_suspicious")

    if failures:
        guarded_status = "blocked_by_guard"
        guarded_action = "keep_manual_review"
        target_bucket = row["policy_bucket"]
        target_risk = row["risk_level"]
        reasons.extend(f"guard_failed:{failure}" for failure in failures)
    else:
        guarded_status = "ready_for_guarded_dry_run_release"
        guarded_action = profile["target_action"]
        target_bucket = profile["target_bucket"]
        target_risk = profile["target_risk"]
        reasons.extend(
            [
                "ready_agent_evidence_positive_only",
                f"positive_count:{ready['positive_count']}",
                f"distinct_method_count:{ready['distinct_method_count']}",
                f"scope:{guarded_scope}",
                f"target_bucket:{target_bucket}",
                f"target_risk:{target_risk}",
            ]
        )

    return {
        **row,
        "method_signature": method_signature(row),
        "guarded_status": guarded_status,
        "guarded_action": guarded_action,
        "target_policy_bucket": target_bucket,
        "target_risk_level": target_risk,
        "guard_reasons": reasons,
        "apply_allowed": 0,
    }


def insert_run(
    conn,
    *,
    policy_run_id: int,
    audit_run_id: int,
    min_positive: int,
    started_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO segment_token_gender_split_guarded_policy_runs (
            rule_version,
            policy_run_id,
            audit_run_id,
            min_positive,
            started_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (RULE_VERSION, policy_run_id, audit_run_id, min_positive, started_at, started_at),
    )
    return int(cur.lastrowid)


def insert_items(conn, *, run_id: int, rows: list[dict[str, Any]], now: str) -> None:
    for row in rows:
        conn.execute(
            """
            INSERT INTO segment_token_gender_split_guarded_policy_items (
                run_id,
                policy_run_id,
                policy_item_id,
                segment_id,
                relative_path,
                source_key,
                source_line_number,
                split_agent,
                split_maturity,
                method_signature,
                guarded_status,
                guarded_action,
                target_policy_bucket,
                target_risk_level,
                apply_allowed,
                reasons_json,
                missing_tokens_json,
                extra_tokens_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                row["policy_run_id"],
                row["policy_item_id"],
                row["segment_id"],
                row["relative_path"],
                row["source_key"],
                row.get("source_line_number"),
                row["split_agent"],
                row["split_maturity"],
                row["method_signature"],
                row["guarded_status"],
                row["guarded_action"],
                row["target_policy_bucket"],
                row["target_risk_level"],
                row["apply_allowed"],
                json.dumps(row["guard_reasons"], ensure_ascii=False),
                json.dumps(row.get("missing_tokens") or [], ensure_ascii=False),
                json.dumps(row.get("extra_tokens") or [], ensure_ascii=False),
                now,
            ),
        )


def write_outputs(
    settings: dict[str, Any],
    *,
    run_id: int,
    policy_run_id: int,
    audit_run: dict[str, Any],
    rows: list[dict[str, Any]],
    ready_agents: dict[str, dict[str, Any]],
    started_at: datetime,
) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{timestamp}_segment_token_gender_split_guarded_policy"
    txt_path = base.with_suffix(".txt")
    csv_path = base.with_suffix(".csv")
    jsonl_path = base.with_suffix(".jsonl")

    fieldnames = [
        "policy_item_id",
        "policy_run_id",
        "segment_id",
        "relative_path",
        "source_line_number",
        "source_key",
        "split_agent",
        "split_maturity",
        "method_signature",
        "guarded_status",
        "guarded_action",
        "target_policy_bucket",
        "target_risk_level",
        "apply_allowed",
        "guard_reasons",
        "confirmed_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(row.get(key), ensure_ascii=False)
                    if key == "guard_reasons"
                    else row.get(key)
                    for key in fieldnames
                }
            )

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    status_counts = Counter(row["guarded_status"] for row in rows)
    action_counts = Counter(row["guarded_action"] for row in rows)
    lines = [
        "Segment token gender split guarded policy dry-run",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Guarded run id: {run_id}",
        f"Policy run id: {policy_run_id}",
        f"Promotion audit run id: {audit_run['id']}",
        f"Promotion audit min positives: {audit_run['min_positive']}",
        "",
        "Safety contract:",
        "- This is a guarded dry-run, not production promotion.",
        "- Apply allowed: 0",
        "- Writes output/source: no",
        "- Target risk is used only for future orchestration review.",
        "",
        "Ready agents:",
        *[
            (
                f"- {agent}: positives={row['positive_count']} negatives={row['negative_count']} "
                f"manual={row['manual_exception_count']} methods={row['distinct_method_count']}"
            )
            for agent, row in sorted(ready_agents.items())
        ],
        "",
        "Summary:",
        f"- Rows inspected: {len(rows)}",
        f"- Guarded ready: {status_counts['ready_for_guarded_dry_run_release']}",
        f"- Blocked by guard: {status_counts['blocked_by_guard']}",
        "",
        "Actions:",
        *[f"- {key}: {value}" for key, value in action_counts.most_common()],
        "",
        "Samples:",
    ]
    for row in rows[:80]:
        lines.extend(
            [
                (
                    f"- item {row['policy_item_id']} | {row['split_agent']} | {row['guarded_status']} | "
                    f"{row['relative_path']}:{row['source_line_number']} | {row['source_key']}"
                ),
                f"  methods: {row['method_signature']}",
                f"  action: {row['guarded_action']} -> {row['target_policy_bucket']} / {row['target_risk_level']}",
                f"  reasons: {', '.join(row['guard_reasons'])}",
                f"  text: {short(row.get('confirmed_text'), 240)}",
            ]
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path


def update_run(
    conn,
    *,
    run_id: int,
    rows: list[dict[str, Any]],
    ready_agents: dict[str, dict[str, Any]],
    report_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    finished_at: str,
) -> None:
    status_counts = Counter(row["guarded_status"] for row in rows)
    conn.execute(
        """
        UPDATE segment_token_gender_split_guarded_policy_runs
        SET
            total_candidates = ?,
            guarded_ready_count = ?,
            blocked_count = ?,
            enabled_agent_count = ?,
            apply_allowed_count = 0,
            report_path = ?,
            csv_path = ?,
            jsonl_path = ?,
            finished_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            len(rows),
            status_counts["ready_for_guarded_dry_run_release"],
            status_counts["blocked_by_guard"],
            len(ready_agents),
            str(report_path),
            str(csv_path),
            str(jsonl_path),
            finished_at,
            finished_at,
            run_id,
        ),
    )


def main(
    *,
    policy_run_id: int | None = None,
    audit_run_id: int | None = None,
    pending_apply_only: bool = True,
    skip_apply_approved: bool = True,
    agents_value: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    agent_filter = set(split_csv(agents_value)) if agents_value else set(DEFAULT_AGENTS)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_policy_run_id = policy_run_id or latest_policy_run_id(conn)
        selected_audit_run_id = audit_run_id or latest_audit_run_id(conn)
        audit_run = fetch_audit_run(conn, selected_audit_run_id)
        ready_agents = fetch_ready_agents(conn, audit_run_id=selected_audit_run_id, agents=agent_filter)
        raw_rows = fetch_rows(
            conn,
            policy_run_id=selected_policy_run_id,
            pending_apply_only=pending_apply_only,
            skip_apply_approved=skip_apply_approved,
            limit=None,
        )
        run_id = insert_run(
            conn,
            policy_run_id=selected_policy_run_id,
            audit_run_id=selected_audit_run_id,
            min_positive=int(audit_run["min_positive"] or 0),
            started_at=started_at.isoformat(timespec="seconds"),
        )

        classified = [classify_row(row) for row in raw_rows]
        split_rows = [enrich_split_row(row) for row in classified if row["subpolicy_status"] == "needs_split_review"]
        simple_rows = [
            enrich_simple_row(row)
            for row in classified
            if row["subpolicy_status"] in {"subpolicy_candidate_review", "neutralization_candidate_review"}
        ]
        split_rows.extend(simple_rows)
        if agent_filter:
            split_rows = [row for row in split_rows if row["split_agent"] in agent_filter]
        split_rows.sort(
            key=lambda row: (
                row["split_agent"],
                row["relative_path"],
                int(row.get("source_line_number") or 0),
                row["source_key"],
            )
        )
        if limit is not None:
            split_rows = split_rows[:limit]
        rows = [guard_row(row, ready_agents) for row in split_rows]
        now = db.utc_now()
        insert_items(conn, run_id=run_id, rows=rows, now=now)
        report_path, csv_path, jsonl_path = write_outputs(
            settings,
            run_id=run_id,
            policy_run_id=selected_policy_run_id,
            audit_run=audit_run,
            rows=rows,
            ready_agents=ready_agents,
            started_at=started_at,
        )
        finished_at = datetime.now().isoformat(timespec="seconds")
        update_run(
            conn,
            run_id=run_id,
            rows=rows,
            ready_agents=ready_agents,
            report_path=report_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            finished_at=finished_at,
        )
        conn.commit()

    status_counts = Counter(row["guarded_status"] for row in rows)
    print("[segment_token_gender_split_guarded_policy] Guarded policy dry-run generated")
    print(f"[segment_token_gender_split_guarded_policy] Rule version: {RULE_VERSION}")
    print(f"[segment_token_gender_split_guarded_policy] Policy run id: {selected_policy_run_id}")
    print(f"[segment_token_gender_split_guarded_policy] Audit run id: {selected_audit_run_id}")
    print(f"[segment_token_gender_split_guarded_policy] Guarded run id: {run_id}")
    print(f"[segment_token_gender_split_guarded_policy] Rows inspected: {len(rows)}")
    for key, value in status_counts.most_common():
        print(f"[segment_token_gender_split_guarded_policy] {key}: {value}")
    print("[segment_token_gender_split_guarded_policy] Apply allowed: 0")
    print(f"[segment_token_gender_split_guarded_policy] Report: {report_path}")
    print(f"[segment_token_gender_split_guarded_policy] CSV: {csv_path}")
    print(f"[segment_token_gender_split_guarded_policy] JSONL: {jsonl_path}")
    return {
        "run_id": run_id,
        "report_path": str(report_path),
        "rows": len(rows),
        "status_counts": dict(status_counts),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dry-run guarded policy for ready gender split agents.")
    parser.add_argument("--policy-run-id", type=int, default=None)
    parser.add_argument("--audit-run-id", type=int, default=None)
    parser.add_argument("--include-all", action="store_true")
    parser.add_argument("--include-apply-approved", action="store_true")
    parser.add_argument("--agents", default=",".join(DEFAULT_AGENTS))
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    main(
        policy_run_id=args.policy_run_id,
        audit_run_id=args.audit_run_id,
        pending_apply_only=not args.include_all,
        skip_apply_approved=not args.include_apply_approved,
        agents_value=args.agents,
        limit=args.limit,
    )
