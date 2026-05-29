from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import segment_token_overlay_review_queue
from ml_composite_review_progress import (
    build_route_statuses,
    fetch_decisions,
    fetch_latest_queue_runs,
    fetch_queued_policy_item_ids,
    pct,
)
from segment_token_overlay_review_queue import (
    DEFAULT_ACTIVE_GATE_KEY,
    active_gate_overlay_run_id,
    enrich,
    fetch_rows,
)


RULE_VERSION = "ml_composite_next_queue_cycle_v1"
RISK_PRIORITY = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def parse_route_filter(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def risk_sort_value(value: str) -> int:
    return RISK_PRIORITY.get(value, 9)


def aggregate_routes(route_statuses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    buckets_by_route: dict[str, set[str]] = defaultdict(set)
    risks_by_route: dict[str, set[str]] = defaultdict(set)

    for status in route_statuses:
        route = status["suggested_route"]
        current = grouped.setdefault(
            route,
            {
                "suggested_route": route,
                "overlay_risk_level": status["overlay_risk_level"],
                "total_items": 0,
                "queued_items": 0,
                "unqueued_items": 0,
                "reviewed_items": 0,
                "pending_items": 0,
                "approved_for_apply_count": 0,
                "rejected_count": 0,
                "fix_count": 0,
                "needs_subpolicy_count": 0,
                "manual_exception_count": 0,
                "latest_queue_run_id": None,
                "latest_queue_total_rows": 0,
            },
        )
        if risk_sort_value(status["overlay_risk_level"]) < risk_sort_value(current["overlay_risk_level"]):
            current["overlay_risk_level"] = status["overlay_risk_level"]
        for key in (
            "total_items",
            "queued_items",
            "unqueued_items",
            "reviewed_items",
            "pending_items",
            "approved_for_apply_count",
            "rejected_count",
            "fix_count",
            "needs_subpolicy_count",
            "manual_exception_count",
            "latest_queue_total_rows",
        ):
            current[key] += int(status.get(key) or 0)
        if status.get("latest_queue_run_id"):
            current["latest_queue_run_id"] = int(status["latest_queue_run_id"])
        buckets_by_route[route].add(status["overlay_policy_bucket"])
        risks_by_route[route].add(status["overlay_risk_level"])

    routes = []
    for route, status in grouped.items():
        total = int(status["total_items"])
        status["overlay_policy_buckets"] = sorted(buckets_by_route[route])
        status["overlay_risk_levels"] = sorted(risks_by_route[route], key=risk_sort_value)
        status["queue_coverage_pct"] = pct(int(status["queued_items"]), total)
        status["review_coverage_pct"] = pct(int(status["reviewed_items"]), total)
        routes.append(status)
    return routes


def select_route_plan(
    route_statuses: list[dict[str, Any]],
    *,
    max_routes: int,
    batch_size: int,
    route_filter: set[str],
) -> list[dict[str, Any]]:
    eligible = []
    for status in route_statuses:
        if route_filter and status["suggested_route"] not in route_filter:
            continue
        if int(status["unqueued_items"]) <= 0:
            continue
        planned = dict(status)
        planned["requested_limit"] = min(batch_size, int(status["unqueued_items"]))
        eligible.append(planned)

    eligible.sort(
        key=lambda row: (
            risk_sort_value(row["overlay_risk_level"]),
            row["queue_coverage_pct"],
            -int(row["unqueued_items"]),
            row["suggested_route"],
        )
    )
    return eligible[:max_routes]


def build_current_status(settings: dict[str, Any], *, gate_key: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        overlay_run_id, active_gate = active_gate_overlay_run_id(conn, gate_key=gate_key)
        raw_rows = fetch_rows(conn, overlay_run_id=overlay_run_id, critical_only=False)
        rows = [enrich(row) for row in raw_rows]
        decisions = fetch_decisions(conn, policy_run_id=int(active_gate["active_policy_run_id"]))
        latest_queue_runs = fetch_latest_queue_runs(conn)
        queued_policy_item_ids = fetch_queued_policy_item_ids(
            conn,
            gate_key=active_gate["gate_key"],
            overlay_run_id=int(active_gate["active_overlay_run_id"]),
        )
        route_statuses = build_route_statuses(rows, decisions, latest_queue_runs, queued_policy_item_ids)
    return active_gate, aggregate_routes(route_statuses)


def write_report(
    settings: dict[str, Any],
    *,
    active_gate: dict[str, Any],
    route_statuses: list[dict[str, Any]],
    selected_routes: list[dict[str, Any]],
    generated_results: list[dict[str, Any]],
    batch_size: int,
    max_routes: int,
    route_filter: set[str],
    plan_only: bool,
    started_at: datetime,
) -> Path:
    total_items = sum(int(row["total_items"]) for row in route_statuses)
    queued_items = sum(int(row["queued_items"]) for row in route_statuses)
    reviewed_items = sum(int(row["reviewed_items"]) for row in route_statuses)
    generated_by_route = {result["route"]: result for result in generated_results}

    lines = [
        "ML composite next queue cycle",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Gate key: {active_gate['gate_key']}",
        f"Checkpoint id: {active_gate['active_checkpoint_id']}",
        f"Overlay run id: {active_gate['active_overlay_run_id']}",
        f"Policy run id: {active_gate['active_policy_run_id']}",
        f"Plan only: {plan_only}",
        "",
        "Settings:",
        f"- Batch size per route: {batch_size}",
        f"- Max routes: {max_routes}",
        f"- Route filter: {', '.join(sorted(route_filter)) if route_filter else 'none'}",
        "- Skip reviewed: true",
        "- Skip queued: true",
        "",
        "Current coverage before this cycle:",
        f"- Total active gate items: {total_items}",
        f"- Queued items: {queued_items}",
        f"- Queue coverage: {pct(queued_items, total_items):.2f}%",
        f"- Reviewed items: {reviewed_items}",
        f"- Review coverage: {pct(reviewed_items, total_items):.2f}%",
        "",
        "Selected routes:",
    ]
    if selected_routes:
        for index, route in enumerate(selected_routes, start=1):
            generated = generated_by_route.get(route["suggested_route"])
            lines.append(
                (
                    "- #{rank} {route} | risk={risk} | total={total} queued={queued} "
                    "unqueued={unqueued} queue_coverage={queue_coverage:.2f}% "
                    "reviewed={reviewed} requested_limit={limit} generated_rows={generated_rows} "
                    "queue_run_id={queue_run_id}"
                ).format(
                    rank=index,
                    route=route["suggested_route"],
                    risk=route["overlay_risk_level"],
                    total=route["total_items"],
                    queued=route["queued_items"],
                    unqueued=route["unqueued_items"],
                    queue_coverage=route["queue_coverage_pct"],
                    reviewed=route["reviewed_items"],
                    limit=route["requested_limit"],
                    generated_rows=generated.get("rows_selected") if generated else "n/a",
                    queue_run_id=generated.get("queue_run_id") if generated else "n/a",
                )
            )
    else:
        lines.append("- none")

    lines.extend(["", "Generated files:"])
    if generated_results:
        for result in generated_results:
            lines.extend(
                [
                    f"- {result['route']}",
                    f"  queue_run_id: {result['queue_run_id']}",
                    f"  rows_selected: {result['rows_selected']}",
                    f"  report: {result['report_path']}",
                    f"  jsonl: {result['jsonl_path']}",
                    f"  decisions_template: {result['decisions_template_path']}",
                ]
            )
    else:
        lines.append("- none")

    lines.extend(
        [
            "",
            "Interpretation:",
            "- This command only creates review queues and records queue metadata.",
            "- It does not ingest human decisions.",
            "- It does not train models.",
            "- It does not update output files.",
        ]
    )
    return db.write_report(settings, "ml_composite_next_queue_cycle", lines)


def main(
    *,
    gate_key: str = DEFAULT_ACTIVE_GATE_KEY,
    batch_size: int = 25,
    max_routes: int = 4,
    route_filter_value: str | None = None,
    plan_only: bool = False,
) -> dict[str, Any]:
    if batch_size <= 0:
        raise RuntimeError("batch_size must be positive.")
    if max_routes <= 0:
        raise RuntimeError("max_routes must be positive.")

    settings = db.load_settings()
    started_at = datetime.now()
    route_filter = parse_route_filter(route_filter_value)
    active_gate, route_statuses = build_current_status(settings, gate_key=gate_key)
    selected_routes = select_route_plan(
        route_statuses,
        max_routes=max_routes,
        batch_size=batch_size,
        route_filter=route_filter,
    )

    generated_results: list[dict[str, Any]] = []
    if not plan_only:
        for route_plan in selected_routes:
            result = segment_token_overlay_review_queue.main(
                critical_only=False,
                use_active_gate=True,
                gate_key=gate_key,
                route=route_plan["suggested_route"],
                limit=int(route_plan["requested_limit"]),
                skip_reviewed=True,
                skip_queued=True,
            )
            generated_results.append({"route": route_plan["suggested_route"], **result})

    report_path = write_report(
        settings,
        active_gate=active_gate,
        route_statuses=route_statuses,
        selected_routes=selected_routes,
        generated_results=generated_results,
        batch_size=batch_size,
        max_routes=max_routes,
        route_filter=route_filter,
        plan_only=plan_only,
        started_at=started_at,
    )

    selected_summary = [
        {
            "route": row["suggested_route"],
            "risk": row["overlay_risk_level"],
            "unqueued_items": row["unqueued_items"],
            "requested_limit": row["requested_limit"],
        }
        for row in selected_routes
    ]
    print("[ml_composite_next_queue_cycle] Cycle planned")
    print(f"[ml_composite_next_queue_cycle] Rule version: {RULE_VERSION}")
    print(f"[ml_composite_next_queue_cycle] Gate key: {active_gate['gate_key']}")
    print(f"[ml_composite_next_queue_cycle] Checkpoint id: {active_gate['active_checkpoint_id']}")
    print(f"[ml_composite_next_queue_cycle] Overlay run id: {active_gate['active_overlay_run_id']}")
    print(f"[ml_composite_next_queue_cycle] Plan only: {plan_only}")
    print(f"[ml_composite_next_queue_cycle] Selected routes: {json.dumps(selected_summary, ensure_ascii=False)}")
    print(f"[ml_composite_next_queue_cycle] Generated queue runs: {[item['queue_run_id'] for item in generated_results]}")
    print(f"[ml_composite_next_queue_cycle] Report: {report_path}")
    return {
        "report_path": str(report_path),
        "selected_routes": selected_summary,
        "generated_results": generated_results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Plan and optionally generate the next active composite queue cycle.")
    parser.add_argument("--gate-key", default=DEFAULT_ACTIVE_GATE_KEY)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--max-routes", type=int, default=4)
    parser.add_argument("--routes", default=None, help="Comma-separated suggested_route filter.")
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    main(
        gate_key=args.gate_key,
        batch_size=args.batch_size,
        max_routes=args.max_routes,
        route_filter_value=args.routes,
        plan_only=args.plan_only,
    )
