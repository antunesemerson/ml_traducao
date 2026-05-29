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
from ml_composite_subpolicy_diagnostic import classify_token_subtype
from segment_token_overlay_review_queue import (
    DEFAULT_ACTIVE_GATE_KEY,
    active_gate_overlay_run_id,
    enrich,
    fetch_rows,
    parse_csv_filter,
    record_active_gate_queue_run,
    slugify,
)


RULE_VERSION = "ml_composite_subpolicy_evidence_queue_v1"
SOURCE_MODE = "active_composite_subpolicy_evidence_queue"
DEFAULT_STATUSES = "ready_to_design_subpolicy"


def latest_diagnostic_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_composite_subpolicy_diagnostic_runs
        ORDER BY started_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No ml_composite_subpolicy_diagnostic_runs entry found.")
    return int(row["id"])


def fetch_diagnostic_run(conn, run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_composite_subpolicy_diagnostic_runs
        WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Subpolicy diagnostic run {run_id} was not found.")
    return dict(row)


def fetch_diagnostic_groups(
    conn,
    *,
    run_id: int,
    statuses: set[str],
    routes: set[str],
    subtypes: set[str],
    max_groups: int | None,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_composite_subpolicy_diagnostic_items
        WHERE run_id = ?
        ORDER BY
          CASE maturity_status
            WHEN 'policy_candidate_review' THEN 0
            WHEN 'ready_to_design_subpolicy' THEN 1
            WHEN 'negative_boundary_learned' THEN 2
            WHEN 'conflicting_evidence' THEN 3
            WHEN 'queued_waiting_review' THEN 4
            WHEN 'needs_more_review' THEN 5
            WHEN 'needs_queue' THEN 6
            ELSE 9
          END,
          reviewed_items DESC,
          total_items DESC,
          suggested_route,
          token_subtype
        """,
        (run_id,),
    ).fetchall()
    groups = []
    for row in rows:
        group = dict(row)
        if statuses and group["maturity_status"] not in statuses:
            continue
        if routes and group["suggested_route"] not in routes:
            continue
        if subtypes and group["token_subtype"] not in subtypes:
            continue
        groups.append(group)
        if max_groups is not None and len(groups) >= max_groups:
            break
    return groups


def fetch_decision_ids(conn, *, policy_run_id: int) -> set[int]:
    rows = conn.execute(
        """
        SELECT policy_item_id
        FROM segment_token_policy_decisions
        WHERE policy_run_id = ?
        """,
        (policy_run_id,),
    ).fetchall()
    return {int(row["policy_item_id"]) for row in rows}


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


def hypothesis_for(route: str, subtype: str) -> str:
    known = {
        (
            "dynamic_scope_token_review",
            "dynamic_name_form_rewrite",
        ): "Test whether dynamic name-form substitutions preserve the same referent, tooltip/style semantics, and sentence grammar.",
        (
            "select_cstring_dynamic_context_review",
            "select_cstring_to_pronoun_rewrite",
        ): "Test whether Select_CString literal gender context can be replaced by pronoun/scope wording without losing gender or referent information.",
        (
            "gender_token_subspecialist_review",
            "pronoun_added_for_pt_fluency",
        ): "Test whether added PT-BR pronouns improve fluency while pointing to the same CK3 character and avoiding ambiguity.",
        (
            "mixed_token_change_review",
            "name_and_gender_token_rewrite",
        ): "Test whether combined name-form plus gender/article rewrites can be split into a safe narrower policy.",
    }
    return known.get(
        (route, subtype),
        "Collect positive and negative evidence for this route/subtype before any guarded policy promotion.",
    )


def select_rows(
    rows: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    *,
    reviewed_ids: set[int],
    queued_ids: set[int],
    include_reviewed: bool,
    include_queued: bool,
    limit_per_group: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected: list[dict[str, Any]] = []
    group_summaries: list[dict[str, Any]] = []

    rows_by_group: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        subtype, families = classify_token_subtype(row)
        row["token_subtype"] = subtype
        row["token_families"] = sorted(families)
        key = (row["suggested_route"], subtype, row["overlay_policy_bucket"], row["overlay_risk_level"])
        rows_by_group.setdefault(key, []).append(row)

    for group in groups:
        key = (
            group["suggested_route"],
            group["token_subtype"],
            group["overlay_policy_bucket"],
            group["overlay_risk_level"],
        )
        candidates = []
        skipped_reviewed = 0
        skipped_queued = 0
        for row in rows_by_group.get(key, []):
            policy_item_id = int(row["policy_item_id"])
            if not include_reviewed and policy_item_id in reviewed_ids:
                skipped_reviewed += 1
                continue
            if not include_queued and policy_item_id in queued_ids:
                skipped_queued += 1
                continue
            candidate = dict(row)
            candidate["subpolicy_maturity_status"] = group["maturity_status"]
            candidate["subpolicy_confidence_band"] = group["confidence_band"]
            candidate["subpolicy_recommended_action"] = group["recommended_action"]
            candidate["subpolicy_hypothesis"] = hypothesis_for(group["suggested_route"], group["token_subtype"])
            candidates.append(candidate)

        candidates.sort(key=lambda row: (row["relative_path"], int(row.get("source_line_number") or 0), row["source_key"]))
        picked = candidates[:limit_per_group]
        selected.extend(picked)
        group_summaries.append(
            {
                "suggested_route": group["suggested_route"],
                "token_subtype": group["token_subtype"],
                "maturity_status": group["maturity_status"],
                "total_group_items": group["total_items"],
                "reviewed_group_items": group["reviewed_items"],
                "pending_group_items": group["pending_items"],
                "skipped_reviewed": skipped_reviewed,
                "skipped_queued": skipped_queued,
                "available_candidates": len(candidates),
                "selected_rows": len(picked),
                "hypothesis": hypothesis_for(group["suggested_route"], group["token_subtype"]),
            }
        )
    return selected, group_summaries


def write_outputs(
    settings: dict[str, Any],
    *,
    diagnostic_run: dict[str, Any],
    active_gate: dict[str, Any],
    rows: list[dict[str, Any]],
    group_summaries: list[dict[str, Any]],
    limit_per_group: int,
    include_reviewed: bool,
    include_queued: bool,
    plan_only: bool,
    started_at: datetime,
) -> tuple[Path, Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    slug = "subpolicy_evidence"
    if len(group_summaries) == 1:
        group = group_summaries[0]
        slug += "_" + slugify(f"{group['suggested_route']}_{group['token_subtype']}")
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
        "subpolicy_maturity_status",
        "subpolicy_hypothesis",
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
                            f"{RULE_VERSION}; route={row['suggested_route']}; "
                            f"subtype={row['token_subtype']}; hypothesis={row['subpolicy_hypothesis']}; "
                            "use accept_policy_candidate only for a safe narrow reusable pattern; "
                            "use reject_policy_candidate for unsafe boundary examples."
                        ),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    route_counts = Counter(row["suggested_route"] for row in rows)
    subtype_counts = Counter(row["token_subtype"] for row in rows)
    lines = [
        "ML composite subpolicy evidence queue",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Plan only: {plan_only}",
        f"Gate key: {active_gate['gate_key']}",
        f"Checkpoint id: {active_gate['active_checkpoint_id']}",
        f"Overlay run id: {active_gate['active_overlay_run_id']}",
        f"Policy run id: {active_gate['active_policy_run_id']}",
        f"Diagnostic run id: {diagnostic_run['id']}",
        "",
        "Settings:",
        f"- Limit per group: {limit_per_group}",
        f"- Include reviewed: {include_reviewed}",
        f"- Include queued: {include_queued}",
        "",
        "Rows:",
        f"- Selected rows: {len(rows)}",
        "",
        "Routes:",
        *[f"- {key}: {value}" for key, value in route_counts.most_common()],
        "",
        "Subtypes:",
        *[f"- {key}: {value}" for key, value in subtype_counts.most_common()],
        "",
        "Groups:",
    ]
    for group in group_summaries:
        lines.extend(
            [
                f"- {group['suggested_route']} / {group['token_subtype']}",
                f"  maturity: {group['maturity_status']}",
                f"  group total/reviewed/pending: {group['total_group_items']}/{group['reviewed_group_items']}/{group['pending_group_items']}",
                f"  skipped reviewed/queued: {group['skipped_reviewed']}/{group['skipped_queued']}",
                f"  available candidates: {group['available_candidates']}",
                f"  selected rows: {group['selected_rows']}",
                f"  hypothesis: {group['hypothesis']}",
            ]
        )
    lines.extend(["", "Review sample:"])
    for row in rows[:80]:
        lines.extend(
            [
                (
                    f"- item {row['policy_item_id']} | {row['suggested_route']} / {row['token_subtype']} | "
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
            "- This queue asks for positive/negative evidence for narrow subpolicy design.",
            "- The decisions template defaults to needs_subpolicy as a safety brake.",
            "- This command does not ingest decisions, train models, promote policies, or update output files.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, csv_path, jsonl_path, decisions_template_path


def main(
    *,
    diagnostic_run_id: int | None = None,
    statuses_value: str | None = DEFAULT_STATUSES,
    routes_value: str | None = None,
    subtypes_value: str | None = None,
    max_groups: int | None = 4,
    limit_per_group: int = 12,
    include_reviewed: bool = False,
    include_queued: bool = False,
    plan_only: bool = False,
    gate_key: str = DEFAULT_ACTIVE_GATE_KEY,
) -> dict[str, Any]:
    if limit_per_group <= 0:
        raise RuntimeError("limit_per_group must be positive.")
    if max_groups is not None and max_groups <= 0:
        raise RuntimeError("max_groups must be positive when provided.")

    settings = db.load_settings()
    started_at = datetime.now()
    statuses = parse_csv_filter(statuses_value)
    routes = parse_csv_filter(routes_value)
    subtypes = parse_csv_filter(subtypes_value)

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        selected_diagnostic_run_id = diagnostic_run_id or latest_diagnostic_run_id(conn)
        diagnostic_run = fetch_diagnostic_run(conn, selected_diagnostic_run_id)
        overlay_run_id, active_gate = active_gate_overlay_run_id(conn, gate_key=gate_key)
        if int(diagnostic_run["overlay_run_id"]) != overlay_run_id:
            raise RuntimeError(
                f"Diagnostic run overlay {diagnostic_run['overlay_run_id']} does not match active gate overlay {overlay_run_id}."
            )
        groups = fetch_diagnostic_groups(
            conn,
            run_id=selected_diagnostic_run_id,
            statuses=statuses,
            routes=routes,
            subtypes=subtypes,
            max_groups=max_groups,
        )
        raw_rows = fetch_rows(conn, overlay_run_id=overlay_run_id, critical_only=False)
        rows = [enrich(row) for row in raw_rows]
        reviewed_ids = fetch_decision_ids(conn, policy_run_id=int(active_gate["active_policy_run_id"]))
        queued_ids = fetch_queued_ids(conn, gate_key=active_gate["gate_key"], overlay_run_id=overlay_run_id)

    selected_rows, group_summaries = select_rows(
        rows,
        groups,
        reviewed_ids=reviewed_ids,
        queued_ids=queued_ids,
        include_reviewed=include_reviewed,
        include_queued=include_queued,
        limit_per_group=limit_per_group,
    )
    txt_path, csv_path, jsonl_path, decisions_template_path = write_outputs(
        settings,
        diagnostic_run=diagnostic_run,
        active_gate=active_gate,
        rows=selected_rows,
        group_summaries=group_summaries,
        limit_per_group=limit_per_group,
        include_reviewed=include_reviewed,
        include_queued=include_queued,
        plan_only=plan_only,
        started_at=started_at,
    )
    queue_run_id = None
    if not plan_only:
        route_filter = {group["suggested_route"] for group in group_summaries if group["selected_rows"] > 0}
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
            limit=limit_per_group,
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            decisions_template_path=decisions_template_path,
        )

    print("[ml_composite_subpolicy_evidence_queue] Queue generated")
    print(f"[ml_composite_subpolicy_evidence_queue] Rule version: {RULE_VERSION}")
    print(f"[ml_composite_subpolicy_evidence_queue] Diagnostic run id: {diagnostic_run['id']}")
    print(f"[ml_composite_subpolicy_evidence_queue] Gate key: {active_gate['gate_key']}")
    print(f"[ml_composite_subpolicy_evidence_queue] Overlay run id: {overlay_run_id}")
    print(f"[ml_composite_subpolicy_evidence_queue] Groups selected: {len(group_summaries)}")
    print(f"[ml_composite_subpolicy_evidence_queue] Rows selected: {len(selected_rows)}")
    print(f"[ml_composite_subpolicy_evidence_queue] Queue run id: {queue_run_id}")
    print(f"[ml_composite_subpolicy_evidence_queue] Report: {txt_path}")
    print(f"[ml_composite_subpolicy_evidence_queue] CSV: {csv_path}")
    print(f"[ml_composite_subpolicy_evidence_queue] JSONL: {jsonl_path}")
    print(f"[ml_composite_subpolicy_evidence_queue] Decisions template: {decisions_template_path}")
    return {
        "queue_run_id": queue_run_id,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "decisions_template_path": str(decisions_template_path),
        "groups_selected": len(group_summaries),
        "rows_selected": len(selected_rows),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build evidence queues for composite subpolicy candidates.")
    parser.add_argument("--diagnostic-run-id", type=int, default=None)
    parser.add_argument("--statuses", default=DEFAULT_STATUSES)
    parser.add_argument("--routes", default=None)
    parser.add_argument("--subtypes", default=None)
    parser.add_argument("--max-groups", type=int, default=4)
    parser.add_argument("--limit-per-group", type=int, default=12)
    parser.add_argument("--include-reviewed", action="store_true")
    parser.add_argument("--include-queued", action="store_true")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--gate-key", default=DEFAULT_ACTIVE_GATE_KEY)
    args = parser.parse_args()
    main(
        diagnostic_run_id=args.diagnostic_run_id,
        statuses_value=args.statuses,
        routes_value=args.routes,
        subtypes_value=args.subtypes,
        max_groups=args.max_groups,
        limit_per_group=args.limit_per_group,
        include_reviewed=args.include_reviewed,
        include_queued=args.include_queued,
        plan_only=args.plan_only,
        gate_key=args.gate_key,
    )
