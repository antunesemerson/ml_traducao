from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "issue_long_text_partial_composition_impact_v1"
POLICY_NAME = "long_text_partial_composition_impact_shadow_v1"
COORDINATOR_AGENT = "composition_coordinator_v1"
QUEUE_BUCKET = "long_text_composer_blocker"


def latest_decision_run_id(conn, decision_run_id: int | None) -> int:
    if decision_run_id is not None:
        return decision_run_id
    row = conn.execute(
        """
        SELECT run_id
        FROM ml_issue_review_decisions
        WHERE agent_key = ?
          AND queue_bucket = ?
          AND valid = 1
        GROUP BY run_id
        ORDER BY run_id DESC
        LIMIT 1
        """,
        (COORDINATOR_AGENT, QUEUE_BUCKET),
    ).fetchone()
    if row is None:
        raise RuntimeError("No reviewed long-text composition decision run found.")
    return int(row["run_id"])


def latest_id(conn, table_name: str) -> int | None:
    row = conn.execute(
        f"""
        SELECT id
        FROM {table_name}
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    return int(row["id"]) if row else None


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_partial_composition_impact_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            decision_run_id INTEGER NOT NULL,
            candidate_segment_count INTEGER NOT NULL DEFAULT 0,
            review_closed_baseline_count INTEGER NOT NULL DEFAULT 0,
            released_component_count INTEGER NOT NULL DEFAULT 0,
            released_component_segment_count INTEGER NOT NULL DEFAULT 0,
            blocker_component_count INTEGER NOT NULL DEFAULT 0,
            blocker_segment_count INTEGER NOT NULL DEFAULT 0,
            candidate_recheck_segment_count INTEGER NOT NULL DEFAULT 0,
            partial_covered_blocked_segment_count INTEGER NOT NULL DEFAULT 0,
            blocked_no_released_segment_count INTEGER NOT NULL DEFAULT 0,
            unmapped_needs_repair_segment_count INTEGER NOT NULL DEFAULT 0,
            potential_close_after_recheck_count INTEGER NOT NULL DEFAULT 0,
            component_source_counts_json TEXT,
            blocker_route_counts_json TEXT,
            coverage_state_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            json_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_long_text_partial_composition_impact_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            decision_run_id INTEGER NOT NULL,
            decision_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            normalized_decision TEXT NOT NULL,
            queue_bucket TEXT NOT NULL,
            coverage_state TEXT NOT NULL,
            released_component_count INTEGER NOT NULL DEFAULT 0,
            released_component_sources_json TEXT,
            blocker_count INTEGER NOT NULL DEFAULT 0,
            blocker_routes_json TEXT,
            potential_close_after_recheck INTEGER NOT NULL DEFAULT 0,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_long_text_partial_composition_impact_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], decision_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_long_text_partial_composition_impact_decision_run_{decision_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".json")


def fetch_decisions(conn, decision_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            id AS decision_id,
            segment_id,
            relative_path,
            source_key,
            source_line_number,
            normalized_decision,
            queue_bucket,
            notes
        FROM ml_issue_review_decisions
        WHERE run_id = ?
          AND agent_key = ?
          AND queue_bucket = ?
          AND valid = 1
        ORDER BY segment_id, source_key
        """,
        (decision_run_id, COORDINATOR_AGENT, QUEUE_BUCKET),
    ).fetchall()
    return [dict(row) for row in rows]


def add_component(
    components_by_segment: dict[int, list[dict[str, Any]]],
    *,
    segment_id: int,
    source: str,
    component_key: str,
    item_id: int,
    detail: str = "",
) -> None:
    components_by_segment[int(segment_id)].append(
        {
            "source": source,
            "component_key": component_key,
            "item_id": int(item_id),
            "detail": detail,
        }
    )


def gather_released_components(conn) -> tuple[dict[int, list[dict[str, Any]]], dict[str, int | None]]:
    components: dict[int, list[dict[str, Any]]] = defaultdict(list)
    run_ids = {
        "route_lifecycle_run_id": latest_id(conn, "ml_issue_long_text_repair_route_lifecycle_runs"),
        "structural_lifecycle_run_id": latest_id(conn, "ml_issue_long_text_structural_subpolicy_lifecycle_runs"),
        "split_lifecycle_run_id": latest_id(conn, "ml_issue_long_text_mixed_structural_split_lifecycle_runs"),
        "token_lifecycle_run_id": latest_id(conn, "ml_issue_long_text_mixed_structural_token_policy_lifecycle_runs"),
        "subject_pronoun_lifecycle_run_id": latest_id(conn, "ml_issue_long_text_subject_pronoun_form_lifecycle_runs"),
    }
    if run_ids["route_lifecycle_run_id"]:
        for row in conn.execute(
            """
            SELECT id, segment_id, repair_route, token_status
            FROM ml_issue_long_text_repair_route_lifecycle_items
            WHERE run_id = ?
              AND policy_allowed = 1
            """,
            (run_ids["route_lifecycle_run_id"],),
        ):
            add_component(
                components,
                segment_id=row["segment_id"],
                source="route_lifecycle",
                component_key=row["repair_route"],
                item_id=row["id"],
                detail=row["token_status"],
            )
    if run_ids["structural_lifecycle_run_id"]:
        for row in conn.execute(
            """
            SELECT id, segment_id, subpolicy_name, repair_route
            FROM ml_issue_long_text_structural_subpolicy_lifecycle_items
            WHERE run_id = ?
              AND policy_allowed = 1
            """,
            (run_ids["structural_lifecycle_run_id"],),
        ):
            add_component(
                components,
                segment_id=row["segment_id"],
                source="structural_lifecycle",
                component_key=row["subpolicy_name"],
                item_id=row["id"],
                detail=row["repair_route"],
            )
    if run_ids["split_lifecycle_run_id"]:
        for row in conn.execute(
            """
            SELECT id, segment_id, microagent_key, micro_issue_kind
            FROM ml_issue_long_text_mixed_structural_split_lifecycle_items
            WHERE run_id = ?
              AND policy_allowed = 1
              AND production_release_allowed = 0
            """,
            (run_ids["split_lifecycle_run_id"],),
        ):
            add_component(
                components,
                segment_id=row["segment_id"],
                source="mixed_split_lifecycle",
                component_key=row["microagent_key"],
                item_id=row["id"],
                detail=row["micro_issue_kind"],
            )
    if run_ids["token_lifecycle_run_id"]:
        for row in conn.execute(
            """
            SELECT id, segment_id, microagent_key, micro_issue_kind
            FROM ml_issue_long_text_mixed_structural_token_policy_lifecycle_items
            WHERE run_id = ?
              AND policy_allowed = 1
              AND production_release_allowed = 0
            """,
            (run_ids["token_lifecycle_run_id"],),
        ):
            add_component(
                components,
                segment_id=row["segment_id"],
                source="token_policy_lifecycle",
                component_key=row["microagent_key"],
                item_id=row["id"],
                detail=row["micro_issue_kind"],
            )
    if run_ids["subject_pronoun_lifecycle_run_id"]:
        for row in conn.execute(
            """
            SELECT id, segment_id, microagent_key, subcomponent_kind
            FROM ml_issue_long_text_subject_pronoun_form_lifecycle_items
            WHERE run_id = ?
              AND policy_allowed = 1
              AND production_release_allowed = 0
            """,
            (run_ids["subject_pronoun_lifecycle_run_id"],),
        ):
            add_component(
                components,
                segment_id=row["segment_id"],
                source="subject_pronoun_lifecycle",
                component_key=row["microagent_key"],
                item_id=row["id"],
                detail=row["subcomponent_kind"],
            )
    return components, run_ids


def gather_blockers(conn) -> tuple[dict[int, list[dict[str, Any]]], int | None]:
    blockers: dict[int, list[dict[str, Any]]] = defaultdict(list)
    run_id = latest_id(conn, "ml_issue_long_text_mixed_structural_token_policy_blocker_subsplit_runs")
    if not run_id:
        return blockers, None
    rows = conn.execute(
        """
        SELECT
            id,
            segment_id,
            microagent_key,
            micro_issue_kind,
            subcomponent_kind,
            subsplit_status,
            block_reason,
            review_route
        FROM ml_issue_long_text_mixed_structural_token_policy_blocker_subsplit_items
        WHERE run_id = ?
          AND subsplit_status != 'subsplit_ready'
        ORDER BY segment_id, microagent_key
        """,
        (run_id,),
    ).fetchall()
    for row in rows:
        blockers[int(row["segment_id"])].append(
            {
                "source": "token_policy_blocker_subsplit",
                "item_id": int(row["id"]),
                "microagent_key": row["microagent_key"],
                "micro_issue_kind": row["micro_issue_kind"],
                "subcomponent_kind": row["subcomponent_kind"],
                "subsplit_status": row["subsplit_status"],
                "block_reason": row["block_reason"],
                "review_route": row["review_route"],
            }
        )
    return blockers, run_id


def classify_segment(
    decision: dict[str, Any],
    components: list[dict[str, Any]],
    blockers: list[dict[str, Any]],
) -> tuple[str, int]:
    normalized = decision["normalized_decision"]
    if normalized == "composition_ready":
        return "review_closed_baseline", 1
    if blockers and components:
        return "partial_covered_blocked", 0
    if blockers:
        return "blocked_no_released_component", 0
    if components:
        return "candidate_composition_recheck", 1
    return "unmapped_needs_repair", 0


def build_items(
    decisions: list[dict[str, Any]],
    components_by_segment: dict[int, list[dict[str, Any]]],
    blockers_by_segment: dict[int, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for decision in decisions:
        segment_id = int(decision["segment_id"])
        components = components_by_segment.get(segment_id, [])
        blockers = blockers_by_segment.get(segment_id, [])
        coverage_state, potential = classify_segment(decision, components, blockers)
        items.append(
            {
                **decision,
                "coverage_state": coverage_state,
                "released_component_count": len(components),
                "released_component_sources": components,
                "blocker_count": len(blockers),
                "blocker_routes": blockers,
                "potential_close_after_recheck": potential,
            }
        )
    return items


def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
    state_counts = Counter(item["coverage_state"] for item in items)
    component_source_counts = Counter(
        component["source"]
        for item in items
        for component in item["released_component_sources"]
    )
    blocker_route_counts = Counter(
        blocker["review_route"] or blocker["subsplit_status"]
        for item in items
        for blocker in item["blocker_routes"]
    )
    released_component_count = sum(item["released_component_count"] for item in items)
    released_component_segments = sum(1 for item in items if item["released_component_count"] > 0)
    blocker_count = sum(item["blocker_count"] for item in items)
    blocker_segments = sum(1 for item in items if item["blocker_count"] > 0)
    return {
        "candidate_segment_count": len(items),
        "review_closed_baseline_count": state_counts["review_closed_baseline"],
        "released_component_count": released_component_count,
        "released_component_segment_count": released_component_segments,
        "blocker_component_count": blocker_count,
        "blocker_segment_count": blocker_segments,
        "candidate_recheck_segment_count": state_counts["candidate_composition_recheck"],
        "partial_covered_blocked_segment_count": state_counts["partial_covered_blocked"],
        "blocked_no_released_segment_count": state_counts["blocked_no_released_component"],
        "unmapped_needs_repair_segment_count": state_counts["unmapped_needs_repair"],
        "potential_close_after_recheck_count": sum(item["potential_close_after_recheck"] for item in items),
        "component_source_counts": dict(component_source_counts),
        "blocker_route_counts": dict(blocker_route_counts),
        "coverage_state_counts": dict(state_counts),
    }


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    json_path: Path,
    impact_run_id: int,
    decision_run_id: int,
    lifecycle_run_ids: dict[str, int | None],
    blocker_subsplit_run_id: int | None,
    items: list[dict[str, Any]],
    summary: dict[str, Any],
    started_at: datetime,
) -> None:
    fieldnames = [
        "decision_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "normalized_decision",
        "coverage_state",
        "released_component_count",
        "blocker_count",
        "potential_close_after_recheck",
        "released_component_sources_json",
        "blocker_routes_json",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in items:
            writer.writerow(
                {
                    **{field: item.get(field) for field in fieldnames},
                    "released_component_sources_json": json.dumps(
                        item["released_component_sources"], ensure_ascii=False, sort_keys=True
                    ),
                    "blocker_routes_json": json.dumps(item["blocker_routes"], ensure_ascii=False, sort_keys=True),
                }
            )
    payload = {
        "rule_version": RULE_VERSION,
        "policy_name": POLICY_NAME,
        "impact_run_id": impact_run_id,
        "decision_run_id": decision_run_id,
        "lifecycle_run_ids": lifecycle_run_ids,
        "blocker_subsplit_run_id": blocker_subsplit_run_id,
        "summary": summary,
        "items": items,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    state_counts = summary["coverage_state_counts"]
    lines = [
        "Issue long-text partial composition impact",
        f"Rule version: {RULE_VERSION}",
        f"Policy name: {POLICY_NAME}",
        f"Impact run id: {impact_run_id}",
        f"Decision run id: {decision_run_id}",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        "",
        "Input runs:",
        *[f"- {key}: {value}" for key, value in lifecycle_run_ids.items()],
        f"- blocker_subsplit_run_id: {blocker_subsplit_run_id}",
        "",
        "Summary:",
        f"- Candidate segments: {summary['candidate_segment_count']:,}",
        f"- Review-closed baseline: {summary['review_closed_baseline_count']:,}",
        f"- Released components: {summary['released_component_count']:,}",
        f"- Segments with released components: {summary['released_component_segment_count']:,}",
        f"- Blocker components: {summary['blocker_component_count']:,}",
        f"- Segments with blockers: {summary['blocker_segment_count']:,}",
        f"- Candidate composition recheck: {summary['candidate_recheck_segment_count']:,}",
        f"- Partial-covered but blocked: {summary['partial_covered_blocked_segment_count']:,}",
        f"- Blocked without released component: {summary['blocked_no_released_segment_count']:,}",
        f"- Unmapped needs-repair: {summary['unmapped_needs_repair_segment_count']:,}",
        f"- Potential close after recheck: {summary['potential_close_after_recheck_count']:,}",
        f"- Coverage states: {json.dumps(state_counts, ensure_ascii=False, sort_keys=True)}",
        f"- Component sources: {json.dumps(summary['component_source_counts'], ensure_ascii=False, sort_keys=True)}",
        f"- Blocker routes: {json.dumps(summary['blocker_route_counts'], ensure_ascii=False, sort_keys=True)}",
        "",
        "Interpretation:",
        "- Potential close after recheck is a shadow metric, not output application.",
        "- Candidate composition recheck means no blocker is known and at least one lifecycle component exists.",
        "- Partial-covered blocked means the network already fixed one part, but another component still needs a specialist.",
        "",
        "Segments by state:",
    ]
    for item in sorted(items, key=lambda row: (row["coverage_state"], row["segment_id"])):
        lines.append(
            (
                f"- {item['coverage_state']} | components={item['released_component_count']} "
                f"blockers={item['blocker_count']} potential={item['potential_close_after_recheck']} | "
                f"{item['relative_path']}::{item['source_key']}"
            )
        )
    lines.extend(
        [
            "",
            "Next focus by blocker route:",
        ]
    )
    if summary["blocker_route_counts"]:
        for route, count in Counter(summary["blocker_route_counts"]).most_common():
            lines.append(f"- {route}: {count:,}")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Safety note:",
            "- Diagnostic only: no source/output reads, no confirmation promotion, no segment-state closure.",
            "- This measures learning coverage and composition opportunity only.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, decision_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_decision_run_id = latest_decision_run_id(conn, decision_run_id)
        decisions = fetch_decisions(conn, selected_decision_run_id)
        if not decisions:
            raise RuntimeError(f"No valid long-text decisions found for run {selected_decision_run_id}.")
        components_by_segment, lifecycle_run_ids = gather_released_components(conn)
        blockers_by_segment, blocker_subsplit_run_id = gather_blockers(conn)
        items = build_items(decisions, components_by_segment, blockers_by_segment)
        summary = summarize(items)
        txt_path, csv_path, json_path = report_paths(settings, selected_decision_run_id)
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_long_text_partial_composition_impact_runs (
                rule_version,
                policy_name,
                decision_run_id,
                candidate_segment_count,
                review_closed_baseline_count,
                released_component_count,
                released_component_segment_count,
                blocker_component_count,
                blocker_segment_count,
                candidate_recheck_segment_count,
                partial_covered_blocked_segment_count,
                blocked_no_released_segment_count,
                unmapped_needs_repair_segment_count,
                potential_close_after_recheck_count,
                component_source_counts_json,
                blocker_route_counts_json,
                coverage_state_counts_json,
                report_path,
                csv_path,
                json_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                POLICY_NAME,
                selected_decision_run_id,
                summary["candidate_segment_count"],
                summary["review_closed_baseline_count"],
                summary["released_component_count"],
                summary["released_component_segment_count"],
                summary["blocker_component_count"],
                summary["blocker_segment_count"],
                summary["candidate_recheck_segment_count"],
                summary["partial_covered_blocked_segment_count"],
                summary["blocked_no_released_segment_count"],
                summary["unmapped_needs_repair_segment_count"],
                summary["potential_close_after_recheck_count"],
                json.dumps(summary["component_source_counts"], ensure_ascii=False, sort_keys=True),
                json.dumps(summary["blocker_route_counts"], ensure_ascii=False, sort_keys=True),
                json.dumps(summary["coverage_state_counts"], ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(json_path),
                started_at.isoformat(timespec="seconds"),
                now,
                now,
            ),
        )
        impact_run_id = int(cur.lastrowid)
        created_at = db.utc_now()
        for item in items:
            conn.execute(
                """
                INSERT INTO ml_issue_long_text_partial_composition_impact_items (
                    run_id,
                    decision_run_id,
                    decision_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    normalized_decision,
                    queue_bucket,
                    coverage_state,
                    released_component_count,
                    released_component_sources_json,
                    blocker_count,
                    blocker_routes_json,
                    potential_close_after_recheck,
                    notes,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    impact_run_id,
                    selected_decision_run_id,
                    int(item["decision_id"]),
                    int(item["segment_id"]),
                    item["relative_path"],
                    item["source_key"],
                    item.get("source_line_number"),
                    item["normalized_decision"],
                    item["queue_bucket"],
                    item["coverage_state"],
                    int(item["released_component_count"]),
                    json.dumps(item["released_component_sources"], ensure_ascii=False, sort_keys=True),
                    int(item["blocker_count"]),
                    json.dumps(item["blocker_routes"], ensure_ascii=False, sort_keys=True),
                    int(item["potential_close_after_recheck"]),
                    item.get("notes") or "",
                    created_at,
                ),
            )
        write_outputs(
            txt_path=txt_path,
            csv_path=csv_path,
            json_path=json_path,
            impact_run_id=impact_run_id,
            decision_run_id=selected_decision_run_id,
            lifecycle_run_ids=lifecycle_run_ids,
            blocker_subsplit_run_id=blocker_subsplit_run_id,
            items=items,
            summary=summary,
            started_at=started_at,
        )
        conn.commit()

    print("[issue_long_text_partial_composition_impact] Impact generated")
    print(f"[issue_long_text_partial_composition_impact] Rule version: {RULE_VERSION}")
    print(f"[issue_long_text_partial_composition_impact] Impact run id: {impact_run_id}")
    print(f"[issue_long_text_partial_composition_impact] Decision run id: {selected_decision_run_id}")
    print(f"[issue_long_text_partial_composition_impact] Candidate segments: {summary['candidate_segment_count']:,}")
    print(f"[issue_long_text_partial_composition_impact] Released components: {summary['released_component_count']:,}")
    print(f"[issue_long_text_partial_composition_impact] Candidate recheck: {summary['candidate_recheck_segment_count']:,}")
    print(f"[issue_long_text_partial_composition_impact] Potential close after recheck: {summary['potential_close_after_recheck_count']:,}")
    print(f"[issue_long_text_partial_composition_impact] Report: {txt_path}")
    return {
        "impact_run_id": impact_run_id,
        "decision_run_id": selected_decision_run_id,
        **summary,
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "json_path": str(json_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Measure partial long-text composition impact from shadow lifecycles.")
    parser.add_argument("--decision-run-id", type=int, default=None)
    args = parser.parse_args()
    main(decision_run_id=args.decision_run_id)
