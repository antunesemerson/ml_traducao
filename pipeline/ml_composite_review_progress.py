from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from segment_token_overlay_review_queue import (
    DEFAULT_ACTIVE_GATE_KEY,
    active_gate_overlay_run_id,
    enrich,
    fetch_rows,
)


RULE_VERSION = "ml_composite_review_progress_v1"


def pct(part: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(100.0 * part / total, 2)


def fetch_decisions(conn, *, policy_run_id: int) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            policy_item_id,
            decision,
            approved_for_apply,
            corrected_text,
            reviewer,
            updated_at
        FROM segment_token_policy_decisions
        WHERE policy_run_id = ?
        """,
        (policy_run_id,),
    ).fetchall()
    return {int(row["policy_item_id"]): dict(row) for row in rows}


def fetch_latest_queue_runs(conn) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_composite_gate_queue_runs
        WHERE source_mode = 'active_composite_gate'
          AND route_filter_csv IS NOT NULL
        ORDER BY started_at DESC, id DESC
        """
    ).fetchall()
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        route_filter = row["route_filter_csv"] or ""
        routes = [item.strip() for item in route_filter.split(",") if item.strip()]
        if len(routes) != 1:
            continue
        route = routes[0]
        if route not in latest:
            latest[route] = dict(row)
    return latest


def fetch_queued_policy_item_ids(conn, *, gate_key: str, overlay_run_id: int) -> set[int]:
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


def decision_counters(rows: list[dict[str, Any]], decisions: dict[int, dict[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        decision = decisions.get(int(row["policy_item_id"]))
        if not decision:
            counts["pending_items"] += 1
            continue
        counts["reviewed_items"] += 1
        decision_name = decision["decision"]
        counts[f"decision_{decision_name}"] += 1
        if int(decision["approved_for_apply"] or 0):
            counts["approved_for_apply_count"] += 1
        if decision_name in {"reject_policy_candidate", "needs_subpolicy"}:
            counts["rejected_count"] += 1
        if decision_name in {"fix_confirmed_text", "encoding_cleanup_required", "manual_token_rewrite_required"}:
            counts["fix_count"] += 1
        if decision_name == "needs_subpolicy":
            counts["needs_subpolicy_count"] += 1
        if decision_name == "keep_manual_exception_only":
            counts["manual_exception_count"] += 1
    return counts


def grouped_rows(rows: list[dict[str, Any]]) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (row["suggested_route"], row["overlay_policy_bucket"], row["overlay_risk_level"])
        groups[key].append(row)
    return groups


def write_report(
    settings: dict[str, Any],
    *,
    active_gate: dict[str, Any],
    rows: list[dict[str, Any]],
    decisions: dict[int, dict[str, Any]],
    route_statuses: list[dict[str, Any]],
    queued_policy_item_ids: set[int],
    started_at: datetime,
) -> Path:
    total_counts = decision_counters(rows, decisions)
    queued_count = sum(1 for row in rows if int(row["policy_item_id"]) in queued_policy_item_ids)
    lines = [
        "ML composite gate review progress",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Gate key: {active_gate['gate_key']}",
        f"Checkpoint id: {active_gate['active_checkpoint_id']}",
        f"Overlay run id: {active_gate['active_overlay_run_id']}",
        f"Policy run id: {active_gate['active_policy_run_id']}",
        "",
        "Summary:",
        f"- Total active gate items: {len(rows)}",
        f"- Queued items: {queued_count}",
        f"- Unqueued items: {len(rows) - queued_count}",
        f"- Queue coverage: {pct(queued_count, len(rows)):.2f}%",
        f"- Reviewed items: {total_counts['reviewed_items']}",
        f"- Pending items: {total_counts['pending_items']}",
        f"- Review coverage: {pct(total_counts['reviewed_items'], len(rows)):.2f}%",
        f"- Approved for apply: {total_counts['approved_for_apply_count']}",
        f"- Rejected/needs subpolicy: {total_counts['rejected_count']}",
        f"- Fix/manual rewrite: {total_counts['fix_count']}",
        f"- Needs subpolicy: {total_counts['needs_subpolicy_count']}",
        f"- Manual exceptions: {total_counts['manual_exception_count']}",
        "",
        "Routes:",
    ]
    for status in route_statuses:
        lines.append(
            "- {route} | {bucket} | {risk} | total={total} reviewed={reviewed} "
            "pending={pending} review_coverage={coverage:.2f}% queued={queued} queue_coverage={queue_coverage:.2f}% "
            "approved={approved} rejected={rejected} fixes={fixes} "
            "latest_queue={queue_id} queue_rows={queue_rows}".format(
                route=status["suggested_route"],
                bucket=status["overlay_policy_bucket"],
                risk=status["overlay_risk_level"],
                total=status["total_items"],
                reviewed=status["reviewed_items"],
                pending=status["pending_items"],
                coverage=status["review_coverage_pct"],
                queued=status["queued_items"],
                queue_coverage=status["queue_coverage_pct"],
                approved=status["approved_for_apply_count"],
                rejected=status["rejected_count"],
                fixes=status["fix_count"],
                queue_id=status.get("latest_queue_run_id") or "none",
                queue_rows=status.get("latest_queue_total_rows") or 0,
            )
        )
    return db.write_report(settings, "ml_composite_review_progress", lines)


def insert_snapshot(
    conn,
    *,
    active_gate: dict[str, Any],
    rows: list[dict[str, Any]],
    decisions: dict[int, dict[str, Any]],
    route_statuses: list[dict[str, Any]],
    queued_policy_item_ids: set[int],
    report_path: Path,
    started_at: datetime,
) -> int:
    finished_at = datetime.now().isoformat(timespec="seconds")
    started_at_text = started_at.isoformat(timespec="seconds")
    total_counts = decision_counters(rows, decisions)
    queued_count = sum(1 for row in rows if int(row["policy_item_id"]) in queued_policy_item_ids)
    cursor = conn.execute(
        """
        INSERT INTO ml_composite_gate_review_snapshots (
            rule_version,
            gate_key,
            checkpoint_id,
            overlay_run_id,
            source_policy_run_id,
            total_items,
            queued_items,
            unqueued_items,
            reviewed_items,
            pending_items,
            approved_for_apply_count,
            rejected_count,
            fix_count,
            needs_subpolicy_count,
            manual_exception_count,
            review_coverage_pct,
            queue_coverage_pct,
            report_path,
            started_at,
            finished_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            RULE_VERSION,
            active_gate["gate_key"],
            active_gate["active_checkpoint_id"],
            active_gate["active_overlay_run_id"],
            active_gate["active_policy_run_id"],
            len(rows),
            queued_count,
            len(rows) - queued_count,
            total_counts["reviewed_items"],
            total_counts["pending_items"],
            total_counts["approved_for_apply_count"],
            total_counts["rejected_count"],
            total_counts["fix_count"],
            total_counts["needs_subpolicy_count"],
            total_counts["manual_exception_count"],
            pct(total_counts["reviewed_items"], len(rows)),
            pct(queued_count, len(rows)),
            str(report_path),
            started_at_text,
            finished_at,
            finished_at,
        ),
    )
    snapshot_id = int(cursor.lastrowid)
    for status in route_statuses:
        conn.execute(
            """
            INSERT INTO ml_composite_gate_review_route_status (
                snapshot_id,
                suggested_route,
                overlay_policy_bucket,
                overlay_risk_level,
                total_items,
                queued_items,
                unqueued_items,
                reviewed_items,
                pending_items,
                approved_for_apply_count,
                rejected_count,
                fix_count,
                needs_subpolicy_count,
                manual_exception_count,
                review_coverage_pct,
                queue_coverage_pct,
                latest_queue_run_id,
                latest_queue_total_rows,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                status["suggested_route"],
                status["overlay_policy_bucket"],
                status["overlay_risk_level"],
                status["total_items"],
                status["queued_items"],
                status["unqueued_items"],
                status["reviewed_items"],
                status["pending_items"],
                status["approved_for_apply_count"],
                status["rejected_count"],
                status["fix_count"],
                status["needs_subpolicy_count"],
                status["manual_exception_count"],
                status["review_coverage_pct"],
                status["queue_coverage_pct"],
                status.get("latest_queue_run_id"),
                status.get("latest_queue_total_rows") or 0,
                finished_at,
            ),
        )
    return snapshot_id


def build_route_statuses(
    rows: list[dict[str, Any]],
    decisions: dict[int, dict[str, Any]],
    latest_queue_runs: dict[str, dict[str, Any]],
    queued_policy_item_ids: set[int],
) -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = []
    for (route, bucket, risk), group in grouped_rows(rows).items():
        counts = decision_counters(group, decisions)
        queued_count = sum(1 for row in group if int(row["policy_item_id"]) in queued_policy_item_ids)
        queue_run = latest_queue_runs.get(route)
        statuses.append(
            {
                "suggested_route": route,
                "overlay_policy_bucket": bucket,
                "overlay_risk_level": risk,
                "total_items": len(group),
                "queued_items": queued_count,
                "unqueued_items": len(group) - queued_count,
                "reviewed_items": counts["reviewed_items"],
                "pending_items": counts["pending_items"],
                "approved_for_apply_count": counts["approved_for_apply_count"],
                "rejected_count": counts["rejected_count"],
                "fix_count": counts["fix_count"],
                "needs_subpolicy_count": counts["needs_subpolicy_count"],
                "manual_exception_count": counts["manual_exception_count"],
                "review_coverage_pct": pct(counts["reviewed_items"], len(group)),
                "queue_coverage_pct": pct(queued_count, len(group)),
                "latest_queue_run_id": int(queue_run["id"]) if queue_run else None,
                "latest_queue_total_rows": int(queue_run["total_rows"]) if queue_run else 0,
            }
        )
    return sorted(statuses, key=lambda row: (row["review_coverage_pct"], -row["total_items"], row["suggested_route"]))


def main(*, gate_key: str = DEFAULT_ACTIVE_GATE_KEY) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
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
        report_path = write_report(
            settings,
            active_gate=active_gate,
            rows=rows,
            decisions=decisions,
            route_statuses=route_statuses,
            queued_policy_item_ids=queued_policy_item_ids,
            started_at=started_at,
        )
        snapshot_id = insert_snapshot(
            conn,
            active_gate=active_gate,
            rows=rows,
            decisions=decisions,
            route_statuses=route_statuses,
            queued_policy_item_ids=queued_policy_item_ids,
            report_path=report_path,
            started_at=started_at,
        )
        conn.commit()

    total_counts = decision_counters(rows, decisions)
    queued_count = len(queued_policy_item_ids)
    print("[ml_composite_review_progress] Snapshot generated")
    print(f"[ml_composite_review_progress] Rule version: {RULE_VERSION}")
    print(f"[ml_composite_review_progress] Snapshot id: {snapshot_id}")
    print(f"[ml_composite_review_progress] Gate key: {active_gate['gate_key']}")
    print(f"[ml_composite_review_progress] Checkpoint id: {active_gate['active_checkpoint_id']}")
    print(f"[ml_composite_review_progress] Overlay run id: {active_gate['active_overlay_run_id']}")
    print(f"[ml_composite_review_progress] Policy run id: {active_gate['active_policy_run_id']}")
    print(f"[ml_composite_review_progress] Total items: {len(rows)}")
    print(f"[ml_composite_review_progress] Queued items: {queued_count}")
    print(f"[ml_composite_review_progress] Queue coverage: {pct(queued_count, len(rows)):.2f}%")
    print(f"[ml_composite_review_progress] Reviewed items: {total_counts['reviewed_items']}")
    print(f"[ml_composite_review_progress] Pending items: {total_counts['pending_items']}")
    print(f"[ml_composite_review_progress] Review coverage: {pct(total_counts['reviewed_items'], len(rows)):.2f}%")
    print(f"[ml_composite_review_progress] Approved for apply: {total_counts['approved_for_apply_count']}")
    print(f"[ml_composite_review_progress] Report: {report_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Summarize review progress for the active composite gate.")
    parser.add_argument("--gate-key", default=DEFAULT_ACTIVE_GATE_KEY)
    args = parser.parse_args()
    main(gate_key=args.gate_key)
