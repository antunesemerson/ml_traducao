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
from ml_composite_subpolicy_diagnostic import classify_token_subtype, fetch_decisions
from ml_composite_subpolicy_promotion_audit import rule_key_for
from segment_token_overlay_review_queue import (
    DEFAULT_ACTIVE_GATE_KEY,
    active_gate_overlay_run_id,
    enrich,
    fetch_rows,
    parse_csv_filter,
    record_active_gate_queue_run,
    slugify,
)


RULE_VERSION = "ml_composite_subpolicy_promotion_queue_v1"
SOURCE_MODE = "active_composite_subpolicy_promotion_queue"
DEFAULT_STATUSES = "needs_more_positive_evidence,split_required_conflicting_evidence"


def latest_audit_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_composite_subpolicy_promotion_audit_runs
        WHERE finished_at IS NOT NULL
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No ml_composite_subpolicy_promotion_audit_runs entry found.")
    return int(row["id"])


def fetch_audit_run(conn, run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_composite_subpolicy_promotion_audit_runs
        WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Promotion audit run {run_id} was not found.")
    return dict(row)


def fetch_audit_rules(
    conn,
    *,
    run_id: int,
    statuses: set[str],
    routes: set[str],
    subtypes: set[str],
    rule_keys: set[str],
    max_rules: int | None,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_composite_subpolicy_promotion_audit_items
        WHERE run_id = ?
        ORDER BY
          CASE promotion_status
            WHEN 'split_required_conflicting_evidence' THEN 0
            WHEN 'needs_more_positive_evidence' THEN 1
            WHEN 'pending_review_only' THEN 2
            WHEN 'manual_exception_boundary' THEN 3
            ELSE 9
          END,
          pending_items DESC,
          total_items DESC,
          suggested_route,
          token_subtype,
          rule_key
        """,
        (run_id,),
    ).fetchall()
    selected: list[dict[str, Any]] = []
    for raw_row in rows:
        row = dict(raw_row)
        if statuses and row["promotion_status"] not in statuses:
            continue
        if routes and row["suggested_route"] not in routes:
            continue
        if subtypes and row["token_subtype"] not in subtypes:
            continue
        if rule_keys and row["rule_key"] not in rule_keys:
            continue
        selected.append(row)
        if max_rules is not None and len(selected) >= max_rules:
            break
    return selected


def fetch_queued_ids(conn, *, gate_key: str, overlay_run_id: int) -> set[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT qi.policy_item_id
        FROM ml_composite_gate_queue_items qi
        JOIN ml_composite_gate_queue_runs qr ON qr.id = qi.queue_run_id
        WHERE qi.gate_key = ?
          AND qi.overlay_run_id = ?
          AND (
              qr.route_filter_csv IS NOT NULL
              OR qr.risk_filter_csv IS NOT NULL
              OR qr.limit_count IS NOT NULL
          )
        """,
        (gate_key, overlay_run_id),
    ).fetchall()
    return {int(row["policy_item_id"]) for row in rows}


def select_rows(
    *,
    active_rows: list[dict[str, Any]],
    audit_rules: list[dict[str, Any]],
    reviewed_ids: set[int],
    queued_ids: set[int],
    include_reviewed: bool,
    include_queued: bool,
    limit_per_rule: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    active_by_rule: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in active_rows:
        subtype, families = classify_token_subtype(row)
        row["token_subtype"] = subtype
        row["token_families"] = sorted(families)
        rule_key, signature = rule_key_for(row)
        row["promotion_rule_key"] = rule_key
        row["promotion_rule_signature"] = signature
        key = (row["suggested_route"], subtype, rule_key)
        active_by_rule.setdefault(key, []).append(row)

    selected: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for rule in audit_rules:
        key = (rule["suggested_route"], rule["token_subtype"], rule["rule_key"])
        candidates = []
        skipped_reviewed = 0
        skipped_queued = 0
        for row in active_by_rule.get(key, []):
            policy_item_id = int(row["policy_item_id"])
            if not include_reviewed and policy_item_id in reviewed_ids:
                skipped_reviewed += 1
                continue
            if not include_queued and policy_item_id in queued_ids:
                skipped_queued += 1
                continue
            candidate = dict(row)
            candidate["promotion_audit_run_id"] = rule["run_id"]
            candidate["promotion_status"] = rule["promotion_status"]
            candidate["promotion_confidence_band"] = rule["confidence_band"]
            candidate["promotion_recommended_action"] = rule["recommended_action"]
            candidates.append(candidate)

        candidates.sort(key=lambda row: (row["relative_path"], int(row.get("source_line_number") or 0), row["source_key"]))
        picked = candidates[:limit_per_rule]
        selected.extend(picked)
        summaries.append(
            {
                "suggested_route": rule["suggested_route"],
                "token_subtype": rule["token_subtype"],
                "rule_key": rule["rule_key"],
                "promotion_status": rule["promotion_status"],
                "total_rule_items": rule["total_items"],
                "reviewed_rule_items": rule["reviewed_items"],
                "pending_rule_items": rule["pending_items"],
                "accept_count": rule["accept_count"],
                "needs_subpolicy_count": rule["needs_subpolicy_count"],
                "skipped_reviewed": skipped_reviewed,
                "skipped_queued": skipped_queued,
                "available_candidates": len(candidates),
                "selected_rows": len(picked),
            }
        )
    return selected, summaries


def write_outputs(
    settings: dict[str, Any],
    *,
    audit_run: dict[str, Any],
    active_gate: dict[str, Any],
    rows: list[dict[str, Any]],
    rule_summaries: list[dict[str, Any]],
    limit_per_rule: int,
    include_reviewed: bool,
    include_queued: bool,
    plan_only: bool,
    started_at: datetime,
) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = started_at.strftime("%Y%m%d_%H%M%S_%f")
    slug = "subpolicy_promotion_targeted"
    if len(rule_summaries) == 1:
        rule = rule_summaries[0]
        slug += "_" + slugify(f"{rule['suggested_route']}_{rule['token_subtype']}_{rule['rule_key']}")
    base = reports_dir / f"{timestamp}_ml_composite_{slug}_queue"
    txt_path = base.with_suffix(".txt")
    csv_path = base.with_suffix(".csv")
    jsonl_path = base.with_suffix(".jsonl")
    decisions_template_path = base.with_name(base.name + "_decisions_template").with_suffix(".jsonl")

    fieldnames = [
        "policy_item_id",
        "segment_id",
        "suggested_route",
        "token_subtype",
        "promotion_rule_key",
        "promotion_status",
        "promotion_recommended_action",
        "overlay_policy_bucket",
        "overlay_risk_level",
        "relative_path",
        "source_line_number",
        "source_key",
        "missing_tokens",
        "extra_tokens",
        "token_families",
        "confirmed_text",
        "output_text",
        "spanish_text",
        "english_text",
        "old_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(row[key], ensure_ascii=False)
                    if key in {"missing_tokens", "extra_tokens", "token_families"}
                    else row.get(key)
                    for key in fieldnames
                }
            )

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    with decisions_template_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    {
                        "policy_item_id": row["policy_item_id"],
                        "decision": "needs_subpolicy",
                        "corrected_text": "",
                        "reviewer": "",
                        "notes": (
                            f"{RULE_VERSION}; route={row['suggested_route']}; subtype={row['token_subtype']}; "
                            f"rule_key={row['promotion_rule_key']}; promotion_status={row['promotion_status']}; "
                            "use accept_policy_candidate only when this exact narrow rule is reusable; "
                            "use reject_policy_candidate or needs_subpolicy for unsafe boundaries."
                        ),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    route_counts = Counter(row["suggested_route"] for row in rows)
    rule_counts = Counter(row["promotion_rule_key"] for row in rows)
    lines = [
        "ML composite subpolicy promotion targeted queue",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Plan only: {plan_only}",
        f"Gate key: {active_gate['gate_key']}",
        f"Checkpoint id: {active_gate['active_checkpoint_id']}",
        f"Overlay run id: {active_gate['active_overlay_run_id']}",
        f"Policy run id: {active_gate['active_policy_run_id']}",
        f"Promotion audit run id: {audit_run['id']}",
        "",
        "Settings:",
        f"- Limit per rule: {limit_per_rule}",
        f"- Include reviewed: {include_reviewed}",
        f"- Include queued: {include_queued}",
        "",
        "Rows:",
        f"- Selected rows: {len(rows)}",
        "",
        "Routes:",
        *[f"- {key}: {value}" for key, value in route_counts.most_common()],
        "",
        "Rules:",
        *[f"- {key}: {value}" for key, value in rule_counts.most_common()],
        "",
        "Rule summaries:",
    ]
    for rule in rule_summaries:
        lines.extend(
            [
                f"- {rule['suggested_route']} / {rule['token_subtype']} / {rule['rule_key']}",
                f"  status: {rule['promotion_status']}",
                f"  rule total/reviewed/pending: {rule['total_rule_items']}/{rule['reviewed_rule_items']}/{rule['pending_rule_items']}",
                f"  accept/needs: {rule['accept_count']}/{rule['needs_subpolicy_count']}",
                f"  skipped reviewed/queued: {rule['skipped_reviewed']}/{rule['skipped_queued']}",
                f"  available candidates: {rule['available_candidates']}",
                f"  selected rows: {rule['selected_rows']}",
            ]
        )
    lines.extend(["", "Review sample:"])
    for row in rows[:80]:
        lines.extend(
            [
                (
                    f"- item {row['policy_item_id']} | {row['promotion_rule_key']} | "
                    f"{row['relative_path']}:{row['source_line_number']} | {row['source_key']}"
                ),
                f"  missing: {json.dumps(row['missing_tokens'], ensure_ascii=False)}",
                f"  extra: {json.dumps(row['extra_tokens'], ensure_ascii=False)}",
                f"  confirmed: {short(row.get('confirmed_text'), 260)}",
                f"  english: {short(row.get('english_text'), 220)}",
            ]
        )
    lines.extend(
        [
            "",
            "Interpretation:",
            "- This queue targets exact rule families from the promotion audit.",
            "- The template defaults to needs_subpolicy; positive labels should be rare and rule-specific.",
            "- This command does not ingest decisions, promote policies, train models, or update output files.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path, decisions_template_path


def main(
    *,
    audit_run_id: int | None = None,
    statuses_value: str | None = DEFAULT_STATUSES,
    routes_value: str | None = None,
    subtypes_value: str | None = None,
    rule_keys_value: str | None = None,
    max_rules: int | None = 5,
    limit_per_rule: int = 12,
    include_reviewed: bool = False,
    include_queued: bool = True,
    plan_only: bool = False,
    gate_key: str = DEFAULT_ACTIVE_GATE_KEY,
) -> dict[str, Any]:
    if limit_per_rule <= 0:
        raise RuntimeError("limit_per_rule must be positive.")
    if max_rules is not None and max_rules <= 0:
        raise RuntimeError("max_rules must be positive when provided.")

    settings = db.load_settings()
    started_at = datetime.now()
    statuses = parse_csv_filter(statuses_value)
    routes = parse_csv_filter(routes_value)
    subtypes = parse_csv_filter(subtypes_value)
    rule_keys = parse_csv_filter(rule_keys_value)

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_audit_run_id = audit_run_id or latest_audit_run_id(conn)
        audit_run = fetch_audit_run(conn, selected_audit_run_id)
        overlay_run_id, active_gate = active_gate_overlay_run_id(conn, gate_key=gate_key)
        if int(audit_run["overlay_run_id"]) != overlay_run_id:
            raise RuntimeError(
                f"Promotion audit overlay {audit_run['overlay_run_id']} does not match active gate overlay {overlay_run_id}."
            )
        audit_rules = fetch_audit_rules(
            conn,
            run_id=selected_audit_run_id,
            statuses=statuses,
            routes=routes,
            subtypes=subtypes,
            rule_keys=rule_keys,
            max_rules=max_rules,
        )
        raw_rows = fetch_rows(conn, overlay_run_id=overlay_run_id, critical_only=False)
        active_rows = [enrich(row) for row in raw_rows]
        decisions = fetch_decisions(conn, policy_run_id=int(active_gate["active_policy_run_id"]))
        reviewed_ids = set(decisions)
        queued_ids = fetch_queued_ids(conn, gate_key=active_gate["gate_key"], overlay_run_id=overlay_run_id)

    selected_rows, rule_summaries = select_rows(
        active_rows=active_rows,
        audit_rules=audit_rules,
        reviewed_ids=reviewed_ids,
        queued_ids=queued_ids,
        include_reviewed=include_reviewed,
        include_queued=include_queued,
        limit_per_rule=limit_per_rule,
    )
    txt_path, csv_path, jsonl_path, decisions_template_path = write_outputs(
        settings,
        audit_run=audit_run,
        active_gate=active_gate,
        rows=selected_rows,
        rule_summaries=rule_summaries,
        limit_per_rule=limit_per_rule,
        include_reviewed=include_reviewed,
        include_queued=include_queued,
        plan_only=plan_only,
        started_at=started_at,
    )
    queue_run_id = None
    if not plan_only and selected_rows:
        route_filter = {rule["suggested_route"] for rule in rule_summaries if rule["selected_rows"] > 0}
        risk_filter = {row["overlay_risk_level"] for row in selected_rows}
        queue_run_id = record_active_gate_queue_run(
            settings,
            active_gate=active_gate,
            overlay_run_id=overlay_run_id,
            source_mode=SOURCE_MODE,
            rows=selected_rows,
            critical_only=False,
            route_filter=route_filter,
            risk_filter=risk_filter,
            limit=limit_per_rule,
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            decisions_template_path=decisions_template_path,
        )

    print("[ml_composite_subpolicy_promotion_queue] Queue generated")
    print(f"[ml_composite_subpolicy_promotion_queue] Rule version: {RULE_VERSION}")
    print(f"[ml_composite_subpolicy_promotion_queue] Audit run id: {audit_run['id']}")
    print(f"[ml_composite_subpolicy_promotion_queue] Gate key: {active_gate['gate_key']}")
    print(f"[ml_composite_subpolicy_promotion_queue] Overlay run id: {overlay_run_id}")
    print(f"[ml_composite_subpolicy_promotion_queue] Rules selected: {len(rule_summaries)}")
    print(f"[ml_composite_subpolicy_promotion_queue] Rows selected: {len(selected_rows)}")
    print(f"[ml_composite_subpolicy_promotion_queue] Queue run id: {queue_run_id}")
    print(f"[ml_composite_subpolicy_promotion_queue] Report: {txt_path}")
    print(f"[ml_composite_subpolicy_promotion_queue] CSV: {csv_path}")
    print(f"[ml_composite_subpolicy_promotion_queue] JSONL: {jsonl_path}")
    print(f"[ml_composite_subpolicy_promotion_queue] Decisions template: {decisions_template_path}")
    return {
        "queue_run_id": queue_run_id,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
        "rules_selected": len(rule_summaries),
        "rows_selected": len(selected_rows),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build targeted queues from composite subpolicy promotion audit families.")
    parser.add_argument("--audit-run-id", type=int, default=None)
    parser.add_argument("--statuses", default=DEFAULT_STATUSES)
    parser.add_argument("--routes", default=None)
    parser.add_argument("--subtypes", default=None)
    parser.add_argument("--rule-keys", default=None)
    parser.add_argument("--max-rules", type=int, default=5)
    parser.add_argument("--limit-per-rule", type=int, default=12)
    parser.add_argument("--include-reviewed", action="store_true")
    parser.add_argument("--skip-queued", action="store_true")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--gate-key", default=DEFAULT_ACTIVE_GATE_KEY)
    args = parser.parse_args()
    main(
        audit_run_id=args.audit_run_id,
        statuses_value=args.statuses,
        routes_value=args.routes,
        subtypes_value=args.subtypes,
        rule_keys_value=args.rule_keys,
        max_rules=args.max_rules,
        limit_per_rule=args.limit_per_rule,
        include_reviewed=args.include_reviewed,
        include_queued=not args.skip_queued,
        plan_only=args.plan_only,
        gate_key=args.gate_key,
    )
