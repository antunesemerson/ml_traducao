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
from ml_composite_subpolicy_diagnostic import classify_token_subtype
from ml_composite_subpolicy_guarded_overlay import (
    DEFAULT_STATUSES,
    can_release,
    fetch_audit_run,
    fetch_ready_rules,
    latest_audit_run_id,
    text_hygiene_flags,
)
from ml_composite_subpolicy_promotion_audit import rule_key_for
from segment_token_overlay_review_queue import parse_csv_filter, suggested_route
from segment_token_policy_decision_rebase import latest_policy_run_id, latest_state_run_id
from segment_token_policy_review_queue import parse_json_list


RULE_VERSION = "segment_token_composite_decision_bridge_v1"
REVIEWER = "codex_composite_bridge"
FIX_DECISIONS = {"fix_confirmed_text", "encoding_cleanup_required", "manual_token_rewrite_required"}


def utc_now() -> str:
    return db.utc_now()


def sha256_text(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def fetch_policy_run(conn, policy_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM segment_token_policy_runs
        WHERE id = ?
          AND finished_at IS NOT NULL
        """,
        (policy_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Segment token policy run {policy_run_id} is missing or unfinished.")
    return dict(row)


def fetch_current_rows(
    conn,
    *,
    state_run_id: int,
    policy_run_id: int,
    buckets: set[str],
    pending_apply_only: bool,
) -> list[dict[str, Any]]:
    join_params: list[Any] = []
    where_params: list[Any] = [policy_run_id]
    where = ["i.run_id = ?"]
    joins = [
        "JOIN source_segments s ON s.id = i.segment_id",
        "LEFT JOIN output_segments o ON o.segment_id = i.segment_id",
        "LEFT JOIN segment_confirmations sc ON sc.segment_id = i.segment_id",
        """
        LEFT JOIN segment_token_policy_decisions d
          ON d.policy_run_id = i.run_id
         AND d.policy_item_id = i.id
        """,
    ]
    if pending_apply_only:
        joins.append(
            """
            JOIN segment_state_items state_i
              ON state_i.segment_id = i.segment_id
             AND state_i.run_id = ?
             AND state_i.apply_state = 'needs_apply'
            """
        )
        join_params.append(state_run_id)
    if buckets:
        where.append("i.policy_bucket IN ({})".format(", ".join("?" for _ in buckets)))
        where_params.extend(sorted(buckets))

    rows = conn.execute(
        f"""
        SELECT
            i.id AS policy_item_id,
            i.run_id AS policy_run_id,
            i.state_run_id,
            i.segment_id,
            i.relative_path,
            i.source_key,
            i.source_line_number,
            i.review_state,
            i.diff_kind,
            i.policy_bucket AS base_policy_bucket,
            i.risk_level AS base_risk_level,
            i.policy_bucket AS overlay_policy_bucket,
            i.risk_level AS overlay_risk_level,
            'base_segment_token_policy' AS overlay_action,
            'base_segment_token_policy' AS overlay_agent_key,
            '' AS decision,
            '' AS rule_key,
            i.recommendation AS base_recommendation,
            i.missing_tokens_json,
            i.extra_tokens_json,
            i.issue_flags_json,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS output_text,
            sc.confirmed_text,
            sc.confirmation_level,
            sc.confirmation_source,
            sc.confirmation_label,
            sc.locked,
            d.id AS current_decision_id,
            d.decision AS current_decision,
            d.approved_for_apply AS current_approved_for_apply,
            d.notes AS current_decision_notes
        FROM segment_token_policy_items i
        {" ".join(joins)}
        WHERE {" AND ".join(where)}
        ORDER BY
            CASE i.risk_level
                WHEN 'critical' THEN 0
                WHEN 'high' THEN 1
                WHEN 'medium' THEN 2
                WHEN 'low' THEN 3
                ELSE 9
            END,
            i.policy_bucket,
            i.relative_path,
            i.source_line_number,
            i.segment_id
        """,
        tuple(join_params + where_params),
    ).fetchall()
    return [dict(row) for row in rows]


def enrich_current_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload["missing_tokens"] = parse_json_list(row.get("missing_tokens_json"))
    payload["extra_tokens"] = parse_json_list(row.get("extra_tokens_json"))
    payload["issue_flags"] = parse_json_list(row.get("issue_flags_json"))
    route, route_reason = suggested_route(payload)
    payload["suggested_route"] = route
    payload["suggested_route_reason"] = route_reason
    return payload


def ready_rule_candidates(
    *,
    ready_by_key: dict[tuple[str, str, str], dict[str, Any]],
    route: str,
    subtype: str,
    rule_key: str,
) -> list[dict[str, Any]]:
    keys = [(route, subtype, rule_key)]
    if route == "gender_token_subspecialist_review" and subtype == "pronoun_added_for_pt_fluency":
        keys.append(("gender_pronoun_english_aligned_subpolicy", subtype, rule_key))
    return [ready_by_key[key] for key in keys if key in ready_by_key]


def classify_bridge_row(row: dict[str, Any], ready_by_key: dict[tuple[str, str, str], dict[str, Any]]) -> dict[str, Any]:
    subtype, families = classify_token_subtype(row)
    row["token_subtype"] = subtype
    row["token_families"] = sorted(families)
    rule_key, signature = rule_key_for(row)
    row["candidate_rule_key"] = rule_key
    row["candidate_signature"] = signature

    current_decision = str(row.get("current_decision") or "")
    existing_decision = row.get("current_decision_id") is not None and current_decision not in FIX_DECISIONS
    hygiene = text_hygiene_flags(row.get("confirmed_text"))
    candidate_rules = ready_rule_candidates(
        ready_by_key=ready_by_key,
        route=row["suggested_route"],
        subtype=subtype,
        rule_key=rule_key,
    )

    release = False
    release_reasons: list[str] = []
    release_profile: dict[str, str] | None = None
    ready_rule: dict[str, Any] | None = None
    guard_failures: list[str] = []
    for candidate_rule in candidate_rules:
        candidate_release, candidate_reasons, candidate_profile = can_release(row, candidate_rule)
        if candidate_release:
            release = True
            release_reasons = candidate_reasons
            release_profile = candidate_profile
            ready_rule = candidate_rule
            break
        guard_failures.extend(candidate_reasons)

    if existing_decision:
        bridge_status = "skip_existing_current_decision"
    elif hygiene:
        bridge_status = "blocked_text_hygiene"
    elif release and release_profile and release_profile.get("target_risk") == "low":
        bridge_status = "ready_to_bridge_apply_decision"
    elif release:
        bridge_status = "release_not_apply_bridge_target_not_low"
    elif candidate_rules:
        bridge_status = "guard_failed_for_ready_rule"
    else:
        bridge_status = "no_ready_composite_rule"

    return {
        **row,
        "ready_rule_route": ready_rule["suggested_route"] if ready_rule else "",
        "ready_rule_subtype": ready_rule["token_subtype"] if ready_rule else "",
        "ready_rule_key": ready_rule["rule_key"] if ready_rule else "",
        "ready_rule_status": ready_rule["promotion_status"] if ready_rule else "",
        "release_profile_agent": release_profile["agent_key"] if release_profile else "",
        "release_profile_bucket": release_profile["target_bucket"] if release_profile else "",
        "release_profile_risk": release_profile["target_risk"] if release_profile else "",
        "release_profile_action": release_profile["target_action"] if release_profile else "",
        "bridge_status": bridge_status,
        "bridge_reasons": release_reasons or guard_failures or ["no_matching_ready_rule"],
        "text_hygiene_flags": hygiene,
        "apply_allowed": 0,
    }


def insert_decision_run(
    conn,
    *,
    policy_run_id: int,
    report_path: Path,
    started_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO segment_token_policy_decision_runs (
            rule_version,
            policy_run_id,
            source_report,
            decisions_path,
            started_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (RULE_VERSION, policy_run_id, "composite_decision_bridge", str(report_path), started_at, started_at),
    )
    return int(cur.lastrowid)


def upsert_bridge_decision(
    conn,
    *,
    run_id: int,
    row: dict[str, Any],
    reviewer: str,
    now: str,
) -> None:
    reasons_json = json.dumps(
        {
            "rule_version": RULE_VERSION,
            "bridge_status": row["bridge_status"],
            "bridge_reasons": row["bridge_reasons"],
            "ready_rule": {
                "route": row["ready_rule_route"],
                "subtype": row["ready_rule_subtype"],
                "rule_key": row["ready_rule_key"],
                "status": row["ready_rule_status"],
            },
            "release_profile": {
                "agent": row["release_profile_agent"],
                "bucket": row["release_profile_bucket"],
                "risk": row["release_profile_risk"],
                "action": row["release_profile_action"],
            },
            "missing_tokens": row.get("missing_tokens") or [],
            "extra_tokens": row.get("extra_tokens") or [],
            "issue_flags": row.get("issue_flags") or [],
        },
        ensure_ascii=False,
    )
    notes = (
        f"Composite bridge approved current policy item via {row['ready_rule_route']} / "
        f"{row['ready_rule_subtype']} / {row['ready_rule_key']}; "
        f"agent={row['release_profile_agent']}; dry-run output only."
    )
    conn.execute(
        """
        INSERT INTO segment_token_policy_decisions (
            run_id,
            policy_run_id,
            policy_item_id,
            segment_id,
            relative_path,
            source_key,
            policy_bucket,
            risk_level,
            decision,
            approved_for_apply,
            corrected_text,
            notes,
            reviewer,
            confirmed_text_hash,
            output_text_hash,
            reasons_json,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'accept_policy_candidate', 1, '', ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(policy_run_id, policy_item_id) DO UPDATE SET
            run_id = excluded.run_id,
            decision = excluded.decision,
            approved_for_apply = excluded.approved_for_apply,
            corrected_text = excluded.corrected_text,
            notes = excluded.notes,
            reviewer = excluded.reviewer,
            confirmed_text_hash = excluded.confirmed_text_hash,
            output_text_hash = excluded.output_text_hash,
            reasons_json = excluded.reasons_json,
            updated_at = excluded.updated_at
        """,
        (
            run_id,
            row["policy_run_id"],
            row["policy_item_id"],
            row["segment_id"],
            row["relative_path"],
            row["source_key"],
            row["base_policy_bucket"],
            row["base_risk_level"],
            notes,
            reviewer,
            sha256_text(row.get("confirmed_text")),
            sha256_text(row.get("output_text") or ""),
            reasons_json,
            now,
            now,
        ),
    )


def update_decision_run(
    conn,
    *,
    run_id: int,
    total: int,
    applied: int,
    skipped: int,
    report_path: Path,
    finished_at: str,
) -> None:
    conn.execute(
        """
        UPDATE segment_token_policy_decision_runs
        SET
            total_decisions = ?,
            approved_count = ?,
            rejected_count = 0,
            fix_count = 0,
            skipped_count = ?,
            report_path = ?,
            finished_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (total, applied, skipped, str(report_path), finished_at, finished_at, run_id),
    )


def write_reports(
    settings: dict[str, Any],
    *,
    started_at: datetime,
    state_run_id: int,
    policy_run_id: int,
    audit_run_id: int,
    rows: list[dict[str, Any]],
    apply: bool,
    applied: int,
    decision_run_id: int | None,
) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{timestamp}_segment_token_composite_decision_bridge"
    txt_path = base.with_suffix(".txt")
    csv_path = base.with_suffix(".csv")
    jsonl_path = base.with_suffix(".jsonl")

    counts = Counter(row["bridge_status"] for row in rows)
    by_rule = Counter(
        (row["bridge_status"], row.get("ready_rule_key") or row.get("candidate_rule_key") or "<none>")
        for row in rows
    )
    by_bucket = Counter(
        (row["bridge_status"], row.get("base_policy_bucket") or "<none>", row.get("base_risk_level") or "<none>")
        for row in rows
    )

    fieldnames = [
        "policy_item_id",
        "segment_id",
        "relative_path",
        "source_line_number",
        "source_key",
        "base_policy_bucket",
        "base_risk_level",
        "suggested_route",
        "token_subtype",
        "candidate_rule_key",
        "ready_rule_route",
        "ready_rule_key",
        "release_profile_agent",
        "bridge_status",
        "missing_tokens",
        "extra_tokens",
        "text_hygiene_flags",
        "confirmed_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(row[key], ensure_ascii=False)
                    if key in {"missing_tokens", "extra_tokens", "text_hygiene_flags"}
                    else row.get(key)
                    for key in fieldnames
                }
            )

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    lines = [
        "Segment token composite decision bridge",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"State run id: {state_run_id}",
        f"Policy run id: {policy_run_id}",
        f"Promotion audit run id: {audit_run_id}",
        f"Apply: {apply}",
        f"Decision run id: {decision_run_id or 'not created'}",
        "",
        "Summary:",
        f"- Rows inspected: {len(rows)}",
        f"- Ready to bridge apply decision: {counts['ready_to_bridge_apply_decision']}",
        f"- Applied bridge decisions: {applied}",
        f"- Existing current decisions skipped: {counts['skip_existing_current_decision']}",
        f"- Text hygiene blocked: {counts['blocked_text_hygiene']}",
        f"- Guard failed for ready rule: {counts['guard_failed_for_ready_rule']}",
        f"- No ready composite rule: {counts['no_ready_composite_rule']}",
        f"- Release target not low: {counts['release_not_apply_bridge_target_not_low']}",
        "",
        "Status counts:",
        *[f"- {key}: {value}" for key, value in counts.most_common()],
        "",
        "Top status/rules:",
    ]
    for (status, rule), value in by_rule.most_common(30):
        lines.append(f"- {status} | {rule}: {value}")
    lines.extend(["", "Top status/buckets:"])
    for (status, bucket, risk), value in by_bucket.most_common(30):
        lines.append(f"- {status} | {bucket} | {risk}: {value}")
    lines.extend(["", "Ready sample:"])
    for row in [item for item in rows if item["bridge_status"] == "ready_to_bridge_apply_decision"][:80]:
        lines.extend(
            [
                (
                    f"- item {row['policy_item_id']} | segment {row['segment_id']} | "
                    f"{row['relative_path']}:{row['source_line_number']} | {row['source_key']} | "
                    f"{row['ready_rule_key']}"
                ),
                f"  AGENT: {row['release_profile_agent']}",
                f"  EXTRA: {json.dumps(row.get('extra_tokens') or [], ensure_ascii=False)}",
                f"  MISSING: {json.dumps(row.get('missing_tokens') or [], ensure_ascii=False)}",
                f"  CONFIRMED: {short(row.get('confirmed_text'), 220)}",
            ]
        )
    lines.extend(
        [
            "",
            "Interpretation:",
            "- This bridge does not touch output files.",
            "- Apply=true only writes token policy decisions for rows whose current item passes guarded composite rules.",
            "- Critical/high target releases are not converted to apply decisions here.",
            "- Existing non-fix current decisions are not overwritten.",
            "- Previous fix_confirmed_text decisions may be superseded after the confirmation text is clean.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path


def main(
    *,
    state_run_id: int | None = None,
    policy_run_id: int | None = None,
    audit_run_id: int | None = None,
    statuses_value: str | None = DEFAULT_STATUSES,
    buckets_value: str | None = None,
    pending_apply_only: bool = True,
    apply: bool = False,
    reviewer: str = REVIEWER,
) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    applied = 0
    decision_run_id: int | None = None
    statuses = parse_csv_filter(statuses_value)
    buckets = parse_csv_filter(buckets_value)

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_state_run_id = state_run_id or latest_state_run_id(conn)
        selected_policy_run_id = policy_run_id or latest_policy_run_id(conn, selected_state_run_id)
        selected_audit_run_id = audit_run_id or latest_audit_run_id(conn)
        _policy_run = fetch_policy_run(conn, selected_policy_run_id)
        audit_run = fetch_audit_run(conn, selected_audit_run_id)
        ready_rules = fetch_ready_rules(
            conn,
            audit_run_id=selected_audit_run_id,
            statuses=statuses,
            rule_keys=set(),
        )
        ready_by_key = {
            (row["suggested_route"], row["token_subtype"], row["rule_key"]): row
            for row in ready_rules
        }
        rows = [
            classify_bridge_row(enrich_current_row(row), ready_by_key)
            for row in fetch_current_rows(
                conn,
                state_run_id=selected_state_run_id,
                policy_run_id=selected_policy_run_id,
                buckets=buckets,
                pending_apply_only=pending_apply_only,
            )
        ]

        report_path, _csv_path, _jsonl_path = write_reports(
            settings,
            started_at=started_at,
            state_run_id=selected_state_run_id,
            policy_run_id=selected_policy_run_id,
            audit_run_id=selected_audit_run_id,
            rows=rows,
            apply=apply,
            applied=0,
            decision_run_id=None,
        )

        bridge_rows = [row for row in rows if row["bridge_status"] == "ready_to_bridge_apply_decision"]
        if apply and bridge_rows:
            now = utc_now()
            decision_run_id = insert_decision_run(
                conn,
                policy_run_id=selected_policy_run_id,
                report_path=report_path,
                started_at=now,
            )
            for row in bridge_rows:
                upsert_bridge_decision(conn, run_id=decision_run_id, row=row, reviewer=reviewer, now=now)
            applied = len(bridge_rows)
            update_decision_run(
                conn,
                run_id=decision_run_id,
                total=len(bridge_rows),
                applied=applied,
                skipped=max(0, len(rows) - applied),
                report_path=report_path,
                finished_at=utc_now(),
            )
            conn.commit()
            report_path, _csv_path, _jsonl_path = write_reports(
                settings,
                started_at=started_at,
                state_run_id=selected_state_run_id,
                policy_run_id=selected_policy_run_id,
                audit_run_id=selected_audit_run_id,
                rows=rows,
                apply=apply,
                applied=applied,
                decision_run_id=decision_run_id,
            )
            update_decision_run(
                conn,
                run_id=decision_run_id,
                total=len(bridge_rows),
                applied=applied,
                skipped=max(0, len(rows) - applied),
                report_path=report_path,
                finished_at=utc_now(),
            )
            conn.commit()

    counts = Counter(row["bridge_status"] for row in rows)
    print("[segment_token_composite_decision_bridge] Bridge audit complete")
    print(f"[segment_token_composite_decision_bridge] Rule version: {RULE_VERSION}")
    print(f"[segment_token_composite_decision_bridge] State run id: {selected_state_run_id}")
    print(f"[segment_token_composite_decision_bridge] Policy run id: {selected_policy_run_id}")
    print(f"[segment_token_composite_decision_bridge] Promotion audit run id: {selected_audit_run_id}")
    print(f"[segment_token_composite_decision_bridge] Rows inspected: {len(rows)}")
    print(f"[segment_token_composite_decision_bridge] Ready bridge decisions: {counts['ready_to_bridge_apply_decision']}")
    print(f"[segment_token_composite_decision_bridge] Applied bridge decisions: {applied}")
    for key, value in counts.most_common():
        print(f"[segment_token_composite_decision_bridge] {key}: {value}")
    print(f"[segment_token_composite_decision_bridge] Report: {report_path}")
    return {
        "report_path": str(report_path),
        "rows": len(rows),
        "ready": counts["ready_to_bridge_apply_decision"],
        "applied": applied,
        "decision_run_id": decision_run_id,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bridge mature composite subpolicy knowledge into current token decisions.")
    parser.add_argument("--state-run-id", type=int, default=None)
    parser.add_argument("--policy-run-id", type=int, default=None)
    parser.add_argument("--audit-run-id", type=int, default=None)
    parser.add_argument("--statuses", default=DEFAULT_STATUSES)
    parser.add_argument("--buckets", default=None)
    parser.add_argument("--include-all", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reviewer", default=REVIEWER)
    args = parser.parse_args()
    main(
        state_run_id=args.state_run_id,
        policy_run_id=args.policy_run_id,
        audit_run_id=args.audit_run_id,
        statuses_value=args.statuses,
        buckets_value=args.buckets,
        pending_apply_only=not args.include_all,
        apply=args.apply,
        reviewer=args.reviewer,
    )
